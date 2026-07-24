from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from frontend.run_reader import (
    DatabaseNotFoundError,
    list_run_cards,
    load_run_detail,
)
from frontend.streamlit_app import (
    FrontendRunSummary,
    discover_fixture_runs,
    run_fixture_for_frontend,
)
from models import RunManifest, RunStatus, Stage
from orchestrator import run_fixture_pipeline
from store import init_db, insert_run

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURE_ROOT = _REPO_ROOT / "tests" / "fixtures"
_VALID_FIXTURE = _FIXTURE_ROOT / "basic_valid_run"
_INVALID_FIXTURE = _FIXTURE_ROOT / "invalid_release_run"


def test_fixture_discovery_finds_expected_fixture_runs() -> None:
    fixtures = discover_fixture_runs(_FIXTURE_ROOT)

    fixture_names = {fixture.name for fixture in fixtures}

    assert {"basic_valid_run", "invalid_release_run"} <= fixture_names
    assert ".phase6_output" not in fixture_names
    assert all(Path(fixture.path).is_dir() for fixture in fixtures)


def test_frontend_wrapper_runs_valid_fixture(tmp_path: Path) -> None:
    summary = run_fixture_for_frontend(_VALID_FIXTURE, output_dir=tmp_path / "valid")

    assert isinstance(summary, FrontendRunSummary)
    assert summary.status == "released"
    assert summary.validation.valid is True
    assert summary.validation.rendered_brief_hash is not None
    assert summary.validation.validation_artifact_hash is not None
    assert summary.final_brief is not None
    assert "Schools reported higher completion rates" in summary.final_brief
    assert summary.block_reason is None
    assert summary.counts.ledger_records >= 1
    assert summary.metadata.fixture_name == "basic_valid_run"


def test_frontend_wrapper_runs_invalid_fixture(tmp_path: Path) -> None:
    summary = run_fixture_for_frontend(_INVALID_FIXTURE, output_dir=tmp_path / "invalid")

    assert summary.status == "blocked"
    assert summary.validation.valid is False
    assert summary.validation.rendered_brief_hash is None
    assert summary.validation.validation_artifact_hash is not None
    assert summary.final_brief is None
    assert summary.block_reason is not None
    assert "altered_statement" in summary.block_reason
    assert any(error.code == "altered_statement" for error in summary.validation.errors)


def test_frontend_summary_contains_structured_display_information(tmp_path: Path) -> None:
    summary = run_fixture_for_frontend(_VALID_FIXTURE, output_dir=tmp_path / "structured")

    assert summary.run_id == "60000000-0000-0000-0000-000000000001"
    assert summary.raw_claim
    assert summary.counts.retrievals == 2
    assert summary.counts.snapshots == 2
    assert summary.counts.provisional_candidates == 2
    assert summary.counts.audit_entries == len(summary.audit_trail)
    assert summary.metadata.db_path.endswith("fixture_pipeline.sqlite3")
    assert summary.metadata.audit_path.endswith("audit.json")
    assert summary.metadata.result_path.endswith("result.json")
    assert summary.validation.validator_config_version


# ---------------------------------------------------------------------------
# Read-only run browser
# ---------------------------------------------------------------------------


def test_run_reader_lists_persisted_runs(tmp_path: Path) -> None:
    result = run_fixture_pipeline(_VALID_FIXTURE, output_dir=tmp_path / "listed")

    cards = list_run_cards(result.db_path)

    assert len(cards) == 1
    assert cards[0].run_id == str(result.run_id)
    assert cards[0].outcome == "released"
    assert cards[0].status == "completed"
    assert cards[0].ledger_count == len(result.ledger_records)


def test_run_reader_detail_reproduces_the_released_brief_hash(tmp_path: Path) -> None:
    result = run_fixture_pipeline(_VALID_FIXTURE, output_dir=tmp_path / "released")

    detail = load_run_detail(result.db_path, result.run_id)

    assert detail.outcome == "released"
    assert detail.final_brief == result.final_brief
    assert detail.recomputed_brief_hash == result.rendered_brief_hash
    assert detail.brief_hash_matches is True
    assert detail.funnel_rows[0] == ("Retrieval attempts", len(result.retrievals))
    assert detail.funnel_rows[-1] == ("Ledger records", len(result.ledger_records))


def test_run_reader_detail_reports_a_blocked_run_without_a_brief(tmp_path: Path) -> None:
    result = run_fixture_pipeline(_INVALID_FIXTURE, output_dir=tmp_path / "blocked")

    detail = load_run_detail(result.db_path, result.run_id)

    assert detail.outcome == "blocked"
    assert detail.final_brief is None
    assert detail.recomputed_brief_hash is None
    assert detail.brief_hash_matches is False
    assert detail.validation is not None
    assert any(error.code.value == "altered_statement" for error in detail.validation.errors)


def test_run_reader_tolerates_a_run_without_synthesis_or_validation(tmp_path: Path) -> None:
    db_path = str(tmp_path / "partial.sqlite3")
    init_db(db_path)
    now = datetime(2026, 7, 20, 9, 0, tzinfo=UTC)
    manifest = RunManifest(
        run_id=uuid4(),
        status=RunStatus.FAILED,
        raw_claim="A run that stopped early.",
        current_stage=Stage.SUPPORTING_RESEARCHER,
        created_at=now,
        updated_at=now,
    )
    insert_run(db_path, manifest)

    detail = load_run_detail(db_path, manifest.run_id)

    assert detail.outcome == "failed"
    assert detail.planner is None
    assert detail.synthesis is None
    assert detail.validation is None
    assert detail.final_brief is None
    assert detail.retrievals == []
    assert detail.ledger_records == []


def test_run_reader_requires_an_existing_database(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sqlite3"

    with pytest.raises(DatabaseNotFoundError):
        list_run_cards(str(missing))

    assert not missing.exists()


def test_run_reader_rejects_an_unknown_run(tmp_path: Path) -> None:
    result = run_fixture_pipeline(_VALID_FIXTURE, output_dir=tmp_path / "unknown")

    with pytest.raises(KeyError):
        load_run_detail(result.db_path, UUID("00000000-0000-0000-0000-0000000000ff"))
