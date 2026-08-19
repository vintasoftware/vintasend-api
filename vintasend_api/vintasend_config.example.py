"""Example VintaSend service factory.

Copy this file to ``vintasend_api/vintasend_config.py`` (gitignored) and adapt it to your
own backend, adapters and template renderer, then point the setting at it::

    NOTIFICATION_SERVICE_FACTORY=vintasend_api.vintasend_config.create_notification_service

The only contract is: expose a callable that returns a configured ``NotificationService``
or ``AsyncIONotificationService``. The API calls it once per process and reuses the
result, so it must be safe to call once and the service it returns must be safe to share
across requests.

This is the same setting VintaSend's background-send worker reads, so one factory can
serve the worker and this API. Point both at it and they are guaranteed to agree about
which backend holds the notifications.

The imports below are illustrative -- install the implementation packages your deployment
actually uses (``vintasend-django``, ``vintasend-sqlalchemy``, ``vintasend-fastapi-mail``,
``vintasend-flask-mail``, ``vintasend-jinja``, ``vintasend-s3-attachments``, ...).
"""

from typing import Any


def create_notification_service() -> Any:
    raise NotImplementedError(
        "No VintaSend service configured. Copy vintasend_config.example.py to "
        "vintasend_config.py, build your service there, and set "
        "NOTIFICATION_SERVICE_FACTORY to point at it."
    )

    # A Django deployment, reading the notifications your app already writes:
    #
    # from vintasend.services.notification_service import NotificationService
    # from vintasend_django.services.notification_backends.django_db_notification_backend import (
    #     DjangoDbNotificationBackend,
    # )
    # from vintasend_django.services.notification_adapters.django_email import (
    #     DjangoEmailNotificationAdapter,
    # )
    # from vintasend_django.services.notification_template_renderers.django_templated_email_renderer import (  # noqa: E501
    #     DjangoTemplatedEmailRenderer,
    # )
    #
    # backend = DjangoDbNotificationBackend()
    # renderer = DjangoTemplatedEmailRenderer()
    # adapter = DjangoEmailNotificationAdapter(renderer, backend)
    #
    # return NotificationService(
    #     notification_adapters=[adapter],
    #     notification_backend=backend,
    # )

    # A SQLAlchemy deployment -- a FastAPI application's notifications served by this
    # Django API. The backend seam is what makes that work: nothing about the store has
    # to match the framework serving the HTTP layer.
    #
    # from vintasend_sqlalchemy.services.notification_backends.sqlalchemy_notification_backend import (  # noqa: E501
    #     SQLAlchemyNotificationBackend,
    # )
    #
    # backend = SQLAlchemyNotificationBackend(session_factory=my_session_factory)
    # ...

    # Context generators must be imported before a preview regenerates a context, since
    # @register_context populates a registry at import time. Importing the module that
    # defines them here is the simplest way to guarantee that:
    #
    # import myapp.notifications.contexts  # noqa: F401
