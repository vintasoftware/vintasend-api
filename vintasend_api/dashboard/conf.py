"""Every setting the app reads, with the default it takes when a host leaves it out.

The bundled project's ``settings.py`` defines all of them from the environment, but a host
project that embeds the app defines only what it needs. Each one is read here, through
``getattr`` with a default, and nowhere else, so a setting the host never heard of is a
default rather than an ``AttributeError`` on the first request.

Read on every call rather than captured at import, so a test using ``override_settings`` --
and a deployment that reloads settings -- sees the change.

Required, with an empty default that the system checks report:

- ``NOTIFICATION_SERVICE_FACTORY`` -- dotted path to the service factory.
- ``VINTASEND_API_KEY`` -- the shared bearer secret. Not required when an authenticator is set.

Optional:

- ``VINTASEND_API_AUTHENTICATOR`` -- ``(request) -> None``, replacing the shared-key check.
- ``VINTASEND_BACKEND_IDENTIFIER`` -- a non-primary backend registered in the service.
- ``VINTASEND_API_CORS_ORIGINS`` -- browser origins the CORS middleware allows; none by default.
- ``VINTASEND_UNHANDLED_ERROR_HANDLER`` -- ``(exc, request, request_id) -> None``.
- ``GITHUB_*`` -- the template source for ``/preview``, only read when it is called.

The two callables are resolved by ``hooks.configured_hook``, which reads them through
``hook_setting`` below.
"""

from collections.abc import Iterable

from django.conf import settings


AUTHENTICATOR = "VINTASEND_API_AUTHENTICATOR"
API_KEY = "VINTASEND_API_KEY"
SERVICE_FACTORY = "NOTIFICATION_SERVICE_FACTORY"
BACKEND_IDENTIFIER = "VINTASEND_BACKEND_IDENTIFIER"
CORS_ORIGINS = "VINTASEND_API_CORS_ORIGINS"
UNHANDLED_ERROR_HANDLER = "VINTASEND_UNHANDLED_ERROR_HANDLER"

DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"
DEFAULT_GITHUB_TEMPLATE_CACHE_MAX_ENTRIES = 100
DEFAULT_GITHUB_TEMPLATE_TIMEOUT_SECONDS = 10


def _text(name: str) -> str:
    value = getattr(settings, name, "")
    return value if isinstance(value, str) else ""


def _int(name: str, default: int) -> int:
    value = getattr(settings, name, None)
    return default if value is None or value == "" else int(value)


def api_key() -> str:
    """The shared secret, or ``""`` when there is none -- which no request can match."""
    return _text(API_KEY)


def service_factory() -> str:
    """The dotted path to the service factory, or ``""`` when it is not set."""
    return _text(SERVICE_FACTORY)


def backend_identifier() -> str | None:
    """A non-primary backend registered in the service, or None for the primary one."""
    return _text(BACKEND_IDENTIFIER) or None


def cors_origins() -> list[str]:
    """The browser origins allowed to call the API. Empty means none.

    A host's ``settings.py`` may write it as one comma-separated string, the way the
    environment variable carries it, as readily as a list.
    """
    value: str | Iterable[str] | None = getattr(settings, CORS_ORIGINS, None)
    if not value:
        return []
    entries = value.split(",") if isinstance(value, str) else value
    return [entry.strip() for entry in entries if entry.strip()]


def hook_setting(name: str) -> object:
    """A hook setting's raw value -- a callable, a dotted path, or ``None`` when unset."""
    return getattr(settings, name, None)


# --- template preview (GitHub) -------------------------------------------------------
# Only read when /preview is called, so an API that does not use previews needs none.


def github_repo() -> str:
    return _text("GITHUB_REPO")


def github_api_key() -> str:
    return _text("GITHUB_API_KEY")


def github_api_base_url() -> str:
    return _text("GITHUB_API_BASE_URL") or DEFAULT_GITHUB_API_BASE_URL


def github_templates_base_path() -> str:
    return _text("GITHUB_TEMPLATES_BASE_PATH")


def github_template_cache_max_entries() -> int:
    return _int("GITHUB_TEMPLATE_CACHE_MAX_ENTRIES", DEFAULT_GITHUB_TEMPLATE_CACHE_MAX_ENTRIES)


def github_template_timeout_seconds() -> int:
    return _int("GITHUB_TEMPLATE_TIMEOUT_SECONDS", DEFAULT_GITHUB_TEMPLATE_TIMEOUT_SECONDS)
