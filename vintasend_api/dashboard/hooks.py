"""What happens to an unexpected error.

``VINTASEND_UNHANDLED_ERROR_HANDLER`` names a callable ``(exc, request, request_id) -> None``,
as a dotted path (what an environment variable can carry) or as the callable itself (what a
``settings.py`` can assign). It receives every error the API does not map to a contract
error, before the generic 500 is sent. It may be ``async``. It gets the exception itself, so
keeping health data out of wherever it sends it is the host's responsibility. When it is not
set, or when it raises, ``log_unhandled_error`` writes one redacted line instead.

Unexpected errors are not logged whole by default because an error from a notification
backend, a provider or a context generator can quote notification content, recipients or
context values, and the applications this API serves handle health data.

``configured_hook`` resolves this setting and ``VINTASEND_API_AUTHENTICATOR`` too (see
``auth.py``), so both fail the system checks the same way when they cannot be used.
"""

import inspect
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from typing import cast

from django.http import HttpRequest
from django.utils.module_loading import import_string

from asgiref.sync import async_to_sync

from . import conf


logger = logging.getLogger(__name__)

UnhandledErrorHandler = Callable[[Exception, HttpRequest, str], "None | Awaitable[None]"]

REQUEST_ID_HEADER = "X-Request-Id"

HANDLER_SETTING = conf.UNHANDLED_ERROR_HANDLER

# Only an id that cannot break a log line out of its field is taken from the client. Matched
# with ``fullmatch``: ``$`` would also accept a trailing newline.
_SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


def configured_hook(setting_name: str) -> Callable[..., object] | None:
    """The callable a hook setting names, or None when it is unset.

    The setting holds a dotted path or the callable itself.

    raises ImportError: if a dotted path cannot be imported.
    raises TypeError: if the setting names something that is not callable.
    """
    value = conf.hook_setting(setting_name)
    if not value:
        return None
    hook = import_string(value) if isinstance(value, str) else value
    if not callable(hook):
        raise TypeError(f"{setting_name} must name a callable, not {type(hook).__name__}.")
    return cast(Callable[..., object], hook)


def configured_handler() -> Callable[..., object] | None:
    """The callable ``VINTASEND_UNHANDLED_ERROR_HANDLER`` names, or None when it is unset.

    raises ImportError: if a dotted path cannot be imported.
    raises TypeError: if the setting names something that is not callable.
    """
    return configured_hook(HANDLER_SETTING)


def request_id_for(request: HttpRequest) -> str:
    """The caller's ``X-Request-Id`` when it is safe to log, otherwise a fresh UUID."""
    supplied = request.META.get("HTTP_X_REQUEST_ID", "")
    if isinstance(supplied, str) and _SAFE_REQUEST_ID.fullmatch(supplied):
        return supplied
    return str(uuid.uuid4())


def route_pattern(request: HttpRequest) -> str:
    """The URL pattern the request matched, with no path values filled in."""
    match = request.resolver_match
    return match.route if match is not None and match.route else "<unresolved route>"


def log_unhandled_error(exc: Exception, request: HttpRequest, request_id: str) -> None:
    """The default handler: one line, with no message, traceback, body or path values.

    The error's class name, the request id the 500 carries in its ``X-Request-Id`` header, the
    method and the matched route pattern -- enough to find the request, and nothing that came
    from the notification store, a provider or the caller.
    """
    logger.error(
        "Unhandled %s (request %s) on %s %s",
        type(exc).__name__,
        request_id,
        request.method,
        route_pattern(request),
    )


async def _awaited(awaitable: Awaitable[object]) -> object:
    return await awaitable


def report_unhandled_error(exc: Exception, request: HttpRequest, request_id: str) -> None:
    """Hand ``exc`` to the configured handler, or to ``log_unhandled_error``.

    A handler that raises, or a handler setting that cannot be resolved, falls back to the
    default line, so the error is still recorded somewhere. What a failing handler raised is
    never logged: it is no safer than the error it was handling, and it must not turn a 500
    into a crash.
    """
    try:
        handler = configured_handler()
    except Exception:
        handler = None
    if handler is not None:
        try:
            outcome = handler(exc, request, request_id)
            if inspect.isawaitable(outcome):
                async_to_sync(_awaited)(outcome)
            return
        except Exception:  # noqa: S110 - its own error is not logged, see the docstring
            pass
    try:
        log_unhandled_error(exc, request, request_id)
    except Exception:  # noqa: S110 - a broken logging setup must not turn a 500 into a crash
        pass
