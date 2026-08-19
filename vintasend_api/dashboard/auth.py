"""Shared-secret authentication.

Every ``/api/v1`` request must carry ``Authorization: Bearer <VINTASEND_API_KEY>``. The
dashboard calls the API from its server side only, so the key never reaches a browser.
"""

import hmac

from django.conf import settings
from django.http import HttpRequest
from ninja.security import HttpBearer

from .errors import ApiError


class ApiKeyAuth(HttpBearer):
    """Compares the presented bearer token against the configured key.

    ``hmac.compare_digest`` keeps the comparison time-independent of how many leading
    characters match, so the key cannot be recovered a byte at a time.

    The key is read per request rather than captured at import, so a deployment that
    reloads settings -- and, more practically, a test using ``override_settings`` --
    sees the change.
    """

    def authenticate(self, request: HttpRequest, token: str) -> str | None:
        expected = settings.VINTASEND_API_KEY

        if not expected or not token:
            raise ApiError("UNAUTHORIZED", "A valid API key is required.")

        if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            raise ApiError("UNAUTHORIZED", "A valid API key is required.")

        return token
