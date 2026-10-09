"""Who may call ``/api/v1``.

Two ways, chosen by settings, both read per request so ``override_settings`` works:

``VINTASEND_API_AUTHENTICATOR`` -- ``(request) -> None``
    A host's own check, as a dotted path or as the callable itself. It refuses a caller by
    raising ``ApiError("UNAUTHORIZED", ...)`` (401, no valid credential) or
    ``ApiError("FORBIDDEN", ...)`` (403, a caller it knows and refuses); returning lets the
    request through. It may be ``async``. When it is set, the shared key is not checked and
    need not be configured. ``bearer_token`` reads the header for one built around a token
    the host verifies itself, such as its identity provider's. Mirrors the TypeScript
    reference's ``authenticate`` option.

``VINTASEND_API_KEY``
    The shared secret, when no authenticator is set: every request must carry
    ``Authorization: Bearer <VINTASEND_API_KEY>``. The dashboard calls the API from its
    server side only, so the key never reaches a browser.

An authenticator that cannot be resolved answers every request with a 500 rather than falling
back to the key: a typo in a setting must not quietly change who is let in. The system check
``vintasend_api.E004`` reports it before that.
"""

import hmac
import inspect
import re
from collections.abc import Awaitable, Callable
from typing import cast

from django.http import HttpRequest
from ninja.security.http import HttpAuthBase

from asgiref.sync import async_to_sync

from . import conf
from .errors import ApiError
from .hooks import _awaited, configured_hook


__all__ = [
    "ApiError",
    "ApiKeyAuth",
    "Authenticator",
    "as_refusal",
    "bearer_token",
    "check_api_key",
    "configured_authenticator",
]

Authenticator = Callable[[HttpRequest], "None | Awaitable[None]"]

# The scheme is matched in any case, as RFC 9110 has it. `\s+` rather than one space, and the
# token stripped, so the answer is the same as the TypeScript twin's `bearerToken`.
_BEARER = re.compile(r"Bearer\s+(.+)", re.IGNORECASE)

_KEY_REQUIRED = "A valid API key is required."


# What an authenticator answers when it refuses a caller.
_REFUSALS = ("UNAUTHORIZED", "FORBIDDEN")


def as_refusal(error: BaseException) -> ApiError | None:
    """The refusal an authenticator raised, as this package's ``ApiError``, or None.

    One authenticator can serve both VintaSend APIs mounted in one project, so it may raise
    the other package's ``ApiError``. That one is recognised by its class name and code, as
    the TypeScript packages do, rather than by its class, which this package cannot import.
    Only a 401 or a 403 is an answer an authenticator gives; any other code is a failure.
    """
    if isinstance(error, ApiError):
        return error
    code = getattr(error, "code", None)
    if type(error).__name__ != "ApiError" or code not in _REFUSALS:
        return None
    message = getattr(error, "message", None)
    return ApiError(code, message if isinstance(message, str) else str(error))


def bearer_token(request_or_header: HttpRequest | str | None) -> str | None:
    """The token in an ``Authorization: Bearer <token>`` header, or None when there is none.

    Takes the request or the header's value. A header with another scheme, or with an empty
    token, is None too, so a caller only has to check for one thing.
    """
    header = (
        request_or_header.headers.get("Authorization")
        if isinstance(request_or_header, HttpRequest)
        else request_or_header
    )
    if not header:
        return None
    match = _BEARER.fullmatch(header)
    token = match.group(1).strip() if match else ""
    return token or None


def configured_authenticator() -> Authenticator | None:
    """The callable ``VINTASEND_API_AUTHENTICATOR`` names, or None when it is unset.

    raises ImportError: if a dotted path cannot be imported.
    raises TypeError: if the setting names something that is not callable.
    """
    return cast("Authenticator | None", configured_hook(conf.AUTHENTICATOR))


def check_api_key(request: HttpRequest) -> None:
    """The shared-secret check, used when no authenticator is configured.

    ``hmac.compare_digest`` keeps the comparison time-independent of how many leading
    characters match, so the key cannot be recovered a byte at a time. An unset key matches
    nothing, so a deployment that never set one refuses every request.
    """
    expected = conf.api_key()
    token = bearer_token(request)

    if not expected or token is None:
        raise ApiError.unauthorized(_KEY_REQUIRED)

    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        raise ApiError.unauthorized(_KEY_REQUIRED)


class ApiKeyAuth(HttpAuthBase):
    """Ninja's auth hook for every ``/api/v1`` route.

    Declared as an HTTP bearer scheme, and still named ``ApiKeyAuth``, because Ninja
    documents the security scheme from both: the generated schema describes the same scheme
    whichever check a host chooses. Not an ``HttpBearer``: that refuses a request without a
    bearer header before a host authenticator could look at it, and under ``DEBUG`` it logs a
    header it cannot parse, credentials included.
    """

    openapi_scheme = "bearer"

    def __call__(self, request: HttpRequest) -> bool:
        authenticator = configured_authenticator()
        if authenticator is None:
            check_api_key(request)
        else:
            try:
                outcome = authenticator(request)
                # An unawaited coroutine would let every caller through.
                if inspect.isawaitable(outcome):
                    async_to_sync(_awaited)(outcome)
            except Exception as error:
                refusal = as_refusal(error)
                if refusal is None or refusal is error:
                    raise
                raise refusal from error
        # Ninja treats a falsy answer as "not authenticated", so success is spelled out.
        return True
