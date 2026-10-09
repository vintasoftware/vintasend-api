"""The app inside a host project: absent settings, the host's authenticator, a mount prefix.

A host installs ``vintasend_api.dashboard`` and includes ``vintasend_api.dashboard.urls``
under a prefix of its own. It has none of the bundled project's settings unless it sets them,
and it may authenticate callers itself through ``VINTASEND_API_AUTHENTICATOR`` instead of the
shared key. Mirrors the TypeScript reference's ``createApp({ authenticate, ... })``.

The names under test are imported inside each test, so one that does not exist fails that
test rather than the whole module.
"""

from typing import Any, Callable

from django.http import HttpRequest
from django.test import Client, RequestFactory
from django.urls import reverse

import pytest

from ..errors import ApiError
from .fixtures import AUTH_HEADERS, FakeService


pytestmark = pytest.mark.django_db

HOST_URLCONF = "vintasend_api.dashboard.tests.host_urls"
PREFIX = "/notifications-api"
LIST = "/api/v1/notifications"
ORIGIN = "https://dashboard.example"

# Every setting the app reads apart from the service factory, which a host must set.
APP_SETTINGS = [
    "VINTASEND_API_KEY",
    "VINTASEND_API_AUTHENTICATOR",
    "VINTASEND_BACKEND_IDENTIFIER",
    "VINTASEND_API_CORS_ORIGINS",
    "VINTASEND_UNHANDLED_ERROR_HANDLER",
    "GITHUB_REPO",
    "GITHUB_API_KEY",
    "GITHUB_API_BASE_URL",
    "GITHUB_TEMPLATES_BASE_PATH",
    "GITHUB_TEMPLATE_CACHE_MAX_ENTRIES",
    "GITHUB_TEMPLATE_TIMEOUT_SECONDS",
]


# --- authenticators a host might configure -----------------------------------------------

# A module-level name that resolves but is not callable, for the system check.
NOT_CALLABLE = "this is not an authenticator"


def refuse_as_forbidden(request: HttpRequest) -> None:
    raise ApiError("FORBIDDEN", "Not allowed.")


def refuse_as_unauthorized(request: HttpRequest) -> None:
    raise ApiError("UNAUTHORIZED", "Sign in first.")


async def refuse_asynchronously(request: HttpRequest) -> None:
    raise ApiError("FORBIDDEN", "Not allowed.")


def accept_the_host_token(request: HttpRequest) -> None:
    from ..auth import bearer_token

    if bearer_token(request) != "host-token":
        raise ApiError("UNAUTHORIZED", "Sign in first.")


HOST_HEADERS = {"Authorization": "Bearer host-token"}


def _factory() -> FakeService:
    return FakeService()


@pytest.fixture
def bare_host(settings: Any) -> Any:
    """A host that set only the service factory: every other app setting is absent."""
    for name in APP_SETTINGS:
        if hasattr(settings, name):
            delattr(settings, name)
    settings.NOTIFICATION_SERVICE_FACTORY = f"{__name__}._factory"
    return settings


@pytest.fixture
def mounted(settings: Any) -> Any:
    settings.ROOT_URLCONF = HOST_URLCONF
    return settings


# --- absent settings ---------------------------------------------------------------------


def test_absent_settings_read_as_their_defaults(bare_host: Any) -> None:
    from .. import conf

    assert conf.api_key() == ""
    assert conf.service_factory() == f"{__name__}._factory"
    assert conf.hook_setting(conf.AUTHENTICATOR) is None
    assert conf.backend_identifier() is None
    assert conf.cors_origins() == []
    assert conf.hook_setting(conf.UNHANDLED_ERROR_HANDLER) is None
    assert conf.github_repo() == ""
    assert conf.github_api_key() == ""
    assert conf.github_api_base_url() == "https://api.github.com"
    assert conf.github_templates_base_path() == ""
    assert conf.github_template_cache_max_entries() == 100
    assert conf.github_template_timeout_seconds() == 10


def test_settings_are_read_per_request(bare_host: Any) -> None:
    from .. import conf

    bare_host.VINTASEND_API_KEY = "changed"

    assert conf.api_key() == "changed"


def test_cors_origins_may_be_a_comma_separated_string(bare_host: Any) -> None:
    from .. import conf

    bare_host.VINTASEND_API_CORS_ORIGINS = " https://a.example, ,https://b.example "

    assert conf.cors_origins() == ["https://a.example", "https://b.example"]


def test_the_system_check_reads_an_absent_api_key_as_unset(bare_host: Any) -> None:
    from ..apps import check_api_configuration

    assert [error.id for error in check_api_configuration(None)] == ["vintasend_api.E001"]


def test_a_request_is_served_with_every_optional_setting_absent(
    bare_host: Any, client: Client
) -> None:
    """The service is built from the factory alone, and CORS stays off."""
    bare_host.VINTASEND_API_KEY = "test-api-key"

    response = client.get(LIST, headers={**AUTH_HEADERS, "Origin": ORIGIN})

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response


def test_template_preview_settings_fall_back_to_their_defaults(bare_host: Any) -> None:
    from ..template_source import create_github_template_client_from_settings

    bare_host.GITHUB_REPO = "synthetic-org/templates"
    bare_host.GITHUB_API_KEY = "synthetic-token"

    client = create_github_template_client_from_settings()

    assert client.repo == "synthetic-org/templates"
    assert client.api_base_url == "https://api.github.com"
    assert client.templates_base_path == ""
    assert client.cache_max_entries == 100
    assert client.timeout_seconds == 10


def test_an_absent_template_repository_is_a_template_source_error(bare_host: Any) -> None:
    from ..template_source import TemplateSourceError, create_github_template_client_from_settings

    with pytest.raises(TemplateSourceError, match="GITHUB_REPO is required"):
        create_github_template_client_from_settings()


# --- the host's authenticator ------------------------------------------------------------


def test_the_authenticator_replaces_the_api_key(
    client: Client, settings: Any, install_service: Callable[..., Any]
) -> None:
    """A FORBIDDEN it raises is a 403 in the contract envelope, even with the right key."""
    install_service()
    settings.VINTASEND_API_AUTHENTICATOR = f"{__name__}.refuse_as_forbidden"

    response = client.get(LIST, headers=AUTH_HEADERS)

    assert response.status_code == 403
    assert response.json() == {"error": {"code": "FORBIDDEN", "message": "Not allowed."}}


def test_an_unauthorized_from_the_authenticator_is_a_401(
    client: Client, settings: Any, install_service: Callable[..., Any]
) -> None:
    install_service()
    settings.VINTASEND_API_AUTHENTICATOR = f"{__name__}.refuse_as_unauthorized"

    response = client.get(LIST)

    assert response.status_code == 401
    assert response.json() == {"error": {"code": "UNAUTHORIZED", "message": "Sign in first."}}


def test_an_authenticator_needs_no_api_key(
    client: Client, bare_host: Any, install_service: Callable[..., Any]
) -> None:
    install_service()
    bare_host.VINTASEND_API_AUTHENTICATOR = f"{__name__}.accept_the_host_token"

    assert client.get(LIST, headers=HOST_HEADERS).status_code == 200
    assert client.get(LIST, headers={"Authorization": "Bearer other"}).status_code == 401
    assert client.get(LIST).status_code == 401


def test_the_authenticator_may_be_the_callable_itself(
    client: Client, settings: Any, install_service: Callable[..., Any]
) -> None:
    install_service()
    settings.VINTASEND_API_AUTHENTICATOR = refuse_as_forbidden

    assert client.get(LIST, headers=AUTH_HEADERS).status_code == 403


def test_an_async_authenticator_is_awaited(
    client: Client, settings: Any, install_service: Callable[..., Any]
) -> None:
    """A coroutine nobody awaits would let every caller through."""
    install_service()
    settings.VINTASEND_API_AUTHENTICATOR = refuse_asynchronously

    assert client.get(LIST, headers=AUTH_HEADERS).status_code == 403


def test_the_authenticator_receives_the_request(
    client: Client, settings: Any, install_service: Callable[..., Any]
) -> None:
    install_service()
    seen: list[str] = []

    def record(request: HttpRequest) -> None:
        seen.append(request.path)

    settings.VINTASEND_API_AUTHENTICATOR = record

    assert client.get(LIST).status_code == 200
    assert seen == [LIST]


def test_an_unresolvable_authenticator_fails_closed(
    client: Client, settings: Any, install_service: Callable[..., Any]
) -> None:
    """A broken setting must not fall back to the key, let alone to no check at all."""
    install_service()
    settings.VINTASEND_API_AUTHENTICATOR = "no.such.module.authenticate"

    assert client.get(LIST, headers=AUTH_HEADERS).status_code == 500


def test_health_stays_open_behind_an_authenticator(client: Client, settings: Any) -> None:
    settings.VINTASEND_API_AUTHENTICATOR = refuse_as_forbidden

    assert client.get("/health").status_code == 200


def test_the_auth_module_exports_what_a_host_authenticator_needs() -> None:
    from .. import auth

    assert auth.ApiError is ApiError
    assert ApiError.unauthorized("Sign in first.").status == 401
    assert ApiError.forbidden("Not allowed.").status == 403


def test_check_api_key_is_the_shared_key_check(settings: Any) -> None:
    from ..auth import check_api_key

    factory = RequestFactory()

    check_api_key(factory.get("/", headers=AUTH_HEADERS))
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}):
        with pytest.raises(ApiError) as refused:
            check_api_key(factory.get("/", headers=headers))
        assert refused.value.code == "UNAUTHORIZED"


def test_configured_authenticator_resolves_a_dotted_path(settings: Any) -> None:
    from ..auth import configured_authenticator

    settings.VINTASEND_API_AUTHENTICATOR = f"{__name__}.refuse_as_forbidden"

    assert configured_authenticator() is refuse_as_forbidden


# --- system checks -----------------------------------------------------------------------


def test_an_authenticator_makes_the_api_key_optional(bare_host: Any) -> None:
    from ..apps import check_api_configuration

    bare_host.VINTASEND_API_AUTHENTICATOR = f"{__name__}.refuse_as_forbidden"

    assert check_api_configuration(None) == []


@pytest.mark.parametrize(
    "value", ["no.such.module.authenticate", f"{__name__}.NOT_CALLABLE", "not-a-dotted-path"]
)
def test_an_unusable_authenticator_is_reported(bare_host: Any, value: str) -> None:
    from ..apps import check_api_configuration

    bare_host.VINTASEND_API_AUTHENTICATOR = value

    assert [error.id for error in check_api_configuration(None)] == ["vintasend_api.E004"]


# --- bearer_token ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "token"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER   abc  ", "abc"),
        ("Bearer a.b-c_d", "a.b-c_d"),
        ("Bearer ", None),
        ("Bearer    ", None),
        ("Bearer", None),
        ("Basic abc", None),
        ("abc", None),
        ("", None),
        (None, None),
    ],
)
def test_bearer_token_reads_a_header(header: str | None, token: str | None) -> None:
    from ..auth import bearer_token

    assert bearer_token(header) == token


def test_bearer_token_reads_a_request() -> None:
    from ..auth import bearer_token

    factory = RequestFactory()

    assert bearer_token(factory.get("/", headers={"Authorization": "Bearer abc"})) == "abc"
    assert bearer_token(factory.get("/")) is None


# --- mounting under a host's prefix ------------------------------------------------------


def test_the_app_urlconf_serves_health_under_a_prefix(mounted: Any, client: Client) -> None:
    response = client.get(f"{PREFIX}/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "apiVersion": "v1"}


def test_the_app_urlconf_serves_the_api_under_a_prefix(
    mounted: Any, client: Client, install_service: Callable[..., Any]
) -> None:
    install_service()

    assert client.get(f"{PREFIX}{LIST}", headers=AUTH_HEADERS).status_code == 200
    unauthenticated = client.get(f"{PREFIX}{LIST}")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "UNAUTHORIZED"
    assert client.get(LIST, headers=AUTH_HEADERS).status_code == 404


def test_the_authenticator_guards_the_prefixed_api(
    mounted: Any, client: Client, install_service: Callable[..., Any]
) -> None:
    install_service()
    mounted.VINTASEND_API_AUTHENTICATOR = refuse_as_forbidden

    assert client.get(f"{PREFIX}{LIST}", headers=AUTH_HEADERS).status_code == 403


def test_the_404_envelope_keeps_its_old_dotted_path() -> None:
    from django.utils.module_loading import import_string

    from ..views import envelope_404

    assert import_string("vintasend_api.urls.envelope_404") is envelope_404
    assert import_string("vintasend_api.urls.handler404") == (
        "vintasend_api.dashboard.views.envelope_404"
    )


def test_reverse_finds_the_prefixed_api(mounted: Any) -> None:
    assert reverse("vintasend_api:api-root") == f"{PREFIX}/api/v1/"


def test_cors_follows_the_api_under_a_prefix(
    mounted: Any, client: Client, install_service: Callable[..., Any]
) -> None:
    install_service()
    mounted.VINTASEND_API_CORS_ORIGINS = [ORIGIN]

    response = client.get(f"{PREFIX}{LIST}", headers={**AUTH_HEADERS, "Origin": ORIGIN})

    assert response.status_code == 200
    assert response["Access-Control-Allow-Origin"] == ORIGIN
    assert "Access-Control-Allow-Origin" not in client.get(
        f"{PREFIX}/health", headers={"Origin": ORIGIN}
    )


def test_cors_still_covers_the_standalone_prefix(
    client: Client, settings: Any, install_service: Callable[..., Any]
) -> None:
    install_service()
    settings.VINTASEND_API_CORS_ORIGINS = [ORIGIN]

    response = client.get(LIST, headers={**AUTH_HEADERS, "Origin": ORIGIN})

    assert response["Access-Control-Allow-Origin"] == ORIGIN


# --- URL namespaces ----------------------------------------------------------------------


def test_the_api_has_its_own_url_namespace() -> None:
    """Ninja's default, ``api-<version>``, is the templates management API's too: both
    mounted in one project would collide."""
    from ..api import api, health_api

    assert api.urls_namespace == "vintasend_api"
    assert health_api.urls_namespace not in {api.urls_namespace, f"api-{health_api.version}"}
    assert reverse("vintasend_api:api-root") == "/api/v1/"


def test_the_contract_check_holds_under_a_prefix(mounted: Any) -> None:
    """The test client's contract check must not depend on where the API was mounted when it
    first ran, and must check a mounted route as the operation it is."""
    from .contract import declared_statuses, undeclared_status

    declared_statuses.cache_clear()

    assert ("GET", "/api/v1/notifications") in declared_statuses()
    route = "notifications-api/api/v1/notifications/<id>/cancel"
    assert undeclared_status("POST", route, 404) is None
    assert undeclared_status("POST", route, 400) is not None
