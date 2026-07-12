# Phase 10: Evaluation and Adversarial Testing

## Purpose

Build an offline evaluation and adversarial testing framework for the MVP
that measures whether citation accuracy, snapshot integrity, placement
consistency, validator resistance, and the MiMo-first model-routing policy
actually hold, with machine-readable and human-readable outputs that always
agree.

## Files changed

- `evaluations/__init__.py`, `evaluations/fakes.py`,
  `evaluations/scenarios.py`, `evaluations/run_evaluations.py`,
  `evaluations/README.md`, `evaluations/.gitignore` (new).
- `evaluations/cases/*.json` (27 cases) and
  `evaluations/cases/regressions/regression_altered_statement.json` (new).
- `tests/test_phase10.py` (new, 22 tests).
- `.agent/plans/phase-10-evaluation.md`, `STATUS.md`, `HANDOFF.md`.

No earlier-phase file was modified; no validator was touched.

## Implementation design

- The corpus is data-only JSON under `evaluations/cases/` (plus
  `regressions/`), loaded and sorted deterministically; duplicate or
  malformed case files are rejected.
- Four case kinds:
  - **pipeline**: full offline `run_live` executions with deterministic fake
    providers (`evaluations/fakes.py`, mirroring the Phase 9 test doubles)
    against scenario definitions with expected status plus post-run checks
    (`evaluations/scenarios.py`). Fixed uuid5 run IDs and a thread-safe
    deterministic clock make every execution reproducible.
  - **mutation**: ten adversarial attacks (altered/paraphrased statement,
    placement drift, entailment drift, wrong reviewer approval, unknown
    Ledger claim, unapproved template, qualified-evidence promotion,
    duplicate claim use, hidden prose field) applied to a released base run's
    synthesis; each must be blocked by the untouched Phase 5 validator. Any
    escape is a failing case with a `VALIDATOR ESCAPE` report.
  - **alias_quality**: frozen extractor inputs evaluated per model alias via
    named response kinds, feeding per-alias malformed and
    exact-quote-failure rates and the MiMo Pro minus MiMo normal quality
    delta.
  - **live_comparison**: optional extractor comparison (MiMo V2.5 versus
    DeepSeek V4 Flash) on the *same frozen inputs*, skipped with an explicit
    reason unless `RUN_LIVE_EVALUATIONS=1`; when enabled it records exact
    model aliases, pinned snapshots, and frozen-input SHA-256 hashes.
- Metrics computed from persisted run artifacts (not from case expectations):
  citation accuracy (segment offsets re-verified against snapshots), snapshot
  integrity (hashes recomputed), bracket accuracy (bracket context
  re-validated), unsupported-claim rate, validator escape rate, mutation
  block rate, placement consistency, score separation, Reviewer and Analyst
  rejection rates, retrieval parity, prompt-injection resistance, completion
  time (deterministic clock seconds), per-stage route outcome counts with
  primary-success/retry/fallback rates, per-alias malformed and exact-quote
  failure rates, quality delta, fallback gate violations, same-model
  Analyst/Reviewer correlated-error cases, and token-based costs (configured
  pricing per alias; cost per completed run and per successful artifact,
  with per-alias token usage exposed so the arithmetic is independently
  checkable).
- Outputs: `evaluations/output/evaluation_results.json` (sorted keys, no
  wall-clock or filesystem paths, byte-deterministic for the same corpus)
  and `evaluation_summary.txt` rendered purely from the JSON, so the two
  always agree. Exit code 0 only when no case fails; failing cases produce
  clear per-case failure reports and exit code 1. Skips exist only for the
  explicitly gated live comparison and are always reported.

## Evaluation design for MiMo stage ownership

The alias-quality corpus freezes identical extractor inputs and compares
aliases on schema validity and verbatim-quote success — the two objective
failure modes the deterministic gates measure. Stage ownership follows from
this data plus the route metrics of the pipeline corpus: a stage's primary
should be the cheapest alias whose primary-success rate on that stage's real
work units stays high (extractor/reviewer: MiMo normal), while stages whose
failures are expensive and rare keep MiMo Pro (planner/analyst/synthesizer).
The current shipped corpus yields a pro-minus-normal quality delta of
0.333333 on frozen extraction cases (driven by the deliberately adversarial
`alias_quality_normal_quote_miss` case) while pipeline extractor primary
success remains high, consistent with the MiMo-first defaults.

## Offline routing tests versus optional live comparison

Offline deterministic routing tests (pipeline + alias_quality kinds) are the
source of truth for gate behavior and routing mechanics; they run on every
evaluation. The live comparison is a separate, explicitly configured kind
that measures real vendor quality on the same frozen inputs and never
substitutes for the offline tests.

## Criteria for changing routing defaults after evaluation

Routing defaults change only when all three hold, and never merely on
external benchmark preference:

1. The offline evaluation corpus (or a live comparison over the same frozen
   corpus) shows a material, reproducible per-stage difference — for example
   a sustained primary-success-rate drop or a quality delta that reverses
   sign on the project corpus.
2. The change does not weaken any deterministic or Reviewer gate, and
   fallback gate violations remain zero.
3. Cost per successful artifact under the new routing is justified by the
   measured quality difference on this corpus.

## Non-negotiable invariants held

- Validators are never weakened: the runner imports and calls the Phase 5
  validator unchanged; a test asserts the function object and config version
  are untouched after evaluation runs.
- A failing evaluation produces a clear failure report and nonzero exit;
  failing cases are never skipped.
- Discovered validator escapes must be added to `cases/regressions/`
  (seeded with the historical altered-statement failure mode).
- Cases are deterministic; normal evaluations run offline (socket-guarded in
  tests); optional live evaluation is skipped unless configured; JSON and
  text outputs agree by construction.

## Acceptance criteria, commands run, and exact results

- `python evaluations/run_evaluations.py`: exit 0; 28 cases, 27 passed,
  0 failed, 1 skipped (live comparison); citation/snapshot/bracket accuracy
  1.0; unsupported-claim and validator-escape rates 0.0; mutation block rate
  1.0 (11/11); placement consistency 1.0; retrieval parity 1.0;
  prompt-injection resistance 1.0; fallback gate violations 0; one
  correlated-error case reported.
- `python -m pytest tests/test_phase1.py ... tests/test_phase10.py -q`
  (exact required command): 283 passed, 1 skipped.
- `python -m ruff check .`: all checks passed.
- `python -m ruff format --check .`: 33 files already formatted.
- `python -m pytest -q` (full suite): 289 passed, 1 skipped.

## Unresolved risks

- Offline alias-quality results are scripted fixtures measuring the
  measurement machinery, not real vendor quality; real quality data requires
  the gated live comparison with a configured endpoint.
- Completion-time metrics use the deterministic evaluation clock, not wall
  time; live latency must be measured separately.
- The correlated-error metric flags same-model Analyst/Reviewer pairs but
  does not yet estimate the error correlation itself; that is post-MVP
  hardening work.

## Phase boundary

The next task after this phase — post-MVP hardening based on evaluation
results — was NOT started. No production UI, new provider vendors, or
routing-default changes were made.
