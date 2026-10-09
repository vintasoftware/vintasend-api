"""How a JSON route reads its request body.

Every request body in the contract is ``application/json``. The rule for anything else:

* A request that declares a JSON media type (``application/json`` or
  ``application/*+json``) must carry valid JSON. An empty body there is malformed.
* A request that declares no media type, or another one, is read as ``{}`` when its body
  is empty. That is what lets an all-optional body be omitted. A non-empty body like that
  is refused rather than guessed at.

Refusing beats guessing either way. Reading such a body as JSON anyway (Ninja's default)
accepts a request the contract does not describe. Reading it as ``{}`` is worse:
``curl -d '{"useStoredContext": true}'`` -- form-encoded unless told otherwise -- would
resend the notification with a context regenerated from current data instead of the one
it was sent with. The templates management API applies the same rule.

Every refusal is a 400 ``BAD_REQUEST`` with ``details.issues`` at the empty path, the same
envelope as any other invalid input.
"""

import json
import re
from typing import Any

from django.http import HttpRequest
from ninja.parser import Parser

from .errors import invalid_request, issue


# The media types read as JSON: the same pattern Hono's validator uses, so both servers draw
# the line in the same place.
JSON_MEDIA_TYPE = re.compile(r"application/([a-z-.]+\+)?json(;\s*[a-zA-Z0-9-]+=([^;]+))*", re.I)

MALFORMED_JSON = "Malformed JSON in request body"
NOT_JSON = "Send the request body as application/json."
NOT_AN_OBJECT = "The request body must be a JSON object."


def declares_json(request: HttpRequest) -> bool:
    """Whether the request's ``Content-Type`` is one this API reads as JSON."""
    return JSON_MEDIA_TYPE.fullmatch(request.META.get("CONTENT_TYPE", "")) is not None


class JsonBodyParser(Parser):
    """Reads a non-empty body under the rule above.

    Ninja only calls this for a non-empty body, and wraps whatever it raises in an
    ``HttpError`` with this error as the cause. ``api.handle_http_error`` unwraps it.
    """

    def parse_body(self, request: HttpRequest) -> dict[str, Any]:
        if not declares_json(request):
            raise invalid_request([issue("", NOT_JSON)])
        try:
            data = json.loads(request.body)
        except ValueError as error:
            raise invalid_request([issue("", MALFORMED_JSON)]) from error
        if not isinstance(data, dict):
            raise invalid_request([issue("", NOT_AN_OBJECT)])
        return data


def refuse_an_empty_json_body(request: HttpRequest) -> None:
    """Refuse an empty body that declared itself JSON.

    The parser never sees an empty body: Ninja reads one as absent, so a route whose body
    is optional would run on its defaults. Those routes call this first.

    raises ApiError: 400 if the body is empty and the request declares a JSON media type.
    """
    if not request.body and declares_json(request):
        raise invalid_request([issue("", MALFORMED_JSON)])
