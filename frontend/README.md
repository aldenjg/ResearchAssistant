# Local Frontend

A local Streamlit frontend with two pages. Launch from the repository root:

```bash
streamlit run frontend/streamlit_app.py
```

## Runs (read-only)

Browses the runs already persisted in a run database (default
`live_runs.sqlite3`, changeable in the sidebar). For each run it shows the
release verdict, stage timeline, evidence funnel, and tabs for:

- **Brief** — for released runs, the brief re-rendered from persisted artifacts,
  with its recomputed hash checked against the validator's stored
  `rendered_brief_hash`
- **Ledger** — approved statements with their two-axis Analyst scores, stance,
  placement, entailment, source, snapshot hash, and reviewer approval ID
- **Evidence** — retrieval attempts, trusted snapshots, and gate-passing
  candidates with their quote blocks and segment offsets
- **Decisions** — Analyst approvals/rejections and Statement Reviewer results
  with failure codes and rationales
- **Validation** — the validator verdict and any blocking errors
- **Model attempts** — the audited route, attempt number, latency, and tokens

This page only reads. It never starts a run, calls a provider, touches the
network, or writes to the database — runs are still started from the CLI:

```bash
python cli.py run "Your exact claim text here."
```

Partial and failed runs render whatever they persisted, plus the stage they
stopped at.

## Fixture pipeline

Discovers fixture directories under `tests/fixtures/`, runs the deterministic
offline pipeline, and shows the released or blocked verdict, the final brief,
validator errors, artifact funnel, provenance, and audit trail. Behavior is
unchanged from Phase 7A; only the presentation differs.

## Layout

- `streamlit_app.py` — entry point, navigation shell, and the fixture summary
  helpers (`discover_fixture_runs`, `run_fixture_for_frontend`,
  `summarize_fixture_result`) used by the tests
- `run_reader.py` — typed, Streamlit-free read-only adapters over `store.py`
- `theme.py` + `assets/app.css` — page configuration and HTML/CSS primitives
- `views/` — the two page renderers
