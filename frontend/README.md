# Local Frontend

A local Streamlit frontend with three pages. Launch from the repository root:

```bash
streamlit run frontend/streamlit_app.py
```

## New run

Researches a brand-new claim against the live web, or resumes an unfinished run.

**This page spends money.** Starting a run makes live searches, scrapes real pages, and
calls your model endpoint; the recorded reference run used 22 model attempts and about
53,000 tokens.

- A pre-flight card shows the active search vendor, whether each API key is present, and
  which endpoint models the routing aliases map to. Start stays disabled until the
  configuration is complete, naming the exact missing variable. Key values are never
  displayed.
- Set the claim, search vendor, and model-attempt budget, then acknowledge the cost to
  enable Start.
- While the run executes on a worker thread, the page polls the run's own persisted
  checkpoints and shows the stage timeline, evidence funnel, attempts against budget, and
  token spend. **Cancel** is honored at the next stage boundary, so a stage already in
  flight finishes first. Leaving the page does not stop a run.
- Failed and cancelled runs can be resumed from the Runs page; resuming continues from
  persisted artifacts rather than repeating completed work.

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

This page only reads: it never calls a provider, touches the network, or writes
to the database. Its one action is handing a failed or cancelled run to the New
run page for resume confirmation. Runs can also still be started from the CLI:

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
- `run_launcher.py` — typed, Streamlit-free pre-flight, threaded run execution,
  cancellation, and progress polling; the only write path in the frontend
- `theme.py` + `assets/app.css` — page configuration and HTML/CSS primitives
- `views/` — the three page renderers
