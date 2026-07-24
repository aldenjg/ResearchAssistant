"""Frontend live-run launcher tests.

Every test here is offline: providers are injected fakes, no network call is
made, no API key is used, and no model credit is spent.
"""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from evaluations.fakes import FakeScraper, FakeSearch, StageLLM
from frontend.run_launcher import (
    LiveRunRequest,
    preflight,
    progress,
    start_run,
)

_JOIN_TIMEOUT_SECONDS = 60.0
_CLAIM = "Remote work increased productivity."


def _fake_providers(_vendor: str | None) -> tuple[StageLLM, FakeSearch, FakeScraper]:
    return StageLLM(), FakeSearch(), FakeScraper()


# ---------------------------------------------------------------------------
# Pre-flight configuration
# ---------------------------------------------------------------------------


def test_preflight_names_missing_configuration_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)

    report = preflight("brave")

    assert report.ready is False
    assert report.vendor == "brave"
    joined = " ".join(report.problems)
    assert "OPENAI_API_KEY" in joined
    assert "BRAVE_API_KEY" in joined


def test_preflight_is_ready_when_configuration_is_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")
    monkeypatch.delenv("LLM_MODEL_MAP", raising=False)

    report = preflight("brave")

    assert report.ready is True
    assert report.problems == []
    assert report.model_map
    assert "test-openai-key" not in str(report.model_dump())


def test_preflight_reports_a_malformed_model_map_as_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("BRAVE_API_KEY", "test-brave-key")
    monkeypatch.setenv("LLM_MODEL_MAP", "{not json")

    report = preflight("brave")

    assert report.ready is False
    assert any("LLM_MODEL_MAP" in problem for problem in report.problems)


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_empty_claim_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        LiveRunRequest(raw_claim="", db_path=str(tmp_path / "live.sqlite3"))


def test_zero_model_attempt_budget_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        LiveRunRequest(
            raw_claim=_CLAIM,
            db_path=str(tmp_path / "live.sqlite3"),
            max_model_attempts=0,
        )


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def test_start_run_drives_a_complete_run_with_injected_providers(tmp_path: Path) -> None:
    db_path = tmp_path / "live.sqlite3"
    handle = start_run(
        LiveRunRequest(raw_claim=_CLAIM, db_path=str(db_path)),
        provider_factory=_fake_providers,
    )
    handle.wait(_JOIN_TIMEOUT_SECONDS)

    outcome = handle.outcome()
    assert handle.is_running() is False
    assert handle.error() is None
    assert outcome is not None
    assert outcome.status == "released"
    assert outcome.rendered_brief_hash is not None
    assert outcome.run_id == handle.run_id
    assert db_path.is_file()


def test_progress_tracks_a_finished_run(tmp_path: Path) -> None:
    db_path = tmp_path / "live.sqlite3"
    handle = start_run(
        LiveRunRequest(raw_claim=_CLAIM, db_path=str(db_path)),
        provider_factory=_fake_providers,
    )
    handle.wait(_JOIN_TIMEOUT_SECONDS)

    snapshot = progress(str(db_path), handle.run_id)

    assert snapshot is not None
    assert snapshot.status == "completed"
    assert snapshot.model_attempt_count > 0
    assert snapshot.ledger_count > 0
    assert snapshot.has_validation is True
    assert snapshot.funnel_rows[0] == ("Retrieval attempts", snapshot.retrieval_count)


def test_cancelled_run_ends_as_cancelled(tmp_path: Path) -> None:
    """Cancel before the first checkpoint, which precedes the planner stage."""
    gate = threading.Event()

    def gated_providers(vendor: str | None) -> tuple[StageLLM, FakeSearch, FakeScraper]:
        gate.wait(_JOIN_TIMEOUT_SECONDS)
        return _fake_providers(vendor)

    db_path = tmp_path / "live.sqlite3"
    handle = start_run(
        LiveRunRequest(raw_claim=_CLAIM, db_path=str(db_path)),
        provider_factory=gated_providers,
    )
    handle.cancel()
    gate.set()
    handle.wait(_JOIN_TIMEOUT_SECONDS)

    outcome = handle.outcome()
    assert handle.cancel_requested() is True
    assert outcome is not None
    assert outcome.status == "cancelled"
    assert outcome.failure_reason is not None
    assert progress(str(db_path), handle.run_id).status == "cancelled"


def test_provider_construction_failure_is_surfaced_on_the_handle(tmp_path: Path) -> None:
    def failing_providers(_vendor: str | None) -> tuple[StageLLM, FakeSearch, FakeScraper]:
        raise ValueError("BRAVE_API_KEY is not set")

    handle = start_run(
        LiveRunRequest(raw_claim=_CLAIM, db_path=str(tmp_path / "live.sqlite3")),
        provider_factory=failing_providers,
    )
    handle.wait(_JOIN_TIMEOUT_SECONDS)

    assert handle.outcome() is None
    assert handle.error() is not None
    assert "BRAVE_API_KEY" in handle.error()


# ---------------------------------------------------------------------------
# Progress edge cases
# ---------------------------------------------------------------------------


def test_progress_returns_none_for_an_unknown_run(tmp_path: Path) -> None:
    db_path = tmp_path / "live.sqlite3"
    handle = start_run(
        LiveRunRequest(raw_claim=_CLAIM, db_path=str(db_path)),
        provider_factory=_fake_providers,
    )
    handle.wait(_JOIN_TIMEOUT_SECONDS)

    assert progress(str(db_path), uuid4()) is None


def test_progress_never_creates_a_database_file(tmp_path: Path) -> None:
    missing = tmp_path / "absent.sqlite3"

    assert progress(str(missing), uuid4()) is None
    assert not missing.exists()
