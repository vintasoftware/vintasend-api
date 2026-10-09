"""App configuration, including the startup checks that make a bad deployment fail fast."""

from django.apps import AppConfig
from django.core.checks import Error, register

from . import conf
from .auth import configured_authenticator
from .hooks import HANDLER_SETTING, configured_handler


class DashboardConfig(AppConfig):
    name = "vintasend_api.dashboard"
    label = "vintasend_dashboard"
    verbose_name = "VintaSend Dashboard API"


@register()
def check_api_configuration(app_configs: object, **kwargs: object) -> list[Error]:
    """Refuse to start without the settings every request depends on.

    Registered as a Django system check rather than raised at import time so
    ``manage.py`` stays usable and the message arrives as a readable checklist. Both
    ``runserver`` and ``manage.py check`` run these; ``gunicorn`` deployments should run
    ``manage.py check --deploy`` in their release step to get the same guarantee.

    ``VINTASEND_API_KEY`` is only required when no ``VINTASEND_API_AUTHENTICATOR`` is
    configured: an authenticator replaces the key check.

    The ``GITHUB_*`` settings are deliberately absent: they are only read when
    ``/preview`` is called, so an API that does not use template previews is correctly
    configured without them.
    """
    errors: list[Error] = []

    if not conf.hook_setting(conf.AUTHENTICATOR):
        if not conf.api_key():
            errors.append(
                Error(
                    "VINTASEND_API_KEY is not set.",
                    hint=(
                        "Every /api/v1 request must present this as a bearer token. Set it "
                        "to a long random string shared with the dashboard, or set "
                        f"{conf.AUTHENTICATOR} to authenticate callers yourself."
                    ),
                    id="vintasend_api.E001",
                )
            )
    else:
        try:
            configured_authenticator()
        except (ImportError, TypeError) as error:
            errors.append(
                Error(
                    f"{conf.AUTHENTICATOR} cannot be used: {error}",
                    hint=(
                        "Point it at an importable callable taking the request, or leave it "
                        "empty to require VINTASEND_API_KEY instead."
                    ),
                    id="vintasend_api.E004",
                )
            )

    if not conf.service_factory():
        errors.append(
            Error(
                "NOTIFICATION_SERVICE_FACTORY is not set.",
                hint=(
                    "Point it at a callable returning a configured NotificationService "
                    "or AsyncIONotificationService, for example "
                    "'vintasend_api.vintasend_config.create_notification_service'. "
                    "Start from vintasend_config.example.py."
                ),
                id="vintasend_api.E002",
            )
        )

    try:
        configured_handler()
    except (ImportError, TypeError) as error:
        errors.append(
            Error(
                f"{HANDLER_SETTING} cannot be used: {error}",
                hint="Point it at an importable callable, or leave it empty.",
                id="vintasend_api.E003",
            )
        )

    return errors
