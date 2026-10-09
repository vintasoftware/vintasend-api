"""What ``openapi.yaml`` and the Python routes each say an operation can answer.

``openapi.yaml`` is the TypeScript repository's, shipped here byte-identical, and nothing in
this project generates it. The routes in ``api.py`` declare their error responses by hand. So
two things can drift: a route answering a status it does not declare, and a declaration that
no longer matches the shared document. The test client checks the first on every response;
``test_contract.py`` checks the second.

The YAML is read with a small scan rather than a YAML library, which this project does not
otherwise depend on: paths at two spaces, methods at four, their ``responses:`` at six and
quoted status codes at eight.
"""

import re
from functools import cache
from pathlib import Path
from typing import Any

from ..api import api
from ..contract import API_BASE_PATH


HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

# The repository root. The tests only run from a checkout: the package ships without them.
OPENAPI_PATH = Path(__file__).resolve().parents[3] / "openapi.yaml"


def documented_statuses() -> dict[tuple[str, str], frozenset[str]]:
    """(METHOD, path) -> the status codes ``openapi.yaml`` declares for that operation."""
    operations: dict[tuple[str, str], set[str]] = {}
    in_paths = False
    path: str | None = None
    current: set[str] | None = None
    in_responses = False

    for line in OPENAPI_PATH.read_text().splitlines():
        if line == "paths:":
            in_paths = True
            continue
        if not in_paths:
            continue
        if re.match(r"\S", line):
            break
        if match := re.fullmatch(r" {2}(/\S*):\s*", line):
            path, current = match.group(1), None
            continue
        if (match := re.fullmatch(r" {4}([a-z]+):\s*", line)) and path is not None:
            current = None
            if match.group(1) in HTTP_METHODS:
                current = operations.setdefault((match.group(1).upper(), path), set())
            continue
        if re.match(r" {6}\S", line):
            in_responses = line.strip() == "responses:"
            continue
        if (
            (match := re.fullmatch(r" {8}'(\d{3})':\s*", line))
            and in_responses
            and current is not None
        ):
            current.add(match.group(1))

    return {key: frozenset(codes) for key, codes in operations.items()}


@cache
def declared_statuses() -> dict[tuple[str, str], frozenset[str]]:
    """(METHOD, path) -> the status codes the Python routes declare for that operation.

    Built against the contract's own prefix rather than wherever the URLconf mounts the API:
    left to Ninja, the prefix comes from ``reverse()``, so a first call made while a test has
    the API mounted under a host's prefix would cache that prefix for every test after it.
    """
    paths: dict[str, dict[str, Any]] = api.get_openapi_schema(path_prefix=f"{API_BASE_PATH}/")[
        "paths"
    ]
    return {
        (method.upper(), path): frozenset(str(code) for code in operation["responses"])
        for path, methods in paths.items()
        for method, operation in methods.items()
    }


def undeclared_status(method: str, route: str, status: int) -> str | None:
    """Why ``status`` breaks the contract for the route Django matched, or None if it does not.

    Only client errors are checked. A 500 is the generic answer to anything unexpected and is
    documented once, not per route, and a route no operation names is not the contract's.
    """
    if not 400 <= status < 500:
        return None
    path = "/" + re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", route)
    # Under a host's prefix it is the same operation: the contract's paths start at /api/v1.
    mounted_at = path.find(f"{API_BASE_PATH}/")
    if mounted_at > 0:
        path = path[mounted_at:]
    declared = declared_statuses().get((method, path))
    if declared is None or str(status) in declared:
        return None
    return f"{method} {path} answered {status}, which it does not declare ({sorted(declared)})"
