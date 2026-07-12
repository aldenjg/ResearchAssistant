# Evaluations (Phase 10)

Offline evaluation and adversarial testing framework for the Debate Research
Agent System MVP. It measures whether citation accuracy, snapshot integrity,
placement consistency, validator resistance, and the MiMo-first model-routing
policy actually hold.

## Running

From the repository root:

```powershell
python evaluations/run_evaluations.py
```

Exit code 0 means every case passed (explicitly reported skips of the
optional live comparison are allowed); exit code 1 means at least one case
failed, with a clear failure report per failing case.

Outputs (written to `evaluations/output/`, which is git-ignored):

- `evaluation_results.json` — machine-readable results (deterministic for the
  same corpus: fixed run IDs, a deterministic clock, and no wall-clock or
  filesystem paths).
- `evaluation_summary.txt` — human-readable summary rendered *from* the JSON,
  so the two outputs always agree.

## Corpus

Cases live in `evaluations/cases/*.json` plus
`evaluations/cases/regressions/*.json` and are data-only:

- `pipeline` cases run the real Phase 9 orchestrator offline with
  deterministic fake providers (scenario implementations live in
  `evaluations/scenarios.py`; fakes in `evaluations/fakes.py`). They cover
  primary success, same-alias transient retry, recorded backup fallback,
  extractor exact-quote escalation to MiMo Pro, no escalation on semantic
  disagreement, availability-only DeepSeek third line (fully gated), Reviewer
  revision and double rejection, Analyst score rejection, prompt-injection
  resistance, a same-model Analyst/Reviewer correlated case, and explicit
  run failures.
- `mutation` cases apply adversarial mutations (altered/paraphrased
  statements, placement/entailment drift, wrong IDs, unapproved templates,
  qualified-evidence promotion, duplicate claim use, hidden prose fields) to
  a released run's synthesis and require the final validator to block each
  one. A validator escape is a failing case, never a hidden skip.
- `alias_quality` cases compare model aliases on frozen inputs offline via
  named response kinds, feeding the per-alias malformed and
  exact-quote-failure rates and the MiMo Pro versus MiMo normal quality
  delta.
- `live_comparison` runs the extractor comparison (MiMo V2.5 versus DeepSeek
  V4 Flash) on the same frozen inputs against a real endpoint. It is skipped
  with an explicit reason unless `RUN_LIVE_EVALUATIONS=1` is set, and records
  exact model aliases, pinned snapshots, and frozen-input hashes when it
  runs.

## Regression policy

Any failure discovered by an evaluation (especially a validator escape) must
be captured as a new case under `evaluations/cases/regressions/` before the
fix lands, so the corpus permanently guards against it.
`regression_altered_statement.json` seeds this directory with the historical
altered-statement failure mode.

## Non-negotiables

- Validators are never weakened or patched to make evaluations pass.
- Failing cases produce clear failure reports and a nonzero exit code.
- Normal evaluations are deterministic and fully offline.
- Routing defaults change only on evaluation evidence from this corpus (see
  the Phase 10 plan), not on external benchmark preference.
