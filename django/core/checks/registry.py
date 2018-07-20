from itertools import chain

from django.utils.itercompat import is_iterable


class Tags:
    """
    Built-in tags for internal checks.
    """
    admin = 'admin'
    caches = 'caches'
    compatibility = 'compatibility'
    database = 'database'
    models = 'models'
    security = 'security'
    signals = 'signals'
    templates = 'templates'
    urls = 'urls'


class CheckRegistry:

    def __init__(self):
        self.registered_checks = set()
        self.deployment_checks = set()

    def register(self, check=None, *tags, **kwargs):
        """
        Can be used as a function or a decorator. Register given function
        `f` labeled with given `tags`. The function should receive **kwargs
        and return list of Errors and Warnings.

        Example::

            registry = CheckRegistry()
            @registry.register('mytag', 'anothertag')
            def my_check(apps, **kwargs):
                # ... perform checks and collect `errors` ...
                return errors
            # or
            registry.register(my_check, 'mytag', 'anothertag')
        """
        # 装饰器，所有需要被调用的checker，都需要注册到这里来
        kwargs.setdefault('deploy', False)

        def inner(check):
            check.tags = tags
            checks = self.deployment_checks if kwargs['deploy'] else self.registered_checks
            checks.add(check)
            return check

        if callable(check):
            return inner(check)
        else:
            if check:
                tags += (check, )
            return inner

    def run_checks(self, app_configs=None, tags=None, include_deployment_checks=False):
        """
        Run all registered checks and return list of Errors and Warnings.
        """
        # 调用一遍所有已经注册过的checker
        errors = []
        checks = self.get_checks(include_deployment_checks)

        if tags is not None:
            checks = [check for check in checks if not set(check.tags).isdisjoint(tags)]
        else:
            # By default, 'database'-tagged checks are not run as they do more
            # than mere static code analysis.
            checks = [check for check in checks if Tags.database not in check.tags]

        """
        遍历checker，调用。
        所有的checker都在每个App的checks.py下。
        比如，在Admin下面，有contrib/admin/checks.py
        所有的checks.py模块中定义的checker，比如admin中的:check_admin_app
        而check的注册是在contrib/admin/apps.py中完成的。
        """

        for check in checks:
            new_errors = check(app_configs=app_configs)
            assert is_iterable(new_errors), (
                "The function %r did not return a list. All functions registered "
                "with the checks registry must return a list." % check)
            errors.extend(new_errors)
        return errors

    def tag_exists(self, tag, include_deployment_checks=False):
        return tag in self.tags_available(include_deployment_checks)

    def tags_available(self, deployment_checks=False):
        return set(chain.from_iterable(
            check.tags for check in self.get_checks(deployment_checks)
        ))

    def get_checks(self, include_deployment_checks=False):
        checks = list(self.registered_checks)
        if include_deployment_checks:
            checks.extend(self.deployment_checks)
        return checks


registry = CheckRegistry()
register = registry.register
run_checks = registry.run_checks
tag_exists = registry.tag_exists
