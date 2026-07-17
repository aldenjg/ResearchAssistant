"""Vendor-neutral synchronous search provider contracts and live adapters.

The Protocol and typed artifacts are the Phase 7B contracts. The live
adapters (Brave Search, Serper) are stdlib-only (`urllib`) implementations
added for post-MVP live web runs; API keys are read from the environment at
call time and never stored in the repository.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol, runtime_checkable

from dotenv import load_dotenv
from pydantic import Field

from models import StrictModel

load_dotenv()

_MAX_RESPONSE_BYTES = 2_000_000


class SearchProviderError(RuntimeError):
    """Raised when a search provider cannot return a usable result set."""


class SearchTimeoutError(SearchProviderError):
    """Raised when a search provider exceeds its configured timeout."""


class SearchRequest(StrictModel):
    query_text: str = Field(min_length=1)
    limit: int = Field(ge=1)


class SearchResult(StrictModel):
    original_url: str = Field(min_length=1)
    title: str = ""


class SearchResponse(StrictModel):
    results: list[SearchResult]


@runtime_checkable
class SearchProvider(Protocol):
    """A vendor-isolated, synchronous search provider."""

    def search(self, request: SearchRequest) -> SearchResponse:
        """Return results in provider rank order."""


def _read_json_response(http_request: urllib.request.Request, timeout_seconds: float) -> dict:
    try:
        with urllib.request.urlopen(http_request, timeout=timeout_seconds) as response:
            body = response.read(_MAX_RESPONSE_BYTES)
    except TimeoutError as exc:
        raise SearchTimeoutError(f"search request timed out: {exc}") from exc
    except urllib.error.HTTPError as exc:
        raise SearchProviderError(f"search provider returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise SearchTimeoutError(f"search request timed out: {exc.reason}") from exc
        raise SearchProviderError(f"search connection failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SearchProviderError(f"search provider returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SearchProviderError("search provider returned a non-object JSON body")
    return parsed


def _require_api_key(env_var: str) -> str:
    api_key = os.environ.get(env_var)
    if not api_key:
        raise SearchProviderError(f"missing search API key: set the {env_var} environment variable")
    return api_key


class BraveSearchProvider:
    """Live adapter for the Brave Search API (https://api.search.brave.com).

    Requires the ``BRAVE_API_KEY`` environment variable.
    """

    def __init__(
        self,
        *,
        api_key_env: str = "BRAVE_API_KEY",
        base_url: str = "https://api.search.brave.com/res/v1/web/search",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._api_key_env = api_key_env
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def search(self, request: SearchRequest) -> SearchResponse:
        api_key = _require_api_key(self._api_key_env)
        query = urllib.parse.urlencode({"q": request.query_text, "count": request.limit})
        http_request = urllib.request.Request(
            f"{self._base_url}?{query}",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
            method="GET",
        )
        body = _read_json_response(http_request, self._timeout_seconds)
        web = body.get("web")
        raw_results = web.get("results", []) if isinstance(web, dict) else []
        results = []
        for entry in raw_results[: request.limit]:
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if isinstance(url, str) and url:
                results.append(SearchResult(original_url=url, title=str(entry.get("title") or "")))
        return SearchResponse(results=results)


class SerperSearchProvider:
    """Live adapter for the Serper Google-results API (https://serper.dev).

    Requires the ``SERPER_API_KEY`` environment variable.
    """

    def __init__(
        self,
        *,
        api_key_env: str = "SERPER_API_KEY",
        base_url: str = "https://google.serper.dev/search",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._api_key_env = api_key_env
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def search(self, request: SearchRequest) -> SearchResponse:
        api_key = _require_api_key(self._api_key_env)
        http_request = urllib.request.Request(
            self._base_url,
            data=json.dumps({"q": request.query_text, "num": request.limit}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-API-KEY": api_key,
            },
            method="POST",
        )
        body = _read_json_response(http_request, self._timeout_seconds)
        raw_results = body.get("organic")
        if not isinstance(raw_results, list):
            raw_results = []
        results = []
        for entry in raw_results[: request.limit]:
            if not isinstance(entry, dict):
                continue
            url = entry.get("link")
            if isinstance(url, str) and url:
                results.append(SearchResult(original_url=url, title=str(entry.get("title") or "")))
        return SearchResponse(results=results)


SEARCH_VENDORS: dict[str, type] = {
    "brave": BraveSearchProvider,
    "serper": SerperSearchProvider,
}

SEARCH_KEY_ENV_BY_VENDOR: dict[str, str] = {
    "brave": "BRAVE_API_KEY",
    "serper": "SERPER_API_KEY",
}


def resolve_search_vendor(vendor: str | None = None) -> str:
    """Resolve the configured vendor name (SEARCH_PROVIDER env, default brave)."""
    return (vendor or os.environ.get("SEARCH_PROVIDER") or "brave").strip().lower()


def missing_search_configuration(vendor: str | None = None) -> list[str]:
    """Report missing search configuration without exposing secret values."""
    name = resolve_search_vendor(vendor)
    if name not in SEARCH_VENDORS:
        known = ", ".join(sorted(SEARCH_VENDORS))
        return [f"unknown search vendor {name!r}; known vendors: {known}"]
    key_env = SEARCH_KEY_ENV_BY_VENDOR[name]
    if not os.environ.get(key_env):
        return [f"{key_env} is not set (search vendor {name!r})"]
    return []


def build_search_provider(vendor: str | None = None) -> SearchProvider:
    """Build the configured live search provider.

    The vendor comes from the ``SEARCH_PROVIDER`` environment variable when
    not passed explicitly (default ``brave``).
    """
    name = resolve_search_vendor(vendor)
    provider_type = SEARCH_VENDORS.get(name)
    if provider_type is None:
        known = ", ".join(sorted(SEARCH_VENDORS))
        raise SearchProviderError(f"unknown search vendor {name!r}; known vendors: {known}")
    return provider_type()
