"""The API's error type and its mapping onto the contract's status codes.

Rendering an ``ApiError`` into the response envelope is deliberately not done here --
``api.py`` owns that, so there is exactly one place that decides what an error looks like
on the wire, and validation and auth failures go through the same code as raised errors.
"""

from typing import Any

from .contract import ApiErrorCode


# Two codes deliberately share a status: PREVIEW_UNAVAILABLE is a 409 that says
# specifically *why* a preview cannot be produced, which the dashboard branches on.
STATUS_BY_CODE: dict[str, int] = {
    "BAD_REQUEST": 400,
    "UNAUTHORIZED": 401,
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
    def not_found(cls, message: str) -> "ApiError":
        return cls("NOT_FOUND", message)

    @classmethod
    def conflict(cls, message: str) -> "ApiError":
        return cls("CONFLICT", message)

    @classmethod
    def upstream(cls, message: str) -> "ApiError":
        return cls("UPSTREAM_ERROR", message)
