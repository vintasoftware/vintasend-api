"""Authentication and the error envelope on unmatched routes.

Mirrors the TypeScript reference's ``test/auth.test.ts``.
"""

from typing import Any, Callable

from django.test import Client

import pytest

from .fixtures import AUTH_HEADERS


pytestmark = pytest.mark.django_db


def test_health_is_open(client: Client) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "apiVersion": "v1"}


def test_rejects_requests_without_an_api_key(
    client: Client, install_service: Callable[..., Any]
) -> None:
    install_service()

    response = client.get("/api/v1/notifications")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_rejects_requests_with_the_wrong_api_key(
    client: Client, install_service: Callable[..., Any]
) -> None:
    install_service()

    response = client.get("/api/v1/notifications", headers={"Authorization": "Bearer nope"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_rejects_a_non_bearer_authorization_header(
    client: Client, install_service: Callable[..., Any]
) -> None:
    install_service()

    response = client.get("/api/v1/notifications", headers={"Authorization": "test-api-key"})

    assert response.status_code == 401


def test_accepts_requests_with_the_configured_api_key(
    client: Client, install_service: Callable[..., Any]
) -> None:
    install_service()

    response = client.get("/api/v1/notifications", headers=AUTH_HEADERS)

    assert response.status_code == 200


def test_returns_the_error_envelope_for_unknown_api_routes(
    client: Client, install_service: Callable[..., Any]
) -> None:
    install_service()

    response = client.get("/api/v1/nope", headers=AUTH_HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_returns_the_error_envelope_for_unknown_routes_outside_the_api(client: Client) -> None:
    response = client.get("/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_an_unset_api_key_rejects_every_request(
    client: Client, settings: Any, install_service: Callable[..., Any]
) -> None:
    """A deployment that never set the key must not accidentally accept a blank token.

    The startup check refuses to boot without the key, so this only happens if the check
    is bypassed -- but "no key configured" must never mean "no key required".
    """
    install_service()
    settings.VINTASEND_API_KEY = ""

    assert (
        client.get("/api/v1/notifications", headers={"Authorization": "Bearer "}).status_code == 401
    )
    assert client.get("/api/v1/notifications", headers=AUTH_HEADERS).status_code == 401
