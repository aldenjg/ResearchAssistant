# Post-MVP: Live Web Integration (Phase 11)

## Purpose

Make the completed Phase 0-10 system usable against the real web: live
search and scraper adapters behind the existing Phase 7B protocols, a
model-alias mapping for the live LLM endpoint, and a `python cli.py run`
command that drives the Phase 9 orchestrator end-to-end. This work was
explicitly requested by the user after roadmap completion.

## Files changed

- `providers/search.py`: `BraveSearchProvider` and `SerperSearchProvider`
  (stdlib `urllib` only), `build_search_provider()` vendor selection via
  `SEARCH_PROVIDER`, and `missing_search_configuration()` for pre-flight
  checks. API keys (`BRAVE_API_KEY` / `SERPER_API_KEY`) are read from the
  environment at call time.
- `providers/scraper.py`: `UrllibScraperProvider` — fetches one URL with a
  timeout, follows redirects (recording the resolved URL), caps downloads at
  2 MB, decodes using the response charset, extracts readable text from HTML
  with a stdlib `HTMLParser` (script/style/nav-free), passes other textual
  types through, and reports non-textual content types with empty text so
  the retrieval layer marks them unsupported. Timeouts raise
  `ScraperTimeoutError`; HTTP/connection failures raise
  `ScraperProviderError`.
- `providers/llm.py`: `DEFAULT_LIVE_MODEL_MAP` (routing aliases →
  `gpt-4.1` / `gpt-4.1-mini` tiers), `model_map_from_env()` validating the
  optional `LLM_MODEL_MAP` JSON override against the approved alias
  registry, and `missing_llm_configuration()`.
- `cli.py`: new `run` command (claim, `--db`, `--run-id` resume,
  `--search-vendor`, `--max-model-attempts`) with pre-flight configuration
  checks; exit 0 for released/blocked (the validator doing its job), 1 for
  failed/cancelled with a resume hint, 2 for configuration errors.
- `tests/test_live_providers.py` (new, 17 offline tests).
- `.env.example`, `README.md`, `.agent/PLANS.md`, `STATUS.md`, `HANDOFF.md`.

## Design decisions

- No new dependency: both search adapters and the scraper use only the
  standard library, matching the existing `OpenAICompatibleLLMProvider`.
- Environment reads live in provider modules (the conventions' sanctioned
  env-reading layer), keeping `cli.py` free of `os.environ` so the Phase 6
  offline-guard token scan over `orchestrator.py`/`cli.py` continues to pass
  untouched — no earlier test was modified or weakened.
- The routing aliases and every deterministic gate are unchanged; live
  adapters plug into the same protocols the fake providers implement, so all
  Phase 9 orchestration behavior (audited retry/fallback, budgets, restart,
  cancellation) applies unchanged to live runs.
- `_build_live_providers()` in `cli.py` is a documented seam so tests drive
  the full CLI live path with deterministic fakes.

## Verification

- `python -m pytest tests/test_live_providers.py -q`: 17 passed.
- `python -m pytest -q` (full suite): 306 passed, 1 skipped.
- `python -m ruff check .` / `python -m ruff format --check .`: clean.
- Live smoke test (real network, read-only GETs): `UrllibScraperProvider`
  fetched `https://example.com` and a Wikipedia article, returning
  `text/html` with correctly extracted visible text (16,601 words before the
  downstream 3,000-word snapshot truncation).

## Known limitations and expectations for live runs

- A search API key is required (Brave or Serper free tiers work); without
  it the CLI reports exactly which variable is missing and exits 2.
- Default model names (`gpt-4.1` / `gpt-4.1-mini`) must exist on the
  configured endpoint; override with `LLM_MODEL_MAP` otherwise. Models that
  reject the `temperature` parameter will fail loudly rather than silently.
- The deterministic gates are strict by design: live extractions must quote
  snapshots verbatim with correct bracket sentences and meet length/keyword
  thresholds, so early live runs may reject many candidates or fail with
  "no approved statements" — that is the system refusing to release
  unverified evidence, and the Phase 10 evaluation metrics exist to tune it.
- The scraper does not execute JavaScript and does not consult robots.txt;
  it sends an honest User-Agent and caps downloads. Heavy-JS pages will
  yield thin text and be filtered downstream.
