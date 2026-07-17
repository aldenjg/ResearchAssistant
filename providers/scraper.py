"""Vendor-neutral synchronous scraper provider contracts and a live adapter.

The Protocol and typed artifacts are the Phase 7B contracts. The live
``UrllibScraperProvider`` (stdlib-only) was added for post-MVP live web runs;
it fetches one URL, follows redirects, and extracts readable text from HTML
without interpreting its meaning.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from enum import StrEnum
from html.parser import HTMLParser
from typing import Protocol, runtime_checkable

from pydantic import Field, model_validator

from models import StrictModel

_MAX_DOWNLOAD_BYTES = 2_000_000
_DEFAULT_USER_AGENT = "DebateResearchAgent/0.1 (evidence research; contact repository owner)"
_TEXTUAL_CONTENT_TYPES_PREFIX = "text/"
_TEXTUAL_CONTENT_TYPES = {"application/xhtml+xml", "application/xml"}


class ScraperProviderError(RuntimeError):
    """Raised when a scraper provider fails to retrieve a source."""


class ScraperTimeoutError(ScraperProviderError):
    """Raised when a scraper provider exceeds its configured timeout."""


class ScrapeStatus(StrEnum):
    RETRIEVED = "retrieved"
    FAILED = "failed"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"
    DUPLICATE_URL = "duplicate_url"
    DUPLICATE_CONTENT = "duplicate_content"


class ScrapeRequest(StrictModel):
    url: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


class ScrapeResponse(StrictModel):
    resolved_url: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    text: str


class RetryPolicy(StrictModel):
    max_attempts: int = Field(default=2, ge=1, le=5)
    timeout_seconds: float = Field(default=10.0, gt=0)


class ScrapeFailure(StrictModel):
    status: ScrapeStatus
    message: str = Field(min_length=1)
    attempts_made: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_failure_status(self) -> ScrapeFailure:
        if self.status not in {ScrapeStatus.FAILED, ScrapeStatus.TIMEOUT}:
            raise ValueError("scrape failures require failed or timeout status")
        return self


@runtime_checkable
class ScraperProvider(Protocol):
    """A vendor-isolated, synchronous scraper provider."""

    def scrape(self, request: ScrapeRequest) -> ScrapeResponse:
        """Retrieve one URL without interpreting its content."""


class _HTMLTextExtractor(HTMLParser):
    """Extract visible text from HTML, skipping non-content elements."""

    _SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head", "iframe"}
    _BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "td",
        "th",
        "table",
        "section",
        "article",
        "header",
        "footer",
        "blockquote",
        "figure",
        "figcaption",
        "main",
        "aside",
        "nav",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        return " ".join(part.strip() for part in self._parts if part.strip())


def extract_text_from_html(html: str) -> str:
    """Deterministically extract readable text from an HTML document."""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text()


class UrllibScraperProvider:
    """Stdlib-only live scraper: fetch one URL and extract readable text.

    HTML is reduced to visible text; other textual types are returned as-is.
    Non-textual content types are reported with empty text so the retrieval
    layer can mark them unsupported. Downloads are capped at 2 MB; the
    retrieval layer applies its own 3,000-word snapshot limit downstream.
    """

    def __init__(self, *, user_agent: str = _DEFAULT_USER_AGENT) -> None:
        self._user_agent = user_agent

    def scrape(self, request: ScrapeRequest) -> ScrapeResponse:
        http_request = urllib.request.Request(
            request.url,
            headers={"User-Agent": self._user_agent, "Accept": "text/html,*/*;q=0.8"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(http_request, timeout=request.timeout_seconds) as response:
                resolved_url = response.geturl() or request.url
                raw_content_type = str(response.headers.get("Content-Type") or "")
                content_type = raw_content_type.split(";", maxsplit=1)[0].strip().lower()
                if not content_type:
                    content_type = "application/octet-stream"
                if not _is_textual_content_type(content_type):
                    return ScrapeResponse(
                        resolved_url=resolved_url,
                        content_type=content_type,
                        text="",
                    )
                charset = _charset_from_content_type(raw_content_type)
                body = response.read(_MAX_DOWNLOAD_BYTES)
        except TimeoutError as exc:
            raise ScraperTimeoutError(f"scrape timed out: {request.url}") from exc
        except urllib.error.HTTPError as exc:
            raise ScraperProviderError(
                f"scrape failed with HTTP {exc.code}: {request.url}"
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ScraperTimeoutError(f"scrape timed out: {request.url}") from exc
            raise ScraperProviderError(
                f"scrape connection failed: {request.url}: {exc.reason}"
            ) from exc

        decoded = body.decode(charset, errors="replace")
        if content_type in {"text/html", "application/xhtml+xml"}:
            text = extract_text_from_html(decoded)
        else:
            text = decoded
        return ScrapeResponse(
            resolved_url=resolved_url,
            content_type=content_type,
            text=text,
        )


def _is_textual_content_type(content_type: str) -> bool:
    return (
        content_type.startswith(_TEXTUAL_CONTENT_TYPES_PREFIX)
        or content_type in _TEXTUAL_CONTENT_TYPES
    )


def _charset_from_content_type(raw_content_type: str) -> str:
    for part in raw_content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"')
    return "utf-8"
