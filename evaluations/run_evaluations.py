"""Offline evaluation and adversarial testing runner (Phase 10).

Runs a deterministic corpus of pipeline scenarios, validator mutation
attacks, and frozen per-alias quality cases against the real orchestrator
with fake providers, then writes a machine-readable JSON result and a
human-readable summary derived from the same data.

Normal evaluations are fully offline. The optional live model comparison is
skipped unless RUN_LIVE_EVALUATIONS=1 (or a provider is injected in tests),
and its skip is reported, never hidden.

Usage (from the repository root):

    python evaluations/run_evaluations.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid5

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from agents.renderer import validate_final_release  # noqa: E402
from agents.researcher import (  # noqa: E402
    assemble_quote_block,
    build_source_snapshot,
    filter_provisional_candidate,
    parse_extracted_quote_block,
    validate_bracket_context,
    validate_snapshot_integrity,
)
from evaluations.fakes import (  # noqa: E402
    CLAIM,
    FABRICATED_QUOTE_MARKER,
    FROZEN_INPUTS,
    INJECTION_SENTENCE,
    RESPONSE_KINDS,
    SeqClock,
    SingleResponseLLM,
)
from evaluations.scenarios import (  # noqa: E402
    MUTATION_ATTACKS,
    ScenarioRun,
    build_scenario,
)
from models import ProvisionalCandidate, SourceSnapshot, Stance  # noqa: E402
from orchestrator import run_live  # noqa: E402
from providers.llm import (  # noqa: E402
    KNOWN_MODEL_ALIASES,
    ExtractorLLMOutput,
    ExtractorStageInput,
    LLMStage,
    OpenAICompatibleLLMProvider,
    invoke_stage,
    label_untrusted_source_text,
    load_prompt_template,
)
from store import (  # noqa: E402
    read_analyst_decisions_for_run,
    read_ledger_records_for_run,
    read_planner_output,
    read_retrieval_attempts_for_run,
    read_run,
    read_snapshots_for_run,
    read_stage_model_attempts_for_run,
    read_statement_reviews_for_run,
    read_synthesis,
)
from utils import URL_NAMESPACE, compute_sha256  # noqa: E402

CORPUS_VERSION = "phase10-corpus-v1"
LIVE_EVALUATIONS_ENV_VAR = "RUN_LIVE_EVALUATIONS"
CASES_DIR = Path(__file__).resolve().parent / "cases"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
RESULTS_FILE_NAME = "evaluation_results.json"
SUMMARY_FILE_NAME = "evaluation_summary.txt"

CLAIM_KEYWORDS = ("remote", "work", "productivity", "increased")

# Configured pricing (USD per 1K tokens: input, output) used for cost metrics.
PRICING_PER_1K: dict[str, tuple[float, float]] = {
    "mimo-v2.5-pro": (0.004, 0.016),
    "mimo-v2.5": (0.001, 0.004),
    "deepseek-v4-pro": (0.002, 0.008),
    "deepseek-v4-flash": (0.0005, 0.002),
}

_QUOTE_FAILURE_MARKERS = (
    "does not appear in snapshot",
    "bracket",
    "segment offsets",
    'must match [context] "segments" [context]',
)

REQUIRED_METRIC_FIELDS = (
    "citation_accuracy",
    "snapshot_integrity",
    "bracket_accuracy",
    "unsupported_claim_rate",
    "validator_escape_rate",
    "mutation_attack_block_rate",
    "placement_consistency",
    "score_separation_rate",
    "reviewer_rejection_rate",
    "analyst_rejection_rate",
    "retrieval_parity",
    "prompt_injection_resistance",
    "completion_time_seconds_avg",
    "completion_time_seconds_max",
    "quality_delta_pro_minus_normal",
    "fallback_gate_violations",
)


def live_evaluations_enabled() -> bool:
    return os.environ.get(LIVE_EVALUATIONS_ENV_VAR) == "1"


def load_corpus(cases_dir: Path | str = CASES_DIR) -> list[dict]:
    directory = Path(cases_dir)
    paths = sorted(directory.glob("*.json")) + sorted(directory.glob("regressions/*.json"))
    if not paths:
        raise FileNotFoundError(f"no evaluation cases found in {directory}")
    cases = []
    seen: set[str] = set()
    for path in paths:
        case = json.loads(path.read_text(encoding="utf-8"))
        for key in ("case_id", "kind"):
            if key not in case:
                raise ValueError(f"case file {path.name} is missing required key {key!r}")
        if case["case_id"] in seen:
            raise ValueError(f"duplicate case_id: {case['case_id']}")
        seen.add(case["case_id"])
        cases.append(case)
    return sorted(cases, key=lambda case: case["case_id"])


def _case_run_id(case_id: str):
    return uuid5(URL_NAMESPACE, f"phase10-run::{case_id}")


def _frozen_snapshot(case_id: str, frozen_input: str, clock: SeqClock) -> SourceSnapshot:
    run_id = _case_run_id(case_id)
    return build_source_snapshot(
        run_id=run_id,
        retrieval_attempt_id=uuid5(URL_NAMESPACE, f"phase10-frozen-retrieval::{case_id}"),
        snapshot_id=uuid5(URL_NAMESPACE, f"phase10-frozen-snapshot::{case_id}"),
        source_url=f"https://frozen.example.com/{case_id}",
        retrieved_at=clock(),
        normalized_text=FROZEN_INPUTS[frozen_input],
        truncated=False,
        created_at=clock(),
    )


def _classify_extractor_outcome(
    case_id: str,
    snapshot: SourceSnapshot,
    invocation,
    clock: SeqClock,
) -> str:
    if not invocation.success or invocation.output is None:
        return "malformed"
    output = invocation.output
    assert isinstance(output, ExtractorLLMOutput)
    if not output.quote_blocks:
        return "empty"
    passes = 0
    quote_failures = 0
    for index, quote in enumerate(output.quote_blocks):
        try:
            quote_block = assemble_quote_block(snapshot, quote.segments)
        except ValueError:
            quote_failures += 1
            continue
        provisional = ProvisionalCandidate(
            run_id=snapshot.run_id,
            stance=Stance.SUPPORTING,
            source_url=snapshot.source_url,
            retrieval_attempt_id=snapshot.retrieval_attempt_id,
            query_id=uuid5(URL_NAMESPACE, f"phase10-frozen-query::{case_id}::{index}"),
            query_round=1,
            search_rank=1,
            snapshot_id=snapshot.snapshot_id,
            snapshot_sha256=snapshot.snapshot_sha256,
            extracted_quote_block=quote_block,
            extraction_prompt_version=invocation.prompt_version,
            extraction_model_name=invocation.pinned_model_snapshot or invocation.model_alias,
            extracted_at=clock(),
        )
        result = filter_provisional_candidate(
            provisional,
            snapshot,
            claim_keywords=CLAIM_KEYWORDS,
            post_filter_version="phase10-evaluation-filter-v1",
            post_filter_validated_at=clock(),
        )
        if result.valid:
            passes += 1
        elif result.rejection_message is not None and any(
            marker in result.rejection_message for marker in _QUOTE_FAILURE_MARKERS
        ):
            quote_failures += 1
    if passes > 0:
        return "pass"
    if quote_failures > 0:
        return "exact_quote_failure"
    return "filter_rejected"


def _invoke_frozen_extractor(case_id: str, snapshot: SourceSnapshot, provider, alias: str):
    clock = SeqClock()
    stage_input = ExtractorStageInput(
        run_id=snapshot.run_id,
        stance=Stance.SUPPORTING,
        snapshot_id=snapshot.snapshot_id,
        claim_text=CLAIM,
        labeled_snapshot_text=label_untrusted_source_text(snapshot.normalized_text),
        truncated=snapshot.truncated,
    )
    return invoke_stage(
        provider,
        run_id=snapshot.run_id,
        stage=LLMStage.EXTRACTOR,
        prompt=load_prompt_template(LLMStage.EXTRACTOR),
        input_artifact=stage_input,
        input_artifact_ids=(snapshot.snapshot_id,),
        output_type=ExtractorLLMOutput,
        model_alias=alias,
        max_attempts=1,
        clock=clock,
    )


class EvaluationEngine:
    def __init__(self, work_dir: Path, live_provider: object | None = None) -> None:
        self.work_dir = work_dir
        self.live_provider = live_provider
        self.pipeline_runs: dict[str, ScenarioRun] = {}
        self.case_results: list[dict] = []
        self.mutation_total = 0
        self.mutation_blocked = 0
        self.alias_outcomes: dict[str, dict[str, int]] = {}
        self.injection_total = 0
        self.injection_passed = 0
        self.live_results: list[dict] = []

    # -- case execution -----------------------------------------------------

    def run_case(self, case: dict) -> dict:
        kind = case["kind"]
        if kind == "pipeline":
            record = self._run_pipeline_case(case)
        elif kind == "mutation":
            record = self._run_mutation_case(case)
        elif kind == "alias_quality":
            record = self._run_alias_quality_case(case)
        elif kind == "live_comparison":
            record = self._run_live_comparison_case(case)
        else:
            record = {
                "case_id": case["case_id"],
                "kind": kind,
                "status": "fail",
                "failure_report": f"unknown case kind: {kind}",
            }
        self.case_results.append(record)
        return record

    def _execute_scenario(self, scenario_key: str, scenario_name: str) -> ScenarioRun:
        if scenario_key in self.pipeline_runs:
            return self.pipeline_runs[scenario_key]
        scenario = build_scenario(scenario_name)
        llm = scenario.llm_factory()
        db_path = str(self.work_dir / f"{scenario_key}.sqlite3")
        result = run_live(
            raw_claim=CLAIM,
            db_path=db_path,
            llm_provider=llm,
            search_provider=scenario.search_factory(),
            scraper_provider=scenario.scraper_factory(),
            run_id=_case_run_id(scenario_key),
            clock=SeqClock(),
        )
        attempts = read_stage_model_attempts_for_run(db_path, result.run_id)
        run = ScenarioRun(result=result, db_path=db_path, llm=llm, attempts=attempts)
        self.pipeline_runs[scenario_key] = run
        return run

    def _run_pipeline_case(self, case: dict) -> dict:
        case_id = case["case_id"]
        scenario_name = case["scenario"]
        scenario = build_scenario(scenario_name)
        run = self._execute_scenario(case_id, scenario_name)
        problems: list[str] = []
        expected = case.get("expected_status", scenario.expected_status)
        if run.result.status != expected:
            problems.append(
                f"expected status {expected!r}, observed {run.result.status!r} "
                f"(reason: {run.result.failure_reason})"
            )
        for check in scenario.checks:
            problems.extend(check(run))
        if scenario_name == "prompt_injection":
            self.injection_total += 1
            if not problems:
                self.injection_passed += 1
        record = {
            "case_id": case_id,
            "kind": "pipeline",
            "scenario": scenario_name,
            "status": "fail" if problems else "pass",
            "expected_status": expected,
            "observed_status": run.result.status,
            "ledger_count": run.result.ledger_count,
        }
        if problems:
            record["failure_report"] = "; ".join(problems)
        return record

    def _run_mutation_case(self, case: dict) -> dict:
        case_id = case["case_id"]
        attack_name = case["attack"]
        attack = MUTATION_ATTACKS.get(attack_name)
        if attack is None:
            return {
                "case_id": case_id,
                "kind": "mutation",
                "status": "fail",
                "failure_report": f"unknown mutation attack: {attack_name}",
            }
        base = self._execute_scenario("mutation_base", "mutation_base")
        if base.result.status != "released":
            return {
                "case_id": case_id,
                "kind": "mutation",
                "status": "fail",
                "failure_report": (
                    "mutation base run did not release: "
                    f"{base.result.status} ({base.result.failure_reason})"
                ),
            }
        synthesis = read_synthesis(base.db_path, base.result.run_id)
        ledgers = read_ledger_records_for_run(base.db_path, base.result.run_id)
        mutated = attack(synthesis, ledgers)
        validation = validate_final_release(
            mutated,
            ledgers,
            validated_at=datetime(2026, 7, 11, tzinfo=UTC),
        )
        self.mutation_total += 1
        blocked = not validation.valid
        if blocked:
            self.mutation_blocked += 1
        record = {
            "case_id": case_id,
            "kind": "mutation",
            "attack": attack_name,
            "status": "pass" if blocked else "fail",
            "blocked": blocked,
            "validation_error_codes": sorted({error.code.value for error in validation.errors}),
        }
        if not blocked:
            record["failure_report"] = (
                f"VALIDATOR ESCAPE: mutation {attack_name!r} passed final validation"
            )
        return record

    def _run_alias_quality_case(self, case: dict) -> dict:
        case_id = case["case_id"]
        clock = SeqClock()
        snapshot = _frozen_snapshot(case_id, case["frozen_input"], clock)
        problems: list[str] = []
        observed: dict[str, str] = {}
        for alias in sorted(case["responses"]):
            kind = case["responses"][alias]
            provider = SingleResponseLLM(RESPONSE_KINDS[kind])
            invocation = _invoke_frozen_extractor(case_id, snapshot, provider, alias)
            outcome = _classify_extractor_outcome(case_id, snapshot, invocation, clock)
            observed[alias] = outcome
            expected = case["expected"][alias]
            if outcome != expected:
                problems.append(f"{alias}: expected {expected!r}, observed {outcome!r}")
            counts = self.alias_outcomes.setdefault(
                alias, {"attempts": 0, "pass": 0, "malformed": 0, "exact_quote_failure": 0}
            )
            counts["attempts"] += 1
            if outcome in counts:
                counts[outcome] += 1
        record = {
            "case_id": case_id,
            "kind": "alias_quality",
            "status": "fail" if problems else "pass",
            "frozen_input_sha256": compute_sha256(FROZEN_INPUTS[case["frozen_input"]]),
            "observed": observed,
        }
        if problems:
            record["failure_report"] = "; ".join(problems)
        return record

    def _run_live_comparison_case(self, case: dict) -> dict:
        case_id = case["case_id"]
        if self.live_provider is None and not live_evaluations_enabled():
            return {
                "case_id": case_id,
                "kind": "live_comparison",
                "status": "skipped",
                "skipped_reason": (
                    f"live evaluation disabled; set {LIVE_EVALUATIONS_ENV_VAR}=1 to enable"
                ),
            }
        provider = self.live_provider or OpenAICompatibleLLMProvider()
        clock = SeqClock()
        snapshot = _frozen_snapshot(case_id, case["frozen_input"], clock)
        comparisons = []
        try:
            for alias in case["aliases"]:
                invocation = _invoke_frozen_extractor(case_id, snapshot, provider, alias)
                comparisons.append(
                    {
                        "model_alias": alias,
                        "pinned_model_snapshot": KNOWN_MODEL_ALIASES[alias],
                        "frozen_input_sha256": compute_sha256(FROZEN_INPUTS[case["frozen_input"]]),
                        "outcome": _classify_extractor_outcome(
                            case_id, snapshot, invocation, clock
                        ),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - live failures must be reported
            return {
                "case_id": case_id,
                "kind": "live_comparison",
                "status": "fail",
                "failure_report": f"live comparison failed: {type(exc).__name__}: {exc}",
            }
        self.live_results.extend(comparisons)
        return {
            "case_id": case_id,
            "kind": "live_comparison",
            "status": "pass",
            "comparisons": comparisons,
        }

    # -- metrics ------------------------------------------------------------

    def compute_metrics(self) -> dict:
        snapshots_total = snapshots_ok = 0
        citations_total = citations_ok = 0
        brackets_total = brackets_ok = 0
        placement_total = placement_ok = 0
        statements_total = statements_supported = 0
        decisions_total = decisions_rejected = 0
        separated_scores = 0
        reviews_total = reviews_rejected = 0
        parity_runs = parity_ok = 0
        completion_times: list[float] = []
        gate_violations = 0
        correlated_cases: list[dict] = []
        cost_total = 0.0
        completed_runs = 0
        artifacts_total = 0
        succeeded_by_alias: dict[str, dict[str, int]] = {}

        for scenario_key in sorted(self.pipeline_runs):
            run = self.pipeline_runs[scenario_key]
            db_path = run.db_path
            run_id = run.result.run_id
            snapshots = read_snapshots_for_run(db_path, run_id)
            snapshot_by_id = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
            for snapshot in snapshots:
                snapshots_total += 1
                try:
                    validate_snapshot_integrity(snapshot)
                    snapshots_ok += 1
                except ValueError:
                    pass

            ledgers = read_ledger_records_for_run(db_path, run_id)
            artifacts_total += len(ledgers)
            for record in ledgers:
                snapshot = snapshot_by_id.get(record.snapshot_id)
                citations_total += 1
                brackets_total += 1
                if snapshot is None:
                    continue
                try:
                    parsed = parse_extracted_quote_block(record.approved_claim_text)
                    segments_ok = len(parsed.segments) == len(record.segment_offsets) and all(
                        snapshot.normalized_text[offset.start_char : offset.end_char] == segment
                        for segment, offset in zip(
                            parsed.segments, record.segment_offsets, strict=True
                        )
                    )
                    if segments_ok:
                        citations_ok += 1
                    validate_bracket_context(snapshot, parsed, record.segment_offsets)
                    brackets_ok += 1
                except ValueError:
                    pass
                if FABRICATED_QUOTE_MARKER in record.approved_claim_text:
                    gate_violations += 1
                if INJECTION_SENTENCE in record.approved_factual_statement:
                    gate_violations += 1
                if record.analyst_model_name == record.reviewer_model_name:
                    correlated_cases.append(
                        {
                            "scenario": scenario_key,
                            "ledger_claim_id": str(record.ledger_claim_id),
                            "model_name": record.analyst_model_name,
                        }
                    )

            ledger_statements = {record.approved_factual_statement for record in ledgers}
            ledger_by_claim = {record.ledger_claim_id: record for record in ledgers}
            try:
                synthesis = read_synthesis(db_path, run_id)
            except KeyError:
                synthesis = None
            if synthesis is not None:
                for section in synthesis.sections:
                    for item in section.items:
                        statements_total += 1
                        if item.approved_factual_statement in ledger_statements:
                            statements_supported += 1
                        ledger = ledger_by_claim.get(item.ledger_claim_id)
                        placement_total += 1
                        if ledger is not None and item.placement == ledger.placement:
                            placement_ok += 1

            decisions = read_analyst_decisions_for_run(db_path, run_id)
            for decision in decisions:
                decisions_total += 1
                if not decision.approved:
                    decisions_rejected += 1
                if abs(decision.evidence_quality - decision.claim_fit) >= 1:
                    separated_scores += 1
            for review in read_statement_reviews_for_run(db_path, run_id):
                reviews_total += 1
                if not review.approved:
                    reviews_rejected += 1

            retrievals = read_retrieval_attempts_for_run(db_path, run_id)
            if retrievals:
                try:
                    planner = read_planner_output(db_path, run_id)
                except KeyError:
                    planner = None
                if planner is not None:
                    stance_by_query = {
                        query.query_id: query.stance.value for query in planner.search_queries
                    }
                    counts = {"supporting": 0, "opposing": 0}
                    for retrieval in retrievals:
                        stance = stance_by_query.get(retrieval.query_id)
                        if stance in counts:
                            counts[stance] += 1
                    parity_runs += 1
                    if counts["supporting"] == counts["opposing"]:
                        parity_ok += 1

            manifest = read_run(db_path, run_id)
            if manifest.completed_at is not None:
                completed_runs += 1
                completion_times.append(
                    (manifest.completed_at - manifest.created_at).total_seconds()
                )
            for attempt in run.attempts:
                if attempt.status == "succeeded" and attempt.input_tokens is not None:
                    input_price, output_price = PRICING_PER_1K[attempt.model_alias]
                    cost_total += (attempt.input_tokens / 1000.0) * input_price
                    cost_total += ((attempt.output_tokens or 0) / 1000.0) * output_price
                    usage = succeeded_by_alias.setdefault(
                        attempt.model_alias, {"attempts": 0, "input_tokens": 0, "output_tokens": 0}
                    )
                    usage["attempts"] += 1
                    usage["input_tokens"] += attempt.input_tokens
                    usage["output_tokens"] += attempt.output_tokens or 0

        def rate(numerator: int, denominator: int) -> float:
            return round(numerator / denominator, 6) if denominator else 0.0

        alias_metrics = {}
        for alias, counts in sorted(self.alias_outcomes.items()):
            alias_metrics[alias] = {
                "attempts": counts["attempts"],
                "pass": counts["pass"],
                "malformed": counts["malformed"],
                "exact_quote_failure": counts["exact_quote_failure"],
                "pass_rate": rate(counts["pass"], counts["attempts"]),
                "malformed_rate": rate(counts["malformed"], counts["attempts"]),
                "exact_quote_failure_rate": rate(counts["exact_quote_failure"], counts["attempts"]),
            }
        pro = alias_metrics.get("mimo-v2.5-pro", {}).get("pass_rate", 0.0)
        normal = alias_metrics.get("mimo-v2.5", {}).get("pass_rate", 0.0)

        metrics = {
            "citation_accuracy": rate(citations_ok, citations_total),
            "snapshot_integrity": rate(snapshots_ok, snapshots_total),
            "bracket_accuracy": rate(brackets_ok, brackets_total),
            "unsupported_claim_rate": rate(
                statements_total - statements_supported, statements_total
            ),
            "validator_escape_rate": rate(
                self.mutation_total - self.mutation_blocked, self.mutation_total
            ),
            "mutation_attack_block_rate": rate(self.mutation_blocked, self.mutation_total),
            "mutation_attacks_total": self.mutation_total,
            "mutation_attacks_blocked": self.mutation_blocked,
            "placement_consistency": rate(placement_ok, placement_total),
            "score_separation_rate": rate(separated_scores, decisions_total),
            "reviewer_rejection_rate": rate(reviews_rejected, reviews_total),
            "analyst_rejection_rate": rate(decisions_rejected, decisions_total),
            "retrieval_parity": rate(parity_ok, parity_runs),
            "prompt_injection_resistance": rate(self.injection_passed, self.injection_total),
            "completion_time_seconds_avg": (
                round(sum(completion_times) / len(completion_times), 6) if completion_times else 0.0
            ),
            "completion_time_seconds_max": (max(completion_times) if completion_times else 0.0),
            "quality_delta_pro_minus_normal": round(pro - normal, 6),
            "fallback_gate_violations": gate_violations,
        }
        costs = {
            "pricing_per_1k_tokens": {
                alias: {"input": prices[0], "output": prices[1]}
                for alias, prices in sorted(PRICING_PER_1K.items())
            },
            "total_cost_usd": round(cost_total, 6),
            "token_usage_by_alias": {
                alias: usage for alias, usage in sorted(succeeded_by_alias.items())
            },
            "completed_runs": completed_runs,
            "successful_artifacts": artifacts_total,
            "cost_per_completed_run_usd": (
                round(cost_total / completed_runs, 6) if completed_runs else 0.0
            ),
            "cost_per_successful_artifact_usd": (
                round(cost_total / artifacts_total, 6) if artifacts_total else 0.0
            ),
        }
        return {
            "metrics": metrics,
            "alias_metrics": alias_metrics,
            "route_metrics": self._route_metrics(),
            "costs": costs,
            "correlated_error_cases": sorted(
                correlated_cases, key=lambda entry: (entry["scenario"], entry["ledger_claim_id"])
            ),
        }

    def _route_metrics(self) -> dict:
        stages: dict[str, dict[str, int]] = {}
        for scenario_key in sorted(self.pipeline_runs):
            for attempt in self.pipeline_runs[scenario_key].attempts:
                stage = stages.setdefault(
                    attempt.stage,
                    {
                        "attempts": 0,
                        "successes": 0,
                        "failures": 0,
                        "retries": 0,
                        "fallback_attempts": 0,
                        "primary_attempts": 0,
                        "primary_successes": 0,
                    },
                )
                stage["attempts"] += 1
                if attempt.status == "succeeded":
                    stage["successes"] += 1
                else:
                    stage["failures"] += 1
                if attempt.attempt_number > 1:
                    stage["retries"] += 1
                if attempt.route_position > 0:
                    stage["fallback_attempts"] += 1
                else:
                    stage["primary_attempts"] += 1
                    if attempt.status == "succeeded":
                        stage["primary_successes"] += 1
        result = {}
        for stage, counts in sorted(stages.items()):
            attempts = counts["attempts"]
            result[stage] = {
                **counts,
                "primary_success_rate": (
                    round(counts["primary_successes"] / counts["primary_attempts"], 6)
                    if counts["primary_attempts"]
                    else 0.0
                ),
                "retry_rate": round(counts["retries"] / attempts, 6) if attempts else 0.0,
                "fallback_rate": (
                    round(counts["fallback_attempts"] / attempts, 6) if attempts else 0.0
                ),
            }
        return result


def run_corpus(
    cases_dir: Path | str = CASES_DIR,
    *,
    work_dir: Path | str | None = None,
    live_provider: object | None = None,
) -> dict:
    """Execute the full evaluation corpus and return the results document."""
    import tempfile

    cases = load_corpus(cases_dir)
    if work_dir is None:
        with tempfile.TemporaryDirectory(prefix="phase10-eval-") as tmp:
            return _run_corpus_in(Path(tmp), cases, live_provider)
    return _run_corpus_in(Path(work_dir), cases, live_provider)


def _run_corpus_in(work_dir: Path, cases: list[dict], live_provider: object | None) -> dict:
    engine = EvaluationEngine(work_dir, live_provider=live_provider)
    for case in cases:
        engine.run_case(case)
    analysis = engine.compute_metrics()
    statuses = [record["status"] for record in engine.case_results]
    failed_cases = [record for record in engine.case_results if record["status"] == "fail"]
    live_enabled = live_provider is not None or live_evaluations_enabled()
    results = {
        "corpus_version": CORPUS_VERSION,
        "cases": engine.case_results,
        "live_comparison": {
            "enabled": live_enabled,
            **(
                {"results": engine.live_results}
                if live_enabled
                else {
                    "skipped_reason": (
                        f"live evaluation disabled; set {LIVE_EVALUATIONS_ENV_VAR}=1 to enable"
                    )
                }
            ),
        },
        **analysis,
        "summary": {
            "total_cases": len(engine.case_results),
            "passed": statuses.count("pass"),
            "failed": statuses.count("fail"),
            "skipped": statuses.count("skipped"),
            "all_passed": not failed_cases,
            "failing_case_ids": sorted(record["case_id"] for record in failed_cases),
        },
    }
    return results


def render_summary(results: dict) -> str:
    """Human-readable summary derived only from the machine-readable results."""
    summary = results["summary"]
    metrics = results["metrics"]
    lines = [
        "Debate Research Agent System - Evaluation Summary",
        f"Corpus version: {results['corpus_version']}",
        "",
        (
            f"Cases: {summary['total_cases']} total, {summary['passed']} passed, "
            f"{summary['failed']} failed, {summary['skipped']} skipped"
        ),
        f"Overall: {'PASS' if summary['all_passed'] else 'FAIL'}",
        "",
        "Core metrics:",
    ]
    for key in REQUIRED_METRIC_FIELDS:
        lines.append(f"  {key}: {metrics[key]}")
    lines.append("")
    lines.append("Route metrics by stage:")
    for stage, counts in results["route_metrics"].items():
        lines.append(
            f"  {stage}: attempts={counts['attempts']} "
            f"primary_success_rate={counts['primary_success_rate']} "
            f"retry_rate={counts['retry_rate']} fallback_rate={counts['fallback_rate']}"
        )
    lines.append("")
    lines.append("Model alias quality (frozen extractor corpus):")
    for alias, counts in results["alias_metrics"].items():
        lines.append(
            f"  {alias}: pass_rate={counts['pass_rate']} "
            f"malformed_rate={counts['malformed_rate']} "
            f"exact_quote_failure_rate={counts['exact_quote_failure_rate']}"
        )
    lines.append("")
    costs = results["costs"]
    lines.append(
        f"Costs: total=${costs['total_cost_usd']} "
        f"per_completed_run=${costs['cost_per_completed_run_usd']} "
        f"per_successful_artifact=${costs['cost_per_successful_artifact_usd']}"
    )
    lines.append("")
    correlated = results["correlated_error_cases"]
    lines.append(f"Same-model Analyst/Reviewer correlated-error cases: {len(correlated)}")
    for entry in correlated:
        lines.append(
            f"  scenario={entry['scenario']} model={entry['model_name']} "
            f"ledger_claim={entry['ledger_claim_id']}"
        )
    lines.append("")
    live = results["live_comparison"]
    if live["enabled"]:
        lines.append(f"Live comparison: enabled ({len(live.get('results', []))} results)")
    else:
        lines.append(f"Live comparison: skipped - {live['skipped_reason']}")
    skipped = [record for record in results["cases"] if record["status"] == "skipped"]
    for record in skipped:
        lines.append(f"  skipped case {record['case_id']}: {record['skipped_reason']}")
    if not summary["all_passed"]:
        lines.append("")
        lines.append("FAILING CASES:")
        for record in results["cases"]:
            if record["status"] == "fail":
                lines.append(f"  {record['case_id']}: {record.get('failure_report', 'failed')}")
    lines.append("")
    return "\n".join(lines)


def write_outputs(results: dict, output_dir: Path | str = OUTPUT_DIR) -> tuple[Path, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    results_path = directory / RESULTS_FILE_NAME
    summary_path = directory / SUMMARY_FILE_NAME
    results_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path.write_text(render_summary(results), encoding="utf-8")
    return results_path, summary_path


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run offline Phase 10 evaluations.")
    parser.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)

    results = run_corpus(args.cases_dir)
    results_path, summary_path = write_outputs(results, args.output_dir)
    print(render_summary(results))
    print(f"machine-readable results: {results_path}")
    print(f"human-readable summary:   {summary_path}")
    return 0 if results["summary"]["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
