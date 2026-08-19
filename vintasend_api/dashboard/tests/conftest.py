"""Shared test wiring.

Tests drive the real Django application through the test client with an injected fake
service, so they cover routing, auth, validation, filter negotiation and serialization
without needing a database, a mail provider or GitHub.
"""

from typing import Any, Callable, Iterator

from django.test import Client

import pytest

from ..service import ServiceCaller, set_service_caller
from ..template_source import set_template_client
from .fixtures import AUTH_HEADERS, FakeService, FakeTemplateClient


@pytest.fixture(autouse=True)
def api_key(settings: Any) -> None:
    settings.VINTASEND_API_KEY = "test-api-key"


@pytest.fixture(autouse=True)
def reset_injected_singletons() -> Iterator[None]:
    """Clear the process-wide service and template client around every test.

    Both are cached for the life of the process in production, which is exactly what a
    test must not inherit from the test before it.
    """
    set_service_caller(None)
    set_template_client(None)
    yield
    set_service_caller(None)
    set_template_client(None)


@pytest.fixture
def install_service() -> Callable[..., FakeService]:
    """Install a fake service and return it, so a test can assert on what it received."""

    def _install(
        service: FakeService | None = None, backend_identifier: str | None = None
    ) -> FakeService:
        service = service or FakeService()
        set_service_caller(ServiceCaller(service, backend_identifier))
        return service

    return _install


@pytest.fixture
def install_template_client() -> Callable[..., FakeTemplateClient]:
    def _install(client: FakeTemplateClient | None = None) -> FakeTemplateClient:
        client = client or FakeTemplateClient()
        set_template_client(client)
        return client

    return _install


@pytest.fixture
def client() -> Client:
    return Client()


@pytest.fixture
def get(client: Client) -> Callable[..., Any]:
    def _get(path: str, **kwargs: Any) -> Any:
        return client.get(path, headers=AUTH_HEADERS, **kwargs)

    return _get


@pytest.fixture
def post(client: Client) -> Callable[..., Any]:
    def _post(path: str, body: Any = None, **kwargs: Any) -> Any:
        return client.post(
            path,
            data=body if body is not None else {},
            content_type="application/json",
            headers=AUTH_HEADERS,
            **kwargs,
        )

    return _post
