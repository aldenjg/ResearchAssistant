# Phase 9: Real Orchestration and Controlled Concurrency

## Purpose

Complete the real orchestrator connecting provider-backed stages — Planner,
supporting/opposing Researchers, trusted snapshots, LLM extraction,
deterministic post-extraction filtering, Analyst, Statement Reviewer with one
possible revision, Ledger, Synthesizer, deterministic Renderer, and the final
Validator — with audited per-stage retry, ordered model-fallback routing from
the Phase 8 configuration, budgets, idempotent restarts, cancellation, and
explicit run status. All tests use fake providers and run offline.

## Files changed

- `orchestrator.py`: new live-orchestration section (`run_live`,
  `inspect_run`, `RunBudgets`, `LiveRunResult`, `RunInspection`, routed
  invocation, stage functions). The Phase 6 fixture pipeline is unchanged.
- `cli.py`: new `inspect-run <db_path> <run_id>` command for partial-run
  inspection.
- `models.py` (compatibility additions): `RunStatus.CANCELLED`;
  `StageModelAttempt` strict model for restart-safe attempt auditing.
- `store.py` (compatibility additions): insert-only `stage_model_attempts`
  table with schema-migration record 2; `update_run`;
  `insert_stage_model_attempt` / `read_stage_model_attempts_for_run`; typed
  per-run readers for retrieval attempts, snapshots, candidates, analyst
  decisions, drafts, reviews, and Ledger records (needed for restart and
  inspection).
- `agents/synthesizer.py` (compatibility addition): optional heading
  parameters on `build_synthesis_output` with unchanged defaults.
- `tests/test_phase9.py` (new): 30 offline tests.
- `.agent/plans/phase-09-orchestration.md`, `STATUS.md`, `HANDOFF.md`.

## Implementation design

- `run_live()` drives the stages sequentially with a cancellation checkpoint
  and manifest update between each stage. Every run ends in exactly one of
  four explicit states: `released`, `blocked`, `failed`, or `cancelled`, with
  the run manifest updated to COMPLETED/FAILED/CANCELLED accordingly.
- Researchers run in a `ThreadPoolExecutor` with at most two workers. Workers
  receive typed inputs, return typed `ResearcherRetrievalBatch` artifacts, and
  never touch SQLite; the coordinator persists serialized results after both
  workers finish. A test spies on `sqlite3.connect` to prove no connection is
  opened from a worker thread. A failure on either side is explicit and names
  the side; the run never continues with only one side's evidence.
- `_routed_invoke()` walks the stage's ordered aliases from the Phase 8
  routing configuration. `invoke_stage` retries the *current* alias (limit 2
  attempts) only for timeout, transient, or malformed/validation failures.
  Escalation to the next alias requires an objective recorded failure: an
  exhausted invocation, or an objective gate rejection (extractor exact-quote
  failure). Every attempt is persisted as an insert-only `StageModelAttempt`
  recording stage, work unit, model alias, pinned snapshot when available,
  route position, attempt number, status, failure reason, retry reason,
  escalation reason, start/end timestamps, latency, and token metadata when
  available.
- Extractor routing behavior: `mimo-v2.5` first with one retry for retryable
  failures; escalation to `mimo-v2.5-pro` only after an objective recorded
  failure (repeated schema failure or exact-quote failure detected by running
  the deterministic post-extraction filter as a gate); `deepseek-v4-flash` is
  reachable only when the previous alias failed for availability
  (timeout/transient) reasons — quality failures never reach the third line.
- Restart is checkpointed on persisted artifacts: an existing planner output,
  retrieval attempts/snapshots, provisional extractions/candidates, per-quote
  analyst decisions/drafts/reviews/Ledger records, synthesis, and validation
  are all reused instead of re-invoking models. Deterministic uuid5 IDs for
  drafts, approvals, Ledger claims, retrieval attempts, and snapshots plus
  read-and-compare persistence make reruns produce no duplicate rows.
- Budgets: `RunBudgets.max_model_attempts` (persisted attempt count survives
  restarts and is re-loaded on resume) and `max_retrieval_attempts`, which
  must cover the full 18 balanced attempts — the orchestrator refuses to run
  either side at reduced depth, preserving retrieval parity.
- `inspect_run()` returns a typed `RunInspection` of a possibly partial run;
  `python cli.py inspect-run <db> <run_id>` prints it.

## Exact retry/fallback policy and objective escalation reasons

1. Attempt the stage's primary alias; retry it once if the failure is a
   timeout, a transient provider error, or Pydantic-invalid model output.
2. Escalate to the next alias only with a recorded escalation reason:
   - `objective invocation failure on <alias>: <reason>` when both attempts
     on the current alias failed;
   - `objective: exact-quote failure ...` when the extractor's schema-valid
     quotations all failed the deterministic snapshot filter for quote/bracket
     reasons.
3. The extractor's third alias (`deepseek-v4-flash`) is availability-only: it
   is used solely after timeout/transient exhaustion of `mimo-v2.5-pro`.
4. If the route is exhausted, the work unit fails explicitly; stage-level
   policy decides whether the run fails (planner/synthesizer, or no
   candidates / no Ledger records) or continues (per-snapshot,
   per-candidate).

## Why semantic disagreement never causes a provider switch

An empty extraction, a low Analyst score, or a Reviewer rejection is a
semantic judgment, not a malfunction. Switching models on semantic output
would let the orchestrator shop for an agreeable model, undermining the
independent-gate design. The gate function therefore returns an escalation
reason only for objective quote/bracket verification failures, and Reviewer
or Analyst rejections simply drop the candidate through the normal deterministic
path (tested by `test_no_escalation_on_semantic_disagreement_alone`).

## How model-attempt history remains idempotent across restart

Attempt records are insert-only rows keyed by random `attempt_id`; a restart
never rewrites or deletes them. On resume the orchestrator reloads the
persisted attempt count into the budget tracker and reuses persisted stage
artifacts, so completed stages are not re-invoked (no phantom attempts) and
new attempts append after the preserved history
(`test_restart_after_failure_resumes_without_duplicates`,
`test_fallback_attempt_metadata_is_complete_and_restart_safe`).

## Architectural decisions

- Truth gates are unchanged: every model output — including any
  DeepSeek-produced output — passes strict Pydantic schemas, the
  deterministic snapshot/offset filter, Analyst score policy, the
  Statement Reviewer (one revision maximum), Ledger admission
  re-verification, and the final release validator. Blocked releases carry no
  rendered hash (enforced by `ValidationResult` and `LiveRunResult`).
- A run with zero admissible candidates or zero approved Ledger statements
  fails explicitly instead of releasing an empty brief.
- Known crash-window limitation: per-candidate artifacts are persisted at the
  candidate's terminal outcome; a crash between the review insert and the
  Ledger insert is recovered on restart with a fresh analyst invocation to
  recover entailment, with all deterministic admission gates still applied.
- No async was introduced; concurrency remains two synchronous researcher
  workers, matching the architecture.

## Acceptance criteria and exact test results

`python -m pytest tests/test_phase9.py -q`: **30 passed**.

Covered: successful full orchestration; deterministic release hash; one/both
researcher failures; partial retrieval success; extraction failure; analyst
failure; reviewer first-failure-then-approval; reviewer second failure (and
all-rejected run failure); validator rejection blocking release without a
hash; primary transient retry before fallback; malformed-output recorded
fallback; extractor objective escalation to MiMo Pro; no escalation on
semantic disagreement; third-line DeepSeek availability-only and fully gated;
complete restart-safe fallback metadata; model and retrieval budgets
exceeded; restart after failure without duplicates; duplicate retry
idempotency; cancellation between stages plus clean resume; database
reopening; no shared SQLite connections across workers; equal retrieval
budgets; explicit status for every run; CLI inspection; and regression tests
for the `StageModelAttempt`/store compatibility additions.

## Commands run and exact results

- `python -m pytest tests/test_phase9.py -q`: 30 passed.
- `python -m pytest tests/test_phase1.py tests/test_phase2.py tests/test_phase3.py tests/test_phase4.py tests/test_phase5.py tests/test_phase6.py tests/test_phase7.py tests/test_phase8.py tests/test_phase9.py -q`:
  261 passed, 1 skipped (optional Phase 8 live integration).
- `python -m ruff check .`: all checks passed.
- `python -m ruff format --check .`: 28 files already formatted.
- `python -m pytest -q` (full suite): 267 passed, 1 skipped.

## Unresolved risks

- There is still no production search/scraper vendor adapter (intentionally,
  per the Phase 9 forbidden list), so `run_live` cannot yet be driven
  end-to-end from the CLI against the live web.
- Availability-versus-quality classification of provider failures relies on
  typed exception classes; a vendor adapter that misclassifies errors would
  skew escalation behavior.
- The reviewer and analyst can be served by the same model family when
  fallbacks activate; correlated-error measurement is Phase 10 work.

## Phase boundary

Phase 10 (evaluation and adversarial testing) was NOT started. No evaluation
corpus, metrics, or production UI was added.
