import logging
import types

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured, MiddlewareNotUsed
from django.db import connections, transaction
from django.urls import get_resolver, set_urlconf
from django.utils.module_loading import import_string

from .exception import convert_exception_to_response, get_exception_response

logger = logging.getLogger('django.request')


class BaseHandler:
    _request_middleware = None
    _view_middleware = None
    _template_response_middleware = None
    _response_middleware = None
    _exception_middleware = None
    _middleware_chain = None

    def load_middleware(self):
        """
        Populate middleware lists from settings.MIDDLEWARE.

        Must be called after the environment is fixed (see __call__ in subclasses).
        """
        self._request_middleware = []
        self._view_middleware = []
        self._template_response_middleware = []
        self._response_middleware = []
        self._exception_middleware = []

        handler = convert_exception_to_response(self._get_response)
        """
        拆分Middleware的配置，所谓的尘归尘，土归土，view的归view，process_exception的归process_exception

        需要理解的是Django Middleware的逻辑，先调用process request，然后调用process view，最后调用process_response。
        在process request和process response之间，就是下面拆分之后需要处理的。具体逻辑视频里讲的比较清楚了。
        """
        for middleware_path in reversed(settings.MIDDLEWARE):
            """
            'django.middleware.security.SecurityMiddleware',
            'django.contrib.sessions.middleware.SessionMiddleware',
            """
            middleware = import_string(middleware_path)
            try:
                mw_instance = middleware(handler)
            except MiddlewareNotUsed as exc:
                if settings.DEBUG:
                    if str(exc):
                        logger.debug('MiddlewareNotUsed(%r): %s', middleware_path, exc)
                    else:
                        logger.debug('MiddlewareNotUsed: %r', middleware_path)
                continue

            if mw_instance is None:
                raise ImproperlyConfigured(
                    'Middleware factory %s returned None.' % middleware_path
                )

            # 参考：https://docs.djangoproject.com/en/2.0/topics/http/middleware/#process-template-response
            if hasattr(mw_instance, 'process_view'):
                self._view_middleware.insert(0, mw_instance.process_view)
            if hasattr(mw_instance, 'process_template_response'):
                self._template_response_middleware.append(mw_instance.process_template_response)
            if hasattr(mw_instance, 'process_exception'):
                self._exception_middleware.append(mw_instance.process_exception)

            # mw_instance  = 'session', 'security'
            handler = convert_exception_to_response(mw_instance)

        # We only assign to this when initialization is complete as it is used
        # as a flag for initialization being complete.
        self._middleware_chain = handler

    def make_view_atomic(self, view):
        non_atomic_requests = getattr(view, '_non_atomic_requests', set())
        for db in connections.all():
            if db.settings_dict['ATOMIC_REQUESTS'] and db.alias not in non_atomic_requests:
                view = transaction.atomic(using=db.alias)(view)
        return view

    def get_exception_response(self, request, resolver, status_code, exception):
        return get_exception_response(request, resolver, status_code, exception, self.__class__)

    def get_response(self, request):
        """Return an HttpResponse object for the given HttpRequest."""
        # Setup default url resolver for this thread
        # import pdb;pdb.set_trace()
        set_urlconf(settings.ROOT_URLCONF)

        response = self._middleware_chain(request)

        response._closable_objects.append(request)

        # If the exception handler returns a TemplateResponse that has not
        # been rendered, force it to be rendered.
        if not getattr(response, 'is_rendered', True) and callable(getattr(response, 'render', None)):
            response = response.render()

        if response.status_code == 404:
            logger.warning(
                'Not Found: %s', request.path,
                extra={'status_code': 404, 'request': request},
            )

        return response

    def _get_response(self, request):
        """
        Resolve and call the view, then apply view, exception, and
        template_response middleware. This method is everything that happens
        inside the request/response middleware.
        """
        # import pdb;pdb.set_trace()
        response = None

        if hasattr(request, 'urlconf'):
            urlconf = request.urlconf
            set_urlconf(urlconf)
            resolver = get_resolver(urlconf)
        else:
            # 4.3_1 by the5fire
            # 前面做了set_urlconf的操作，这里在获取到
            # 我们需要看下它返回的对象: django.urls.resolvers.URLResolver
            resolver = get_resolver()

        # 4.3_1 by the5fire
        # 从这里开始看URL的解析
        # 解析当前请求应该被哪个方法（View）来处理
        # 最终返回：ResolverMatch(func=myapp.views.index, args=(), kwargs={}, url_name=None, app_names=[], namespaces=[])
        resolver_match = resolver.resolve(request.path_info)
        callback, callback_args, callback_kwargs = resolver_match
        request.resolver_match = resolver_match

        # Apply view middleware
        # Middleware逻辑开始
        for middleware_method in self._view_middleware:
            response = middleware_method(request, callback, callback_args, callback_kwargs)
            if response:
                break

        if response is None:
            # 4.3_1 by the5fire
            # Atomic: 事务原子性
            # Django默认的特性，在settings的DATABASES中可以配置
            # 参考： https://docs.djangoproject.com/en/2.1/topics/db/transactions/#tying-transactions-to-http-requests
            # 这里的wrapped_callback就是views.index函数
            wrapped_callback = self.make_view_atomic(callback)
            try:
                response = wrapped_callback(request, *callback_args, **callback_kwargs)
            except Exception as e:
                response = self.process_exception_by_middleware(e, request)

        # Complain if the view returned None (a common error).
        if response is None:
            if isinstance(callback, types.FunctionType):    # FBV
                view_name = callback.__name__
            else:                                           # CBV
                view_name = callback.__class__.__name__ + '.__call__'

            raise ValueError(
                "The view %s.%s didn't return an HttpResponse object. It "
                "returned None instead." % (callback.__module__, view_name)
            )

        # If the response supports deferred rendering, apply template
        # response middleware and then render the response
        elif hasattr(response, 'render') and callable(response.render):
            for middleware_method in self._template_response_middleware:
                response = middleware_method(request, response)
                # Complain if the template response middleware returned None (a common error).
                if response is None:
                    raise ValueError(
                        "%s.process_template_response didn't return an "
                        "HttpResponse object. It returned None instead."
                        % (middleware_method.__self__.__class__.__name__)
                    )

            try:
                response = response.render()
            except Exception as e:
                response = self.process_exception_by_middleware(e, request)

        return response

    def process_exception_by_middleware(self, exception, request):
        """
        Pass the exception to the exception middleware. If no middleware
        return a response for this exception, raise it.
        """
        for middleware_method in self._exception_middleware:
            response = middleware_method(request, exception)
            if response:
                return response
        raise
