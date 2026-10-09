"""The shared contract: what this server declares, and what ``openapi.yaml`` says it should.

``openapi.yaml`` is the TypeScript repository's, byte-identical, so it is the authority here
and the routes are checked against it rather than the other way around.
"""

from typing import get_args

from ..contract import ApiErrorCode
from .contract import declared_statuses, documented_statuses, undeclared_status


def test_every_api_route_declares_what_the_contract_documents() -> None:
    """A status declared here but not in the document, or the reverse, is drift."""
    documented = {
        key: statuses for key, statuses in documented_statuses().items() if key[1] != "/health"
    }

    assert declared_statuses() == documented


def test_every_api_route_declares_forbidden() -> None:
    """A host that authenticates the caller and then refuses it answers 403, not 401."""
    for (method, path), statuses in declared_statuses().items():
        assert "403" in statuses, f"{method} {path} declares no 403"


def test_forbidden_is_a_code_this_server_can_answer() -> None:
    assert "FORBIDDEN" in get_args(ApiErrorCode)


def test_an_undeclared_status_is_reported() -> None:
    """The test client runs this on every response, so it is only worth having if it fails."""
    route = "api/v1/notifications/<id>/cancel"

    assert undeclared_status("POST", route, 409) is None
    assert undeclared_status("POST", route, 400) is not None
    assert undeclared_status("POST", route, 500) is None
    assert undeclared_status("GET", "api/v1/nowhere", 404) is None
