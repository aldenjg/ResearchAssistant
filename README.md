# Debate Research Agent System

This repository contains a phase-gated Debate Research Agent System. The MVP separates retrieval, semantic approval, Ledger admission, synthesis, and deterministic final validation so factual text can only be released after passing typed gates.

Current status: the full Phase 0-10 roadmap is complete (see `.agent/PLANS.md`), plus post-MVP live web integration. The system covers strict Pydantic contracts, SQLite persistence, deterministic snapshot/quotation integrity, the Analyst score policy, Statement Reviewer auditing with one revision, Ledger admission, typed synthesis, deterministic final rendering and release validation, a vendor-isolated LLM provider with MiMo-first model routing, a real orchestrator with two-worker researcher concurrency and audited retry/fallback, an offline evaluation and adversarial-testing framework, and live web adapters (Brave/Serper search plus a stdlib scraper).

## Running a live research run

1. Copy `.env.example` to `.env` and set:
   - `OPENAI_API_KEY` — key for an OpenAI-compatible LLM endpoint.
   - `BRAVE_API_KEY` (default vendor) or `SERPER_API_KEY` plus `SEARCH_PROVIDER=serper` — a web-search API key.
   - Optionally `LLM_MODEL_MAP` — JSON mapping of routing aliases to your endpoint's model names (defaults target `gpt-4.1` / `gpt-4.1-mini`).
2. Run:

```bash
python cli.py run "Your exact claim text here."
```

The run ends in an explicit state: `released` prints the final brief and its hash; `blocked` prints the validator errors; `failed` prints the reason and a resume command (`--run-id`) that continues from persisted artifacts without duplicating work. Inspect any run with `python cli.py inspect-run live_runs.sqlite3 <run_id>`, or browse every persisted run in the local frontend:

```bash
streamlit run frontend/streamlit_app.py
```

The frontend is read-only over run databases: it shows the release verdict, evidence funnel, Ledger records with their two-axis scores, provenance, validator errors, and audited model attempts, and verifies a released brief's hash by re-rendering it from persisted artifacts. Runs are started from the CLI only. See `frontend/README.md`.

## Offline usage

```bash
python cli.py run-fixture tests/fixtures/basic_valid_run   # deterministic fixture pipeline
python evaluations/run_evaluations.py                      # offline evaluation + adversarial corpus
streamlit run frontend/streamlit_app.py                    # local run browser + fixture frontend
```

## Start here (for contributors and assistants)

1. Read `AGENTS.md`.
2. Read `ARCHITECTURE.md`.
3. Read `CONVENTIONS.md`.
4. Check `STATUS.md`, `HANDOFF.md`, and `.agent/PLANS.md`.
5. Read the relevant phase plan in `.agent/plans/`.

## Verification

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

All normal tests are deterministic and offline; live LLM integration tests and live evaluations are opt-in via `RUN_LLM_INTEGRATION_TESTS=1` and `RUN_LIVE_EVALUATIONS=1`.
