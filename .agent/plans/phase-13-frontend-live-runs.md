# Post-MVP: Live Runs from the Frontend (Phase 13)

## Purpose

Let a user research a brand-new claim from the browser instead of the terminal, and resume
unfinished runs. This was explicitly requested by the user after Phase 12.

Phase 12's handoff drew a read-only boundary around the frontend and recorded that "anything that
would start a run, spend model budget, or write to a database needs explicit user direction first."
That direction was given, and this phase lifts exactly that boundary — nothing else.

No pipeline logic changed. The frontend drives the existing `orchestrator.run_live()` through the
same provider seam `cli.py` uses, so every deterministic gate, budget, retry/fallback policy,
restart behavior, and validator applies to a UI-started run unchanged.

## Files changed

- `frontend/run_launcher.py` (new): the frontend's only write path, Streamlit-free so it is testable
  headlessly.
  - `preflight()` composes `missing_llm_configuration()`, `missing_search_configuration()`,
    `resolve_search_vendor()`, and `model_map_from_env()` into a typed report of vendor, key
    presence, alias-to-model mapping, and blocking problems. Presence only; key values are never
    read into the report. A malformed `LLM_MODEL_MAP` becomes a listed problem instead of an
    exception.
  - `LiveRunRequest` validates the claim, database path, vendor, model-attempt budget, and optional
    resume run ID.
  - `start_run()` assigns the run ID up front (so progress can be polled immediately) and runs
    `run_live()` on a daemon thread with `cancel_check=cancel_event.is_set` and a `RunBudgets`
    budget. `provider_factory` defaults to `cli._build_live_providers` and is the injection seam
    tests use.
  - `RunHandle` exposes `is_running()`, `wait()`, `cancel()`, `cancel_requested()`, `outcome()`
    (the typed `LiveRunResult`), and `error()` for launcher-level failures such as missing
    configuration.
  - `progress()` wraps `inspect_run()` plus `read_stage_model_attempts_for_run()` into a typed
    snapshot including token spend. It never creates a database file.
- `frontend/views/launch_view.py` (new): the "New run" page — pre-flight card, claim form, vendor
  selector, budget input, an explicit cost acknowledgement, live monitor (stage timeline, evidence
  funnel, attempts against budget, token spend), Cancel, and the resume confirmation panel.
- `frontend/views/runs_view.py`: a "Resume this run" button on failed and cancelled runs. It only
  navigates; the launcher owns the single confirmation step that actually spends money.
- `frontend/streamlit_app.py`: three-page nav (New run, Runs, Fixture pipeline) and a
  pending-navigation key applied before the nav widget is created.
- `tests/test_frontend_launcher.py` (new): 11 offline tests.
- `frontend/README.md`, `README.md`, `.agent/PLANS.md`, `STATUS.md`, `HANDOFF.md`.

## Design decisions

- **Worker thread plus DB polling.** A run takes minutes; blocking the Streamlit script would freeze
  the page with no progress and no cancel. The orchestrator already checkpoints stage and artifacts
  to SQLite, so the page polls the run's own persisted state rather than needing any new
  instrumentation. Writer and reader use separate connections, which the store's
  connection-per-call design already guarantees.
- **Cancellation is honest about its granularity.** `_checkpoint()` is the only place cancellation is
  checked, so a stage already in flight finishes first. The UI says so rather than implying an
  instant stop.
- **One confirmation point.** Both starting and resuming route through the launcher page's
  acknowledgement checkbox, so there is exactly one place in the UI that can spend money.
- **Pre-flight blocks rather than fails.** Start stays disabled until configuration is complete, and
  names the exact missing variable.
- No dependency added: `threading` is stdlib. `orchestrator.py` and `cli.py` were not modified, so
  the Phase 6 offline-guard source scan still passes unmodified.

## Verification

- `python -m pytest -q`: 327 passed, 1 skipped (316 before, 11 added).
- `python -m ruff check .` / `python -m ruff format --check .`: clean.
- Browser checks against the real database, without starting a paid run:
  - the pre-flight card correctly reported `BRAVE_API_KEY is not set` for the default vendor and
    flipped to ready when the vendor selector was switched to serper
  - Start stayed disabled while configuration was incomplete
  - "Resume this run" on failed run `9c1345fb` navigated to the launcher and showed the original
    claim, the stage it stopped at, the spend notice, and a disabled Resume button
  - `live_runs.sqlite3` was unchanged throughout
- The start/cancel/resume/progress mechanics are covered offline by
  `tests/test_frontend_launcher.py` using the `evaluations/fakes.py` provider stack: a complete run
  reaching `released`, a cancelled run, a provider-configuration failure surfaced on the handle, and
  progress tracking.

## First live run from the UI

Claim "Four-day work weeks reduce employee burnout." (run_id f2b68873, serper,
gpt-5.4-nano, budget 120) was started from the New run page. Funnel: 18 retrieval attempts ->
8 unique snapshots -> 15 extractions -> 1 gate-passing candidate -> 0 Ledger records. The run
ended `failed` with "no approved statements entered the Ledger: reviewer rejected ... after one
revision (not_entailed)", using 13 model attempts and ~38k tokens.

That is the gate chain doing its job rather than a frontend defect: with no Reviewer-approved
statement, the run failed explicitly instead of releasing unsupported text. The UI drove the whole
loop — pre-flight gating, Start, live progress, explicit verdict, and navigation into the run
detail.

## Known limitations

- Cancellation lands at the next stage boundary, not immediately.
- Closing the browser does not stop a run; the worker thread finishes and persists it.
- One active run per browser session.
- Retrieval quality, not the frontend, is the limiting factor on run outcomes: only 8 of 18
  attempts produced unique snapshots and 1 of 15 extractions passed the quotation gate.
