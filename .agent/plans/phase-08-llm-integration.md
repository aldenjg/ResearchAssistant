# Phase 8: LLM Provider and Structured Prompts

## Purpose

Implement a vendor-isolated LLM provider interface, validated per-stage model
routing configuration, and versioned structured prompts. Phase 8 defines and
validates configuration only; it executes no runtime model failover. Normal
tests use fake providers and require no network.

## Files changed

- `providers/llm.py` (new): provider protocol, routing config, generation
  settings, prompt loading/hashing, untrusted labeling, typed stage invocation,
  optional stdlib-only OpenAI-compatible adapter.
- `prompts/planner.md`, `prompts/extractor.md`, `prompts/analyst.md`,
  `prompts/reviewer.md`, `prompts/synthesizer.md` (new): versioned prompts,
  each declaring `Prompt version: <stage>-v1`.
- `agents/planner.py` (new implementation of the previously empty placeholder):
  provider-backed Planner stage with deterministic system-assigned IDs.
- `tests/test_phase8.py` (new): 28 offline tests plus one optional live
  integration test skipped unless `RUN_LLM_INTEGRATION_TESTS=1`.
- `.env.example`: documents `OPENAI_API_KEY`, `OPENAI_BASE_URL`,
  `RUN_LLM_INTEGRATION_TESTS`.
- `.agent/plans/phase-08-llm-integration.md`, `STATUS.md`, `HANDOFF.md`.

## Implementation design

- `LLMProvider` is a runtime-checkable synchronous Protocol with two methods:
  `capabilities()` and `generate(ProviderRequest) -> ProviderResponse`. All
  request/response artifacts are strict Pydantic models; non-Pydantic returns
  are rejected explicitly at the boundary.
- `invoke_stage()` performs one typed stage invocation: it validates the model
  alias against the approved registry, checks provider capabilities against
  the requested generation settings, serializes the typed input artifact,
  calls the provider, and validates the raw output text into the requested
  Pydantic output type. It records prompt version, prompt SHA-256, model
  alias, pinned model snapshot when available, input artifact IDs, start/end
  timestamps, per-attempt success/failure/retry metadata, applied temperature,
  capability notes, and token usage when available.
- Retries stay on the same model alias and occur only for timeout/transient
  provider errors or invalid model output; `StageInvocationResult` itself
  rejects attempt histories that switch aliases, so Phase 8 cannot execute
  failover even accidentally.
- Prompt templates live in `prompts/<stage>.md`. Each file declares a
  `Prompt version:` line; `load_prompt_template()` parses it and hashes the
  full file text with SHA-256. `PromptTemplate` revalidates the hash and the
  version declaration on construction.
- Stage output schemas (`PlannerLLMOutput`, `ExtractorLLMOutput`,
  `AnalystLLMOutput`, `ReviewerLLMOutput`, `SynthesizerLLMOutput`) contain no
  identifier fields and forbid extras, so the model cannot create evidence
  IDs, choose placement/Ledger scores, or approve its own claims. All IDs are
  assigned deterministically by system code after validation (see
  `agents/planner.py` `build_planner_output()` with `uuid5` derivation).
- Snapshot text is wrapped by `label_untrusted_source_text()` between explicit
  untrusted markers with a notice that embedded instructions must be ignored;
  `ExtractorStageInput`/`AnalystStageInput` reject unlabeled source text at
  construction.
- `OpenAICompatibleLLMProvider` is a stdlib-only (`urllib`) adapter for
  explicitly enabled integration runs. The API key is read from the
  environment at call time (`OPENAI_API_KEY` by default) and never stored.

## Architectural decisions

- The provider boundary transports strings (prompt text in, JSON text out);
  all typing/validation happens on our side of the boundary so no vendor SDK
  shapes leak into internal handoffs.
- Capability mismatches are handled explicitly through
  `GenerationSettings.on_unsupported`: `"error"` (default) raises
  `UnsupportedProviderParameterError`; `"omit_and_record"` omits the control
  and records a capability note on the invocation result. Silent dropping is
  impossible.
- No new dependency was added. The optional live adapter uses only the
  standard library; `python-dotenv` (already declared) loads `.env`.

## Model-routing defaults

| Stage | Primary | Backup | Third line |
|---|---|---|---|
| planner | mimo-v2.5-pro | mimo-v2.5 | deepseek-v4-pro |
| extractor | mimo-v2.5 | mimo-v2.5-pro | deepseek-v4-flash |
| analyst | mimo-v2.5-pro | mimo-v2.5 | deepseek-v4-pro |
| reviewer | mimo-v2.5 | mimo-v2.5-pro | deepseek-v4-pro |
| synthesizer | mimo-v2.5-pro | mimo-v2.5 | deepseek-v4-pro |

MiMo V2.5 Pro is reserved for high-leverage reasoning: planning the research
boundary, dual-axis evidence scoring, and brief framing are one-shot,
judgment-heavy stages where quality failures are expensive and volume is low.
MiMo V2.5 (normal) owns repeated grounded work: extraction and review run many
times per run against verbatim source text, where deterministic downstream
gates catch quote/schema errors cheaply, so the cheaper model is the right
default.

DeepSeek aliases are third-line availability fallbacks only. They exist so a
MiMo outage does not halt a run; they confer no trust. DeepSeek-produced
output passes through exactly the same Pydantic schemas, snapshot/offset
verification, Statement Reviewer approval, Ledger admission, and final
validation gates as any other output, and never bypasses deterministic or
Reviewer gates.

Per-stage generation defaults: planner 0.2, extractor 0.0, analyst 0.1,
reviewer 0.0, synthesizer 0.15.

## Acceptance criteria

- Fake providers produce valid typed outputs for all five stages.
- Invalid raw-dict, non-JSON, wrong-schema, and extra-field model responses
  are rejected by Pydantic and never become successful artifacts.
- Prompt hashes are stable across loads and change on edit.
- Invocation success, failure, and retry metadata are fully recorded.
- Reviewer input excludes forbidden fields.
- Prompt-injection text is explicitly labeled untrusted.
- Routing config enforces exactly one primary and up to two ordered
  fallbacks; unknown/duplicate/empty aliases are rejected; defaults match the
  MiMo-first table.
- Unsupported provider parameters are handled explicitly.
- Phase 8 executes no runtime failover.
- Normal tests run offline; optional integration is skipped without
  `RUN_LLM_INTEGRATION_TESTS=1`.

## Commands run and exact results

- `python -m pytest tests/test_phase8.py -q`: 28 passed, 1 skipped.
- `python -m pytest tests/test_phase1.py tests/test_phase2.py tests/test_phase3.py tests/test_phase4.py tests/test_phase5.py tests/test_phase6.py tests/test_phase7.py tests/test_phase8.py -q`:
  231 passed, 1 skipped.
- `python -m ruff check .`: all checks passed.
- `python -m ruff format --check .`: 27 files already formatted.

(Exact final counts are recorded in `STATUS.md`; the commands above were run
from the repository root without setting `PYTHONPATH`.)

## Environment note

This machine (Windows) previously checked the repository out with CRLF line
endings via `core.autocrlf=true`, which made `ruff format --check` report
whole-file pseudo-diffs. The repo-local Git config was set to
`core.autocrlf=input` and the working tree normalized back to LF; Git
confirmed the change was content-identical. `ruff` (already declared in
`pyproject.toml` dev extras) was installed into the active interpreter so the
exact verification commands run.

## Unresolved risks

- The model alias registry pins snapshot identifiers as configuration
  strings; real vendor snapshot names must be confirmed when a live endpoint
  is first used.
- The optional live adapter targets OpenAI-compatible chat-completions
  endpoints; other wire formats would need a separate adapter behind the same
  protocol.
- Prompt quality is untested against live models; Phase 10 evaluation will
  measure it.

## Phase boundary

Phase 9 (real orchestration and controlled concurrency) was NOT started. No
orchestrator wiring, runtime fallback execution, concurrency, budgets, or
restart behavior was added in Phase 8.
