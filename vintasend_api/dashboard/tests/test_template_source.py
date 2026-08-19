"""The GitHub template client.

Mirrors the TypeScript reference's ``test/github-template-client.test.ts``. No network:
a stub session records the requests and returns canned responses.
"""

import base64
from typing import Any

import pytest
import requests

from ..template_source import (
    GitHubTemplateClient,
    TemplateSourceError,
    normalize_repo,
    resolve_template_path,
)


class StubResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: dict | None = None,
        headers: dict | None = None,
        raise_on_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self._raise_on_json = raise_on_json

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        if self._raise_on_json:
            raise ValueError("not json")
        return self._payload


class StubSession:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url: str, headers: Any = None, timeout: Any = None) -> Any:
        self.requests.append((url, headers or {}))
        response = self.responses.pop(0) if self.responses else StubResponse()
        if isinstance(response, Exception):
            raise response
        return response


def encoded(content: str) -> dict:
    return {
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "encoding": "base64",
    }


def make_client(session: StubSession, **kwargs: Any) -> GitHubTemplateClient:
    defaults: dict[str, Any] = {
        "repo": "vintasoftware/templates",
        "api_key": "gh-token",
        "session": session,
    }
    defaults.update(kwargs)
    return GitHubTemplateClient(**defaults)


# --- repo normalization --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("vintasoftware/templates", "vintasoftware/templates"),
        ("  vintasoftware/templates  ", "vintasoftware/templates"),
        ("https://github.com/vintasoftware/templates", "vintasoftware/templates"),
        ("https://github.com/vintasoftware/templates.git", "vintasoftware/templates"),
        ("git@github.com:vintasoftware/templates.git", "vintasoftware/templates"),
    ],
)
def test_normalize_repo_accepts_every_documented_form(raw: str, expected: str) -> None:
    assert normalize_repo(raw) == expected


@pytest.mark.parametrize("raw", ["", "not-a-repo", "https://github.com/onlyowner"])
def test_normalize_repo_rejects_what_it_cannot_read(raw: str) -> None:
    with pytest.raises(TemplateSourceError):
        normalize_repo(raw)


# --- path resolution -----------------------------------------------------------------


def test_resolve_template_path_without_a_base_path() -> None:
    assert resolve_template_path("/emails/body.html/") == "emails/body.html"


def test_resolve_template_path_joins_the_base_path() -> None:
    assert resolve_template_path("emails/body.html", "/src/templates/") == (
        "src/templates/emails/body.html"
    )


def test_resolve_template_path_requires_a_path() -> None:
    with pytest.raises(TemplateSourceError):
        resolve_template_path("   ")


# --- fetching ------------------------------------------------------------------------


def test_fetches_and_decodes_a_template_at_a_commit() -> None:
    session = StubSession(StubResponse(payload=encoded("h1 Hello")))
    client = make_client(session)

    assert client.get_template_content_by_commit("emails/body.html", "abc123") == "h1 Hello"

    url, headers = session.requests[0]
    assert url == (
        "https://api.github.com/repos/vintasoftware/templates/contents/emails/body.html?ref=abc123"
    )
    assert headers["Authorization"] == "Bearer gh-token"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_applies_the_configured_templates_base_path() -> None:
    session = StubSession(StubResponse(payload=encoded("body")))
    client = make_client(session, templates_base_path="src/templates")

    client.get_template_content_by_commit("emails/body.html", "abc123")

    assert "contents/src/templates/emails/body.html" in session.requests[0][0]


def test_caches_by_repo_path_and_commit() -> None:
    session = StubSession(StubResponse(payload=encoded("cached")))
    client = make_client(session)

    first = client.get_template_content_by_commit("emails/body.html", "abc123")
    second = client.get_template_content_by_commit("emails/body.html", "abc123")

    assert first == second == "cached"
    assert len(session.requests) == 1


def test_a_different_commit_is_a_different_cache_entry() -> None:
    session = StubSession(
        StubResponse(payload=encoded("old")), StubResponse(payload=encoded("new"))
    )
    client = make_client(session)

    assert client.get_template_content_by_commit("emails/body.html", "sha-1") == "old"
    assert client.get_template_content_by_commit("emails/body.html", "sha-2") == "new"


def test_the_cache_evicts_least_recently_used_entries() -> None:
    session = StubSession(*[StubResponse(payload=encoded(f"v{i}")) for i in range(4)])
    client = make_client(session, cache_max_entries=2)

    client.get_template_content_by_commit("a.html", "sha")
    client.get_template_content_by_commit("b.html", "sha")
    client.get_template_content_by_commit("c.html", "sha")
    # "a" was evicted when "c" arrived, so this refetches rather than hitting the cache.
    client.get_template_content_by_commit("a.html", "sha")

    assert len(session.requests) == 4


def test_reports_a_missing_template() -> None:
    session = StubSession(StubResponse(status_code=404))
    client = make_client(session)

    with pytest.raises(TemplateSourceError, match="not found in GitHub"):
        client.get_template_content_by_commit("emails/body.html", "abc123")


def test_reports_an_exhausted_rate_limit() -> None:
    session = StubSession(StubResponse(status_code=403, headers={"x-ratelimit-remaining": "0"}))
    client = make_client(session)

    with pytest.raises(TemplateSourceError, match="rate limit exceeded"):
        client.get_template_content_by_commit("emails/body.html", "abc123")


def test_reports_a_forbidden_request_that_is_not_rate_limiting() -> None:
    session = StubSession(StubResponse(status_code=403, headers={"x-ratelimit-remaining": "42"}))
    client = make_client(session)

    with pytest.raises(TemplateSourceError, match="forbidden"):
        client.get_template_content_by_commit("emails/body.html", "abc123")


def test_reports_a_429_as_rate_limiting() -> None:
    session = StubSession(StubResponse(status_code=429))
    client = make_client(session)

    with pytest.raises(TemplateSourceError, match="rate limit exceeded"):
        client.get_template_content_by_commit("emails/body.html", "abc123")


def test_includes_githubs_message_on_an_unexpected_status() -> None:
    session = StubSession(StubResponse(status_code=500, payload={"message": "boom"}))
    client = make_client(session)

    with pytest.raises(TemplateSourceError, match="status 500: boom"):
        client.get_template_content_by_commit("emails/body.html", "abc123")


def test_rejects_an_unsupported_encoding() -> None:
    session = StubSession(StubResponse(payload={"content": "abc", "encoding": "utf-8"}))
    client = make_client(session)

    with pytest.raises(TemplateSourceError, match="invalid or unsupported"):
        client.get_template_content_by_commit("emails/body.html", "abc123")


def test_turns_a_transport_failure_into_a_template_source_error() -> None:
    session = StubSession(requests.ConnectionError("no route to host"))
    client = make_client(session)

    with pytest.raises(TemplateSourceError, match="GitHub request failed"):
        client.get_template_content_by_commit("emails/body.html", "abc123")


# --- main commit lookup --------------------------------------------------------------


def test_reads_the_latest_main_commit_sha() -> None:
    session = StubSession(StubResponse(payload={"sha": "deadbeef"}))
    client = make_client(session)

    assert client.get_latest_main_commit_sha() == "deadbeef"
    assert session.requests[0][0].endswith("/repos/vintasoftware/templates/commits/main")


def test_reports_a_main_commit_lookup_that_404s() -> None:
    session = StubSession(StubResponse(status_code=404))
    client = make_client(session)

    with pytest.raises(TemplateSourceError, match="Unable to resolve latest commit SHA"):
        client.get_latest_main_commit_sha()


def test_reports_a_main_commit_lookup_without_a_sha() -> None:
    session = StubSession(StubResponse(payload={}))
    client = make_client(session)

    with pytest.raises(TemplateSourceError, match="invalid response"):
        client.get_latest_main_commit_sha()
