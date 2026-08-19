"""Fetches template files from GitHub at a specific commit.

A preview always shows a template as it was when the notification was created, so
lookups are pinned to the commit SHA persisted with the notification rather than to a
branch. Results are cached per (repo, path, sha); that key is immutable, so entries
never go stale.
"""

import base64
import re
import threading
from collections import OrderedDict
from typing import Protocol
from urllib.parse import quote, urlparse

from django.conf import settings

import requests


DEFAULT_GITHUB_API_BASE_URL = "https://api.github.com"

_SSH_REPO_PATTERN = re.compile(r"^git@[^:]+:([^/\s]+)/([^/\s]+)$")
_DIRECT_REPO_PATTERN = re.compile(r"^([^/\s]+)/([^/\s]+)$")


class TemplateSourceClient(Protocol):
    """What a preview needs from a template source.

    ``GitHubTemplateClient`` is the one implementation that ships; the tests substitute
    their own, and a deployment reading templates from somewhere else can too.
    """

    def get_template_content_by_commit(self, template_path: str, git_commit_sha: str) -> str: ...

    def get_latest_main_commit_sha(self) -> str: ...


class TemplateSourceError(RuntimeError):
    """The template source could not be reached, or answered with something unusable.

    The route layer turns this into the contract's 502 ``UPSTREAM_ERROR``.
    """


def normalize_repo(repo: str) -> str:
    """Accept ``owner/repo``, an HTTPS URL or an SSH remote, and return ``owner/repo``."""
    trimmed = re.sub(r"\.git$", "", repo.strip())

    if re.match(r"^https?://", trimmed, re.IGNORECASE):
        parsed = urlparse(trimmed)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) < 2:
            raise TemplateSourceError("GITHUB_REPO URL must include owner and repository name.")
        return f"{segments[0]}/{re.sub(r'.git$', '', segments[1])}"

    ssh_match = _SSH_REPO_PATTERN.match(trimmed)
    if ssh_match:
        return f"{ssh_match.group(1)}/{re.sub(r'.git$', '', ssh_match.group(2))}"

    direct_match = _DIRECT_REPO_PATTERN.match(trimmed)
    if direct_match:
        return f"{direct_match.group(1)}/{direct_match.group(2)}"

    raise TemplateSourceError(
        "GITHUB_REPO is required in owner/repo format or as a GitHub repository URL."
    )


def _trim_path_edges(path: str) -> str:
    return path.strip("/")


def resolve_template_path(template_path: str, templates_base_path: str = "") -> str:
    """Prefix a notification's template path with the configured base path."""
    cleaned_template_path = _trim_path_edges(template_path.strip())
    if not cleaned_template_path:
        raise TemplateSourceError("Template path is required for preview rendering.")

    cleaned_base_path = _trim_path_edges(templates_base_path.strip())
    if not cleaned_base_path:
        return cleaned_template_path

    return f"{cleaned_base_path}/{cleaned_template_path}"


class GitHubTemplateClient:
    """Reads template files out of a GitHub repository at a given commit."""

    def __init__(
        self,
        repo: str,
        api_key: str,
        api_base_url: str = DEFAULT_GITHUB_API_BASE_URL,
        templates_base_path: str = "",
        cache_max_entries: int = 100,
        timeout_seconds: int = 10,
        session: requests.Session | None = None,
    ) -> None:
        self.repo = normalize_repo(repo)
        self.api_key = api_key
        self.api_base_url = api_base_url.rstrip("/")
        self.templates_base_path = templates_base_path
        self.cache_max_entries = cache_max_entries
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_lock = threading.Lock()

    # --- public API ------------------------------------------------------------------

    def get_template_content_by_commit(self, template_path: str, git_commit_sha: str) -> str:
        resolved_path = resolve_template_path(template_path, self.templates_base_path)
        cache_key = f"{self.repo}:{resolved_path}:{git_commit_sha}"

        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        response = self._get(self._contents_url(resolved_path, git_commit_sha))
        if not response.ok:
            raise self._content_error(response)

        payload = self._json(response)
        content = payload.get("content")
        if not content or payload.get("encoding") != "base64":
            raise TemplateSourceError("GitHub template response is invalid or unsupported.")

        try:
            decoded = base64.b64decode(content.replace("\n", "")).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise TemplateSourceError("GitHub template content could not be decoded.") from error

        self._cache_set(cache_key, decoded)
        return decoded

    def get_latest_main_commit_sha(self) -> str:
        response = self._get(f"{self.api_base_url}/repos/{self.repo}/commits/main")
        if not response.ok:
            raise self._commit_error(response)

        sha = self._json(response).get("sha")
        if not sha:
            raise TemplateSourceError(
                "GitHub main branch commit lookup returned an invalid response."
            )
        return str(sha)

    # --- internals -------------------------------------------------------------------

    def _contents_url(self, path: str, git_commit_sha: str) -> str:
        encoded_path = "/".join(quote(segment, safe="") for segment in path.split("/"))
        return (
            f"{self.api_base_url}/repos/{self.repo}/contents/{encoded_path}"
            f"?ref={quote(git_commit_sha, safe='')}"
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.api_key}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, url: str) -> requests.Response:
        try:
            return self.session.get(url, headers=self._headers(), timeout=self.timeout_seconds)
        except requests.RequestException as error:
            raise TemplateSourceError(f"GitHub request failed: {error}") from error

    @staticmethod
    def _json(response: requests.Response) -> dict:
        try:
            payload = response.json()
        except ValueError as error:
            raise TemplateSourceError("GitHub returned a response that is not JSON.") from error
        if not isinstance(payload, dict):
            raise TemplateSourceError("GitHub returned an unexpected response shape.")
        return payload

    def _cache_get(self, key: str) -> str | None:
        with self._cache_lock:
            if key not in self._cache:
                return None
            self._cache.move_to_end(key)
            return self._cache[key]

    def _cache_set(self, key: str, value: str) -> None:
        with self._cache_lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_max_entries:
                self._cache.popitem(last=False)

    def _rate_limited(self, response: requests.Response) -> bool:
        if response.status_code == 429:
            return True
        return response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"

    def _suffix(self, response: requests.Response) -> str:
        try:
            message = response.json().get("message")
        except (ValueError, AttributeError):
            return ""
        return f": {message}" if message else ""

    def _content_error(self, response: requests.Response) -> TemplateSourceError:
        if response.status_code == 404:
            return TemplateSourceError(
                "Template file was not found in GitHub for the requested commit."
            )
        if self._rate_limited(response):
            return TemplateSourceError(
                "GitHub API rate limit exceeded while fetching template preview."
            )
        if response.status_code == 403:
            return TemplateSourceError(
                "GitHub API request was forbidden. Check repository access and token permissions."
            )
        return TemplateSourceError(
            f"GitHub template fetch failed with status {response.status_code}"
            f"{self._suffix(response)}"
        )

    def _commit_error(self, response: requests.Response) -> TemplateSourceError:
        if response.status_code == 404:
            return TemplateSourceError("Unable to resolve latest commit SHA from the main branch.")
        if self._rate_limited(response):
            return TemplateSourceError(
                "GitHub API rate limit exceeded while fetching template preview."
            )
        if response.status_code == 403:
            return TemplateSourceError(
                "GitHub API request was forbidden. Check repository access and token permissions."
            )
        return TemplateSourceError(
            f"GitHub main branch commit lookup failed with status {response.status_code}"
            f"{self._suffix(response)}"
        )


def create_github_template_client_from_settings() -> GitHubTemplateClient:
    """Build the client from Django settings.

    The ``GITHUB_*`` settings are only read here, which is only reached from
    ``/preview``, so an API that does not use template previews runs fine without them.
    """
    if not settings.GITHUB_REPO:
        raise TemplateSourceError("GITHUB_REPO is required (owner/repo).")
    if not settings.GITHUB_API_KEY:
        raise TemplateSourceError("GITHUB_API_KEY is required for template preview.")

    return GitHubTemplateClient(
        repo=settings.GITHUB_REPO,
        api_key=settings.GITHUB_API_KEY,
        api_base_url=settings.GITHUB_API_BASE_URL or DEFAULT_GITHUB_API_BASE_URL,
        templates_base_path=settings.GITHUB_TEMPLATES_BASE_PATH,
        cache_max_entries=settings.GITHUB_TEMPLATE_CACHE_MAX_ENTRIES,
        timeout_seconds=settings.GITHUB_TEMPLATE_TIMEOUT_SECONDS,
    )


_client_lock = threading.Lock()
_cached_client: TemplateSourceClient | None = None


def get_template_client() -> TemplateSourceClient:
    """Return the process-wide template client, building it on first use.

    Held across requests so its template cache is worth having. Configuration failures
    are not cached, so fixing ``GITHUB_REPO`` does not need a restart.
    """
    global _cached_client  # noqa: PLW0603

    if _cached_client is not None:
        return _cached_client

    with _client_lock:
        if _cached_client is None:
            _cached_client = create_github_template_client_from_settings()
        return _cached_client


def set_template_client(client: TemplateSourceClient | None) -> None:
    """Replace the cached client. The seam tests inject a fake template source through."""
    global _cached_client  # noqa: PLW0603

    with _client_lock:
        _cached_client = client
