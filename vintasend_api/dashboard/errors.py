"""The API's error type and its mapping onto the contract's status codes.

Rendering an ``ApiError`` into the response envelope is deliberately not done here --
``api.py`` owns that, so there is exactly one place that decides what an error looks like
on the wire, and validation and auth failures go through the same code as raised errors.
"""

from typing import Any

from .contract import ApiErrorCode


# Two codes deliberately share a status: PREVIEW_UNAVAILABLE is a 409 that says
# specifically *why* a preview cannot be produced, which the dashboard branches on.
#
# FORBIDDEN is what a host answers when it authenticated the caller and then refused it. A
# 401 there would tell a signed-in user to sign in again.
STATUS_BY_CODE: dict[str, int] = {
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401,
    "FORBIDDEN": 403,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "PREVIEW_UNAVAILABLE": 409,
    "UPSTREAM_ERROR": 502,
    "INTERNAL_ERROR": 500,
}


class ApiError(Exception):
    """Error carrying an API error code, turned into the documented status code and
    error envelope by the handler registered in ``api.py``."""

    def __init__(self, code: ApiErrorCode, message: str, details: Any | None = None) -> None:
        super().__init__(message)
        self.code: ApiErrorCode = code
        self.message = message
        self.status = STATUS_BY_CODE[code]
        self.details = details

    @classmethod
    def bad_request(
        cls, message: str, issues: list[Any] | None = None, **context: Any
    ) -> "ApiError":
        """A 400, which always carries ``details.issues``.

        Every invalid input answers in the same shape, so a client reads one list whatever
        it got wrong. A failure that is not about one field is a single issue with an empty
        path repeating the message. ``context`` adds keys next to ``issues``.
        """
        listed = issues if issues is not None else [issue("", message)]
        return cls("BAD_REQUEST", message, {**context, "issues": listed})

    @classmethod
    def unauthorized(cls, message: str) -> "ApiError":
        """A 401: no valid credential was presented."""
        return cls("UNAUTHORIZED", message)

    @classmethod
    def forbidden(cls, message: str) -> "ApiError":
        """A 403: the caller is known, and refused."""
        return cls("FORBIDDEN", message)

    @classmethod
    def not_found(cls, message: str) -> "ApiError":
        return cls("NOT_FOUND", message)

    @classmethod
    def conflict(cls, message: str) -> "ApiError":
        return cls("CONFLICT", message)

    @classmethod
    def upstream(cls, message: str) -> "ApiError":
        return cls("UPSTREAM_ERROR", message)


def issue(path: str, message: str) -> dict[str, str]:
    """One entry of a 400's ``details.issues``. ``path`` is dotted, and empty for the body."""
    return {"path": path, "message": message}


def invalid_request(issues: list[Any]) -> ApiError:
    """The 400 for input that failed validation, wherever in the request it was."""
    return ApiError.bad_request("Invalid request.", issues)
