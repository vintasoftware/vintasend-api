"""Startup checks and service loading.

Mirrors the TypeScript reference's ``test/config.test.ts``, which asserts that a
misconfigured deployment fails immediately rather than on the first request.
"""

from typing import Any

import pytest

from ..apps import check_api_configuration
from ..service import (
    ServiceCaller,
    ServiceConfigurationError,
    load_notification_service,
    set_service_caller,
)
from .fixtures import FakeService


# --- system checks -------------------------------------------------------------------


def test_a_complete_configuration_passes(settings: Any) -> None:
    settings.VINTASEND_API_KEY = "a-key"
    settings.NOTIFICATION_SERVICE_FACTORY = "some.module.factory"

    assert check_api_configuration(None) == []


def test_a_missing_api_key_is_reported(settings: Any) -> None:
    settings.VINTASEND_API_KEY = ""
    settings.NOTIFICATION_SERVICE_FACTORY = "some.module.factory"

    assert [error.id for error in check_api_configuration(None)] == ["vintasend_api.E001"]


def test_a_missing_service_factory_is_reported(settings: Any) -> None:
    settings.VINTASEND_API_KEY = "a-key"
    settings.NOTIFICATION_SERVICE_FACTORY = ""

    assert [error.id for error in check_api_configuration(None)] == ["vintasend_api.E002"]


def test_missing_github_settings_are_not_an_error(settings: Any) -> None:
    """Template previews are optional, so an API that does not use them is correctly
    configured without any ``GITHUB_*`` setting."""
    settings.VINTASEND_API_KEY = "a-key"
    settings.NOTIFICATION_SERVICE_FACTORY = "some.module.factory"
    settings.GITHUB_REPO = ""
    settings.GITHUB_API_KEY = ""

    assert check_api_configuration(None) == []


# --- service loading -----------------------------------------------------------------

_SERVICE = FakeService()

# A module-level name that resolves but is not callable, for the check below.
_NOT_CALLABLE = "this is not a factory"


def _factory() -> FakeService:
    return _SERVICE


async def _async_factory() -> FakeService:
    return _SERVICE


def _not_a_factory() -> None:
    return None


def test_loads_the_service_the_factory_returns() -> None:
    assert load_notification_service(f"{__name__}._factory") is _SERVICE


def test_awaits_a_coroutine_factory() -> None:
    """An AsyncIO deployment's factory may itself be a coroutine function."""
    assert load_notification_service(f"{__name__}._async_factory") is _SERVICE


def test_an_unset_factory_is_reported_with_a_usable_message() -> None:
    with pytest.raises(ServiceConfigurationError, match="NOTIFICATION_SERVICE_FACTORY is not set"):
        load_notification_service("")


def test_an_unimportable_factory_is_reported() -> None:
    with pytest.raises(ServiceConfigurationError, match="Could not import"):
        load_notification_service("no.such.module.factory")


def test_a_factory_returning_nothing_is_reported() -> None:
    with pytest.raises(ServiceConfigurationError, match="did not return a service"):
        load_notification_service(f"{__name__}._not_a_factory")


def test_a_factory_that_is_not_callable_is_reported() -> None:
    with pytest.raises(ServiceConfigurationError, match="is not callable"):
        load_notification_service(f"{__name__}._NOT_CALLABLE")


# --- the service cache ---------------------------------------------------------------


def test_the_service_is_built_once_and_reused(settings: Any) -> None:
    from ..service import get_service_caller

    settings.NOTIFICATION_SERVICE_FACTORY = f"{__name__}._factory"
    settings.VINTASEND_BACKEND_IDENTIFIER = None
    set_service_caller(None)

    first = get_service_caller()
    second = get_service_caller()

    assert first is second
    assert isinstance(first, ServiceCaller)
    assert first.service is _SERVICE


def test_a_failure_is_not_cached(settings: Any) -> None:
    """A transient misconfiguration should be retried on the next request rather than
    poisoning the process until it restarts."""
    from ..service import get_service_caller

    settings.NOTIFICATION_SERVICE_FACTORY = "no.such.module.factory"
    settings.VINTASEND_BACKEND_IDENTIFIER = None
    set_service_caller(None)

    with pytest.raises(ServiceConfigurationError):
        get_service_caller()

    settings.NOTIFICATION_SERVICE_FACTORY = f"{__name__}._factory"

    assert get_service_caller().service is _SERVICE
