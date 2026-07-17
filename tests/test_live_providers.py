"""Tests for the post-MVP live web adapters and the CLI run command.

All tests are offline: HTTP calls are intercepted by patching
``urllib.request.urlopen`` inside the provider modules, and the CLI test
substitutes deterministic fake providers at the documented seam.
"""

from __future__ import annotations

import io
import json
import socket
import urllib.error
from pathlib import Path

import pytest

import cli
import providers.scraper as scraper_module
import providers.search as search_module
from evaluations.fakes import DRAFT_STATEMENT, FakeScraper, FakeSearch, StageLLM
from providers.llm import (
    DEFAULT_LIVE_MODEL_MAP,
    KNOWN_MODEL_ALIASES,
    UnknownModelAliasError,
    model_map_from_env,
)
from providers.scraper import (
    ScrapeRequest,
    ScraperProviderError,
    ScraperTimeoutError,
    UrllibScraperProvider,
    extract_text_from_html,
)
from providers.search import (
    BraveSearchProvider,
    SearchProviderError,
    SearchRequest,
    SearchTimeoutError,
    SerperSearchProvider,
    build_search_provider,
)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _guard(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network access attempted during offline adapter tests")

    monkeypatch.setattr(socket.socket, "connect", _guard)


class FakeHTTPResponse:
    """Minimal stand-in for the object returned by urllib.request.urlopen."""

    def __init__(
        self,
        body: bytes,
        *,
        url: str = "https://resolved.example.com/page",
        content_type: str = "application/json",
    ) -> None:
        self._body = body
        self._url = url
        self.headers = {"Content-Type": content_type}

    def read(self, limit: int | None = None) -> bytes:
        return self._body if limit is None else self._body[:limit]

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def patch_urlopen(monkeypatch: pytest.MonkeyPatch, module: object, handler) -> list:
    requests: list = []

    def fake_urlopen(http_request, timeout: float = 0.0):
        requests.append(http_request)
        result = handler(http_request)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    return requests


# ---------------------------------------------------------------------------
# Search adapters
# ---------------------------------------------------------------------------


def brave_body(urls: list[str]) -> bytes:
    return json.dumps(
        {"web": {"results": [{"url": url, "title": f"Title {i}"} for i, url in enumerate(urls)]}}
    ).encode("utf-8")


def serper_body(urls: list[str]) -> bytes:
    return json.dumps(
        {"organic": [{"link": url, "title": f"Title {i}"} for i, url in enumerate(urls)]}
    ).encode("utf-8")


def test_brave_search_parses_results_in_rank_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    urls = ["https://a.example.com", "https://b.example.com", "https://c.example.com"]
    sent = patch_urlopen(monkeypatch, search_module, lambda req: FakeHTTPResponse(brave_body(urls)))
    response = BraveSearchProvider().search(
        SearchRequest(query_text="remote work -site:reddit.com", limit=3)
    )
    assert [result.original_url for result in response.results] == urls
    assert sent[0].get_header("X-subscription-token") == "test-key"
    assert "remote+work" in sent[0].full_url or "remote%20work" in sent[0].full_url


def test_brave_search_respects_limit_and_skips_malformed_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    body = json.dumps(
        {
            "web": {
                "results": [
                    {"url": "https://a.example.com"},
                    "not-a-dict",
                    {"title": "missing url"},
                    {"url": "https://b.example.com"},
                    {"url": "https://c.example.com"},
                ]
            }
        }
    ).encode("utf-8")
    patch_urlopen(monkeypatch, search_module, lambda req: FakeHTTPResponse(body))
    response = BraveSearchProvider().search(SearchRequest(query_text="q", limit=4))
    assert [result.original_url for result in response.results] == [
        "https://a.example.com",
        "https://b.example.com",
    ]


def test_serper_search_posts_query_and_parses_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "serper-key")
    urls = ["https://x.example.com", "https://y.example.com", "https://z.example.com"]
    sent = patch_urlopen(
        monkeypatch, search_module, lambda req: FakeHTTPResponse(serper_body(urls))
    )
    response = SerperSearchProvider().search(
        SearchRequest(query_text="remote work -site:quora.com", limit=3)
    )
    assert [result.original_url for result in response.results] == urls
    payload = json.loads(sent[0].data.decode("utf-8"))
    assert payload == {"q": "remote work -site:quora.com", "num": 3}
    assert sent[0].get_header("X-api-key") == "serper-key"


def test_search_missing_api_key_raises_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(SearchProviderError, match="BRAVE_API_KEY"):
        BraveSearchProvider().search(SearchRequest(query_text="q", limit=3))
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    with pytest.raises(SearchProviderError, match="SERPER_API_KEY"):
        SerperSearchProvider().search(SearchRequest(query_text="q", limit=3))


def test_search_http_and_timeout_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAVE_API_KEY", "test-key")
    patch_urlopen(
        monkeypatch,
        search_module,
        lambda req: urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, io.BytesIO()),
    )
    with pytest.raises(SearchProviderError, match="HTTP 429"):
        BraveSearchProvider().search(SearchRequest(query_text="q", limit=3))

    patch_urlopen(monkeypatch, search_module, lambda req: TimeoutError("slow"))
    with pytest.raises(SearchTimeoutError):
        BraveSearchProvider().search(SearchRequest(query_text="q", limit=3))

    patch_urlopen(
        monkeypatch,
        search_module,
        lambda req: FakeHTTPResponse(b"this is not json"),
    )
    with pytest.raises(SearchProviderError, match="invalid JSON"):
        BraveSearchProvider().search(SearchRequest(query_text="q", limit=3))


def test_build_search_provider_selects_vendor(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(build_search_provider("brave"), BraveSearchProvider)
    assert isinstance(build_search_provider("serper"), SerperSearchProvider)
    monkeypatch.setenv("SEARCH_PROVIDER", "serper")
    assert isinstance(build_search_provider(), SerperSearchProvider)
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    assert isinstance(build_search_provider(), BraveSearchProvider)
    with pytest.raises(SearchProviderError, match="unknown search vendor"):
        build_search_provider("askjeeves")


# ---------------------------------------------------------------------------
# Scraper adapter and HTML text extraction
# ---------------------------------------------------------------------------

SAMPLE_HTML = b"""
<html>
  <head><title>Ignored Title Block</title><style>body {color: red}</style></head>
  <body>
    <script>var hidden = "should never appear";</script>
    <nav>Site navigation</nav>
    <article>
      <h1>Remote Work Study</h1>
      <p>The study reported a 42 percent increase.</p>
      <p>Analysts cautioned about the sample.</p>
    </article>
  </body>
</html>
"""


def test_extract_text_from_html_skips_non_content() -> None:
    text = extract_text_from_html(SAMPLE_HTML.decode("utf-8"))
    assert "The study reported a 42 percent increase." in text
    assert "Analysts cautioned about the sample." in text
    assert "should never appear" not in text
    assert "color: red" not in text
    assert "Ignored Title Block" not in text


def test_urllib_scraper_returns_extracted_text_and_resolved_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = patch_urlopen(
        monkeypatch,
        scraper_module,
        lambda req: FakeHTTPResponse(
            SAMPLE_HTML,
            url="https://final.example.com/article",
            content_type="text/html; charset=utf-8",
        ),
    )
    response = UrllibScraperProvider().scrape(
        ScrapeRequest(url="https://start.example.com/article", timeout_seconds=5.0)
    )
    assert response.resolved_url == "https://final.example.com/article"
    assert response.content_type == "text/html"
    assert "42 percent increase" in response.text
    assert "should never appear" not in response.text
    assert sent[0].get_header("User-agent")


def test_urllib_scraper_reports_non_textual_content_without_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_urlopen(
        monkeypatch,
        scraper_module,
        lambda req: FakeHTTPResponse(
            b"%PDF-1.7 binary bytes",
            url="https://pdf.example.com/report.pdf",
            content_type="application/pdf",
        ),
    )
    response = UrllibScraperProvider().scrape(
        ScrapeRequest(url="https://pdf.example.com/report.pdf", timeout_seconds=5.0)
    )
    assert response.content_type == "application/pdf"
    assert response.text == ""


def test_urllib_scraper_plain_text_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_urlopen(
        monkeypatch,
        scraper_module,
        lambda req: FakeHTTPResponse(
            "plain text body with facts.".encode("latin-1"),
            content_type="text/plain; charset=latin-1",
        ),
    )
    response = UrllibScraperProvider().scrape(
        ScrapeRequest(url="https://txt.example.com", timeout_seconds=5.0)
    )
    assert response.text == "plain text body with facts."


def test_urllib_scraper_timeout_and_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_urlopen(monkeypatch, scraper_module, lambda req: TimeoutError("slow"))
    with pytest.raises(ScraperTimeoutError):
        UrllibScraperProvider().scrape(
            ScrapeRequest(url="https://slow.example.com", timeout_seconds=1.0)
        )

    patch_urlopen(
        monkeypatch,
        scraper_module,
        lambda req: urllib.error.HTTPError(req.full_url, 404, "not found", {}, io.BytesIO()),
    )
    with pytest.raises(ScraperProviderError, match="HTTP 404"):
        UrllibScraperProvider().scrape(
            ScrapeRequest(url="https://gone.example.com", timeout_seconds=1.0)
        )

    patch_urlopen(
        monkeypatch,
        scraper_module,
        lambda req: urllib.error.URLError(TimeoutError("handshake")),
    )
    with pytest.raises(ScraperTimeoutError):
        UrllibScraperProvider().scrape(
            ScrapeRequest(url="https://slow2.example.com", timeout_seconds=1.0)
        )


# ---------------------------------------------------------------------------
# LLM model-map configuration
# ---------------------------------------------------------------------------


def test_model_map_defaults_cover_every_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_MODEL_MAP", raising=False)
    mapping = model_map_from_env()
    assert set(mapping) == set(KNOWN_MODEL_ALIASES)
    assert mapping == dict(DEFAULT_LIVE_MODEL_MAP)


def test_model_map_env_override_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL_MAP", json.dumps({"mimo-v2.5": "my-custom-model"}))
    mapping = model_map_from_env()
    assert mapping["mimo-v2.5"] == "my-custom-model"
    assert mapping["mimo-v2.5-pro"] == DEFAULT_LIVE_MODEL_MAP["mimo-v2.5-pro"]

    monkeypatch.setenv("LLM_MODEL_MAP", "{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        model_map_from_env()

    monkeypatch.setenv("LLM_MODEL_MAP", json.dumps({"gpt-nonexistent-alias": "x"}))
    with pytest.raises(UnknownModelAliasError):
        model_map_from_env()

    monkeypatch.setenv("LLM_MODEL_MAP", json.dumps({"mimo-v2.5": ""}))
    with pytest.raises(ValueError, match="non-empty"):
        model_map_from_env()


# ---------------------------------------------------------------------------
# CLI run command
# ---------------------------------------------------------------------------


def test_cli_run_reports_missing_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)
    exit_code = cli.main(["run", "Some claim.", "--db", str(tmp_path / "db.sqlite3")])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "OPENAI_API_KEY" in captured.err
    assert "BRAVE_API_KEY" in captured.err


def test_cli_run_executes_live_pipeline_with_injected_providers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")
    monkeypatch.setattr(
        cli,
        "_build_live_providers",
        lambda vendor: (StageLLM(), FakeSearch(), FakeScraper()),
    )
    db_path = tmp_path / "live.sqlite3"
    exit_code = cli.main(["run", "Remote work increased productivity.", "--db", str(db_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "status: released" in captured.out
    assert "rendered hash:" in captured.out
    assert DRAFT_STATEMENT in captured.out
    assert db_path.is_file()


def test_cli_run_failed_run_exits_nonzero_with_resume_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")
    monkeypatch.setattr(
        cli,
        "_build_live_providers",
        lambda vendor: (
            StageLLM(),
            FakeSearch(fail_stances={"opposing"}),
            FakeScraper(),
        ),
    )
    exit_code = cli.main(
        ["run", "Remote work increased productivity.", "--db", str(tmp_path / "f.sqlite3")]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "status: failed" in captured.out
    assert "opposing researcher failed" in captured.out
    assert "--run-id" in captured.out


def test_cli_run_rejects_invalid_run_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")
    exit_code = cli.main(
        [
            "run",
            "Some claim.",
            "--db",
            str(tmp_path / "db.sqlite3"),
            "--run-id",
            "not-a-uuid",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "invalid run id" in captured.err
