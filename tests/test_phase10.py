"""Phase 10 tests: offline evaluation and adversarial testing framework.

All tests run offline with fake providers. The full shipped corpus is
executed once per module through a shared fixture; determinism and failure
handling are exercised through additional targeted corpus runs.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

import agents.renderer
from agents.renderer import VALIDATOR_CONFIG_VERSION, validate_final_release
from evaluations.fakes import RESPONSE_KINDS, STANDARD_PAGE_TEXT, SingleResponseLLM
from evaluations.run_evaluations import (
    CASES_DIR,
    REQUIRED_METRIC_FIELDS,
    load_corpus,
    main,
    render_summary,
    run_corpus,
    write_outputs,
)
from utils import compute_sha256

_ORIGINAL_VALIDATOR = validate_final_release


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _guard(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network access attempted during offline Phase 10 tests")

    monkeypatch.setattr(socket.socket, "connect", _guard)


@pytest.fixture(scope="module")
def corpus_results() -> dict:
    """Execute the full shipped corpus once, with its own socket guard."""
    original_connect = socket.socket.connect

    def _guard(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network access attempted during offline evaluation run")

    socket.socket.connect = _guard  # type: ignore[method-assign]
    try:
        return run_corpus(CASES_DIR)
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]


def write_case(directory: Path, case: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{case['case_id']}.json").write_text(json.dumps(case, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Runner execution and outputs
# ---------------------------------------------------------------------------


def test_evaluation_runner_executes_offline(corpus_results: dict) -> None:
    summary = corpus_results["summary"]
    assert summary["total_cases"] == 28
    assert summary["failed"] == 0
    assert summary["all_passed"] is True
    # The only skip is the explicitly configured live comparison.
    skipped = [case for case in corpus_results["cases"] if case["status"] == "skipped"]
    assert [case["case_id"] for case in skipped] == ["live_extractor_comparison"]
    assert "skipped_reason" in skipped[0]


def test_machine_readable_output_produced(corpus_results: dict, tmp_path: Path) -> None:
    results_path, summary_path = write_outputs(corpus_results, tmp_path)
    parsed = json.loads(results_path.read_text(encoding="utf-8"))
    assert parsed["corpus_version"] == corpus_results["corpus_version"]
    assert parsed["summary"] == corpus_results["summary"]
    assert summary_path.is_file()


def test_human_readable_summary_produced_and_agrees(corpus_results: dict) -> None:
    summary_text = render_summary(corpus_results)
    counts = corpus_results["summary"]
    assert f"{counts['total_cases']} total" in summary_text
    assert f"{counts['passed']} passed" in summary_text
    assert f"{counts['failed']} failed" in summary_text
    assert "Overall: PASS" in summary_text
    for key in REQUIRED_METRIC_FIELDS:
        assert key in summary_text
    # The summary is a pure function of the machine-readable results.
    assert render_summary(corpus_results) == summary_text


def test_metrics_include_required_fields(corpus_results: dict) -> None:
    metrics = corpus_results["metrics"]
    missing = [key for key in REQUIRED_METRIC_FIELDS if key not in metrics]
    assert not missing, f"missing metrics: {missing}"
    assert corpus_results["metrics"]["citation_accuracy"] == 1.0
    assert corpus_results["metrics"]["snapshot_integrity"] == 1.0
    assert corpus_results["metrics"]["bracket_accuracy"] == 1.0
    assert corpus_results["metrics"]["placement_consistency"] == 1.0
    assert corpus_results["metrics"]["retrieval_parity"] == 1.0


def test_script_exits_appropriately(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    passing_dir = tmp_path / "passing"
    write_case(
        passing_dir,
        {
            "case_id": "mutation_smoke",
            "kind": "mutation",
            "attack": "altered_statement",
            "expected": "blocked",
        },
    )
    exit_code = main(["--cases-dir", str(passing_dir), "--output-dir", str(tmp_path / "out")])
    assert exit_code == 0
    assert (tmp_path / "out" / "evaluation_results.json").is_file()
    assert (tmp_path / "out" / "evaluation_summary.txt").is_file()
    capsys.readouterr()

    failing_dir = tmp_path / "failing"
    write_case(
        failing_dir,
        {
            "case_id": "expect_wrong_status",
            "kind": "pipeline",
            "scenario": "primary_success",
            "expected_status": "blocked",
        },
    )
    exit_code = main(["--cases-dir", str(failing_dir), "--output-dir", str(tmp_path / "out2")])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "FAILING CASES" in captured.out
    assert "expect_wrong_status" in captured.out


def test_output_deterministic_for_same_corpus(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    write_case(
        corpus_dir,
        {
            "case_id": "pipeline_det",
            "kind": "pipeline",
            "scenario": "backup_fallback",
            "expected_status": "released",
        },
    )
    write_case(
        corpus_dir,
        {
            "case_id": "mutation_det",
            "kind": "mutation",
            "attack": "placement_drift",
            "expected": "blocked",
        },
    )
    write_case(
        corpus_dir,
        {
            "case_id": "alias_det",
            "kind": "alias_quality",
            "stage": "extractor",
            "frozen_input": "standard_page",
            "responses": {"mimo-v2.5": "good_quote", "mimo-v2.5-pro": "bad_quote"},
            "expected": {"mimo-v2.5": "pass", "mimo-v2.5-pro": "exact_quote_failure"},
        },
    )
    first = json.dumps(run_corpus(corpus_dir), sort_keys=True)
    second = json.dumps(run_corpus(corpus_dir), sort_keys=True)
    assert first == second


# ---------------------------------------------------------------------------
# Validator resistance and adversarial metrics
# ---------------------------------------------------------------------------


def test_validator_escape_rate_calculated(corpus_results: dict) -> None:
    metrics = corpus_results["metrics"]
    assert metrics["mutation_attacks_total"] == 11  # 10 attacks + 1 regression fixture
    assert metrics["mutation_attacks_blocked"] == 11
    assert metrics["validator_escape_rate"] == 0.0
    assert metrics["mutation_attack_block_rate"] == 1.0


def test_unsupported_claim_rate_calculated(corpus_results: dict) -> None:
    assert corpus_results["metrics"]["unsupported_claim_rate"] == 0.0


def test_placement_mutation_attacks_counted(corpus_results: dict) -> None:
    mutation_cases = {
        case["case_id"]: case for case in corpus_results["cases"] if case["kind"] == "mutation"
    }
    assert "mutation_placement_drift" in mutation_cases
    assert mutation_cases["mutation_placement_drift"]["blocked"] is True
    assert "ledger_mismatch" in mutation_cases["mutation_placement_drift"]["validation_error_codes"]
    for name in (
        "mutation_altered_statement",
        "mutation_qualified_promotion",
        "mutation_duplicate_claim_use",
        "mutation_hidden_prose_field",
    ):
        assert mutation_cases[name]["blocked"] is True


def test_prompt_injection_blocked_or_reported(corpus_results: dict) -> None:
    case = next(
        case for case in corpus_results["cases"] if case["case_id"] == "pipeline_prompt_injection"
    )
    assert case["status"] == "pass"
    assert corpus_results["metrics"]["prompt_injection_resistance"] == 1.0


def test_failing_cases_not_silently_skipped(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    write_case(
        corpus_dir,
        {
            "case_id": "must_fail_visibly",
            "kind": "pipeline",
            "scenario": "primary_success",
            "expected_status": "failed",  # deliberately wrong expectation
        },
    )
    results = run_corpus(corpus_dir)
    case = results["cases"][0]
    assert case["status"] == "fail"
    assert "failure_report" in case
    assert results["summary"]["failed"] == 1
    assert results["summary"]["skipped"] == 0
    assert results["summary"]["all_passed"] is False
    assert "must_fail_visibly" in results["summary"]["failing_case_ids"]


def test_discovered_failures_become_regression_fixtures(corpus_results: dict) -> None:
    regressions_dir = CASES_DIR / "regressions"
    assert regressions_dir.is_dir()
    regression_ids = {
        json.loads(path.read_text(encoding="utf-8"))["case_id"]
        for path in regressions_dir.glob("*.json")
    }
    assert "regression_altered_statement" in regression_ids
    executed_ids = {case["case_id"] for case in corpus_results["cases"]}
    assert regression_ids <= executed_ids  # regression fixtures are always executed


def test_evaluation_does_not_weaken_validators(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    write_case(
        corpus_dir,
        {
            "case_id": "mutation_probe",
            "kind": "mutation",
            "attack": "altered_statement",
            "expected": "blocked",
        },
    )
    results = run_corpus(corpus_dir)
    assert results["summary"]["all_passed"] is True
    assert agents.renderer.validate_final_release is _ORIGINAL_VALIDATOR
    assert agents.renderer.VALIDATOR_CONFIG_VERSION == VALIDATOR_CONFIG_VERSION
    assert VALIDATOR_CONFIG_VERSION == "phase5-release-validator-v1"


# ---------------------------------------------------------------------------
# Route, alias, correlated-error, and cost metrics
# ---------------------------------------------------------------------------


def test_route_metrics_present_and_internally_consistent(corpus_results: dict) -> None:
    route_metrics = corpus_results["route_metrics"]
    assert {"planner", "extractor", "analyst", "reviewer", "synthesizer"} <= set(route_metrics)
    for stage, counts in route_metrics.items():
        assert counts["attempts"] == counts["successes"] + counts["failures"], stage
        assert counts["attempts"] == counts["primary_attempts"] + counts["fallback_attempts"]
        assert counts["primary_successes"] <= counts["primary_attempts"]
        for rate_key in ("primary_success_rate", "retry_rate", "fallback_rate"):
            assert 0.0 <= counts[rate_key] <= 1.0, (stage, rate_key)


def test_offline_cases_cover_primary_retry_backup_and_third_line_paths(
    corpus_results: dict,
) -> None:
    case_ids = {case["case_id"] for case in corpus_results["cases"]}
    assert {
        "pipeline_primary_success",
        "pipeline_transient_retry",
        "pipeline_backup_fallback",
        "pipeline_third_line_availability",
    } <= case_ids
    extractor = corpus_results["route_metrics"]["extractor"]
    assert extractor["retries"] > 0
    assert extractor["fallback_attempts"] > 0
    statuses = {
        case["case_id"]: case["status"]
        for case in corpus_results["cases"]
        if case["kind"] == "pipeline"
    }
    assert all(status == "pass" for status in statuses.values())


def test_fallback_never_bypasses_required_gates(corpus_results: dict) -> None:
    assert corpus_results["metrics"]["fallback_gate_violations"] == 0
    third_line_case = next(
        case
        for case in corpus_results["cases"]
        if case["case_id"] == "pipeline_third_line_availability"
    )
    assert third_line_case["status"] == "pass"
    # The gated third-line run released only the other snapshot's evidence.
    assert third_line_case["ledger_count"] == 1


def test_alias_metrics_and_quality_delta(corpus_results: dict) -> None:
    alias_metrics = corpus_results["alias_metrics"]
    assert alias_metrics["mimo-v2.5-pro"]["pass_rate"] == 1.0
    assert alias_metrics["mimo-v2.5"]["exact_quote_failure_rate"] > 0.0
    assert alias_metrics["deepseek-v4-flash"]["malformed_rate"] > 0.0
    delta = corpus_results["metrics"]["quality_delta_pro_minus_normal"]
    assert delta == pytest.approx(
        alias_metrics["mimo-v2.5-pro"]["pass_rate"] - alias_metrics["mimo-v2.5"]["pass_rate"]
    )


def test_same_model_correlated_error_cases_reported(corpus_results: dict) -> None:
    correlated = corpus_results["correlated_error_cases"]
    assert correlated, "expected the reviewer-fallback scenario to report a correlated case"
    scenarios = {entry["scenario"] for entry in correlated}
    assert "pipeline_reviewer_fallback_same_model" in scenarios
    summary_text = render_summary(corpus_results)
    assert "correlated-error cases: 1" in summary_text


def test_cost_calculations_agree_with_recorded_usage_and_pricing(
    corpus_results: dict,
) -> None:
    costs = corpus_results["costs"]
    pricing = costs["pricing_per_1k_tokens"]
    expected_total = 0.0
    for alias, usage in costs["token_usage_by_alias"].items():
        expected_total += (usage["input_tokens"] / 1000.0) * pricing[alias]["input"]
        expected_total += (usage["output_tokens"] / 1000.0) * pricing[alias]["output"]
    assert costs["total_cost_usd"] == pytest.approx(expected_total, abs=1e-6)
    assert costs["cost_per_completed_run_usd"] == pytest.approx(
        costs["total_cost_usd"] / costs["completed_runs"], abs=1e-6
    )
    assert costs["cost_per_successful_artifact_usd"] == pytest.approx(
        costs["total_cost_usd"] / costs["successful_artifacts"], abs=1e-6
    )


# ---------------------------------------------------------------------------
# Optional live comparison
# ---------------------------------------------------------------------------


def test_optional_live_comparison_skipped_unless_configured(
    corpus_results: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RUN_LIVE_EVALUATIONS", raising=False)
    live = corpus_results["live_comparison"]
    assert live["enabled"] is False
    assert "skipped_reason" in live


def test_live_comparison_uses_frozen_inputs_and_records_aliases(
    corpus_results: dict, tmp_path: Path
) -> None:
    corpus_dir = tmp_path / "corpus"
    write_case(
        corpus_dir,
        {
            "case_id": "live_probe",
            "kind": "live_comparison",
            "stage": "extractor",
            "frozen_input": "standard_page",
            "aliases": ["mimo-v2.5", "deepseek-v4-flash"],
        },
    )
    fake_live = SingleResponseLLM(RESPONSE_KINDS["good_quote"])
    results = run_corpus(corpus_dir, live_provider=fake_live)
    live = results["live_comparison"]
    assert live["enabled"] is True
    comparisons = live["results"]
    assert [entry["model_alias"] for entry in comparisons] == [
        "mimo-v2.5",
        "deepseek-v4-flash",
    ]
    frozen_sha = compute_sha256(STANDARD_PAGE_TEXT)
    for entry in comparisons:
        assert entry["frozen_input_sha256"] == frozen_sha
        assert entry["outcome"] == "pass"
    assert comparisons[0]["pinned_model_snapshot"] == "mimo-v2.5-2026-05-12"
    assert comparisons[1]["pinned_model_snapshot"] is None
    # The live comparison used exactly the same frozen input as the offline
    # alias-quality corpus.
    offline_case = next(
        case for case in corpus_results["cases"] if case["case_id"] == "alias_quality_all_good"
    )
    assert offline_case["frozen_input_sha256"] == frozen_sha


def test_corpus_loader_rejects_malformed_cases(tmp_path: Path) -> None:
    corpus_dir = tmp_path / "corpus"
    write_case(corpus_dir, {"case_id": "a", "kind": "mutation", "attack": "placement_drift"})
    (corpus_dir / "broken.json").write_text('{"kind": "pipeline"}', encoding="utf-8")
    with pytest.raises(ValueError, match="missing required key"):
        load_corpus(corpus_dir)
    with pytest.raises(FileNotFoundError):
        load_corpus(tmp_path / "empty")
