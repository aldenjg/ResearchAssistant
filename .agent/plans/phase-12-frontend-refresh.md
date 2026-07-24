# Post-MVP: Frontend Refresh and Read-Only Run Browser (Phase 12)

## Purpose

Replace the Phase 7A "extremely basic" fixture screen with a designed local
frontend, and make the artifacts that live runs already persist inspectable in
the browser rather than only through `python cli.py inspect-run`. This work was
explicitly requested by the user after roadmap completion.

The pipeline is not touched. Every gate, hash, budget, and validator behaves
exactly as before; the frontend only reads what a run already wrote.

## Files changed

- `store.py`: added `read_run_manifests()` — a read-only `SELECT ... FROM runs
  ORDER BY created_at DESC` returning typed `RunManifest`s, reusing the existing
  `_row_to_run()`. An uninitialised database raises `KeyError` instead of
  returning an empty list, so a missing schema can never be mistaken for zero
  runs. No schema change, no migration, no new write path.
- `frontend/run_reader.py` (new): typed, Streamlit-free adapters over the
  existing store readers. `list_run_cards()` returns one `RunCard` per persisted
  run; `load_run_detail()` assembles a `RunDetail` holding the run's manifest,
  planner output, retrievals, snapshots, provisional extractions, candidates,
  Analyst decisions, drafts, Reviewer results, Ledger records, synthesis,
  validation, and audited model attempts. Stages that never ran contribute empty
  collections instead of failing the view. For a run whose validation passed, the
  brief is re-rendered with `agents.renderer.render_brief()` — the same
  reconstruction the orchestrator performs for a completed run — and hashed with
  `utils.compute_sha256()` so the UI can prove the text reproduces the
  validator's released hash. `require_database()` refuses to open a path that
  does not exist, so browsing never creates a stray SQLite file.
- `frontend/theme.py` (new): page configuration, stylesheet injection, and pure
  HTML builders (status pill, stat card, evidence funnel, two-axis score meter,
  stage timeline, verdict banner, hash chip, tables, empty states, brief
  wrapper). Every builder escapes untrusted text — claims, approved statements,
  scraped quote blocks, source URLs — before it reaches the page.
- `frontend/assets/app.css` (new): the stylesheet.
- `frontend/views/runs_view.py` (new): the read-only run browser — run list, and
  a detail page with verdict banner, stat row, stage timeline, evidence funnel,
  and Brief / Ledger / Evidence / Decisions / Validation / Model attempts tabs.
- `frontend/views/fixture_view.py` (new): the restyled fixture page. It calls the
  unchanged `discover_fixture_runs()` and `run_fixture_for_frontend()` and uses
  the same `phase7a_summary` session key.
- `frontend/streamlit_app.py`: `main()` became a two-page navigation shell; the
  raw `st.json`/`st.dataframe` renderer was replaced by the views. Every public
  helper the Phase 7A tests import — `FixtureOption`, the `Frontend*` models,
  `discover_fixture_runs()`, `run_fixture_for_frontend()`,
  `summarize_fixture_result()` — is unchanged.
- `.streamlit/config.toml` (new): dark theme base matching the stylesheet;
  usage-statistics gathering disabled.
- `tests/test_phase2.py`: three additive tests for `read_run_manifests()`.
- `tests/test_phase7_frontend.py`: six additive tests for the run reader. No
  existing test was modified.
- `frontend/README.md`, `README.md`, `.agent/PLANS.md`, `STATUS.md`,
  `HANDOFF.md`.

## Design decisions

- No new dependency. Streamlit was already declared; `pandas` and `altair` are
  only Streamlit's transitive dependencies and are deliberately not imported, so
  the `pyproject.toml` dependency list asserted by `tests/test_phase0_foundation`
  is unchanged. React and FastAPI remain out of scope.
- The frontend is strictly read-only. It never calls `run_live`, never touches
  the network, and never writes to a run database; runs are still started from
  the CLI. The one pipeline execution it can trigger is the existing offline
  fixture run.
- Presentation is CSS plus builder-generated HTML — no JavaScript, and no
  reliance on Streamlit's internal class names beyond a few stable `data-testid`
  hooks.
- The released brief shown in the UI is re-rendered from persisted artifacts and
  its hash is compared against the stored `rendered_brief_hash`, so the page
  displays integrity rather than asserting it.
- `orchestrator.py` and `cli.py` were not touched, so the Phase 6 offline-guard
  source scan continues to pass unmodified.

## Verification

- `python -m pytest -q`: 316 passed, 1 skipped (307 before, 9 added).
- `python -m ruff check .` / `python -m ruff format --check .`: clean.
- `python cli.py run-fixture tests/fixtures/basic_valid_run`: released with the
  unchanged hash
  `cfb4182d7469c05f269150605aa24907fbc850ea7f70e4e86633a9c96f60f1ed`.
- Browser check of `streamlit run frontend/streamlit_app.py`: the five persisted
  runs list correctly; the released run
  `50c39cb2-4853-4f3d-803e-2ee58c2daf70` shows its 18 → 11 → 11 → 4 → 2 funnel,
  22 model attempts, both Ledger records with their two-axis scores, and the
  re-rendered brief matching hash `b47b11e4…8ac887ab`; failed runs render their
  partial state with explicit empty states. No console errors, no horizontal
  overflow at 1440px or 375px.
- `live_runs.sqlite3` size and modification time were unchanged after browsing.
