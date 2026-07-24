"""Read-only typed views over persisted runs.

This module never writes to the database, never calls a provider, and never
triggers a pipeline stage.  It assembles the artifacts a run already left
behind into typed models the frontend can render.  Streamlit is deliberately
not imported here so the readers stay testable headlessly.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypeVar
from uuid import UUID

from pydantic import Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.renderer import render_brief  # noqa: E402
from models import (  # noqa: E402
    CandidateQuoteBlock,
    LedgerRecord,
    PlannerOutput,
    ProvisionalCandidate,
    RetrievalRecord,
    RunManifest,
    RunStatus,
    ScoreDecision,
    SourceSnapshot,
    StageModelAttempt,
    StatementDraft,
    StatementReviewResult,
    StrictModel,
    SynthesisOutput,
    ValidationResult,
)
from store import (  # noqa: E402
    read_analyst_decisions_for_run,
    read_candidates_for_run,
    read_ledger_records_for_run,
    read_planner_output,
    read_provisional_extractions,
    read_retrieval_attempts_for_run,
    read_run,
    read_run_manifests,
    read_snapshots_for_run,
    read_stage_model_attempts_for_run,
    read_statement_drafts_for_run,
    read_statement_reviews_for_run,
    read_synthesis,
    read_validation,
)
from utils import compute_sha256  # noqa: E402

_ArtifactT = TypeVar("_ArtifactT", bound=StrictModel)

Outcome = Literal["released", "blocked", "failed", "cancelled", "running", "planned", "completed"]

OUTCOME_TONES: dict[Outcome, str] = {
    "released": "released",
    "blocked": "blocked",
    "failed": "failed",
    "cancelled": "neutral",
    "running": "running",
    "planned": "neutral",
    "completed": "neutral",
}


class DatabaseNotFoundError(FileNotFoundError):
    """Raised when a requested run database file does not exist."""


class RunCard(StrictModel):
    """Compact list entry for one persisted run."""

    run_id: str = Field(min_length=1)
    raw_claim: str = Field(min_length=1)
    outcome: Outcome
    status: str = Field(min_length=1)
    current_stage: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    completed_at: str | None = None
    ledger_count: int = Field(ge=0)


class RunDetail(StrictModel):
    """Every persisted artifact for one run, plus its deterministic brief."""

    manifest: RunManifest
    outcome: Outcome
    planner: PlannerOutput | None = None
    retrievals: list[RetrievalRecord]
    snapshots: list[SourceSnapshot]
    provisionals: list[ProvisionalCandidate]
    candidates: list[CandidateQuoteBlock]
    decisions: list[ScoreDecision]
    drafts: list[StatementDraft]
    reviews: list[StatementReviewResult]
    ledger_records: list[LedgerRecord]
    synthesis: SynthesisOutput | None = None
    validation: ValidationResult | None = None
    attempts: list[StageModelAttempt]
    final_brief: str | None = None
    recomputed_brief_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @property
    def brief_hash_matches(self) -> bool:
        """True when the re-rendered brief reproduces the validator's hash."""
        if self.validation is None or self.validation.rendered_brief_hash is None:
            return False
        return self.recomputed_brief_hash == self.validation.rendered_brief_hash

    @property
    def funnel_rows(self) -> list[tuple[str, int]]:
        return [
            ("Retrieval attempts", len(self.retrievals)),
            ("Trusted snapshots", len(self.snapshots)),
            ("Extractions", len(self.provisionals)),
            ("Gate-passing candidates", len(self.candidates)),
            ("Reviewer approvals", len([r for r in self.reviews if r.approved])),
            ("Ledger records", len(self.ledger_records)),
        ]


def require_database(db_path: str) -> str:
    """Return *db_path* if it exists; never create a database as a side effect."""
    if not Path(db_path).is_file():
        raise DatabaseNotFoundError(f"database file does not exist: {db_path}")
    return db_path


def list_run_cards(db_path: str) -> list[RunCard]:
    """Every persisted run, newest first."""
    require_database(db_path)
    cards: list[RunCard] = []
    for manifest in read_run_manifests(db_path):
        validation = _optional(read_validation, db_path, manifest.run_id)
        cards.append(
            RunCard(
                run_id=str(manifest.run_id),
                raw_claim=manifest.raw_claim,
                outcome=_outcome(manifest, validation),
                status=manifest.status.value,
                current_stage=manifest.current_stage.value,
                created_at=manifest.created_at.isoformat(),
                completed_at=(manifest.completed_at.isoformat() if manifest.completed_at else None),
                ledger_count=len(read_ledger_records_for_run(db_path, manifest.run_id)),
            )
        )
    return cards


def load_run_detail(db_path: str, run_id: UUID) -> RunDetail:
    """Assemble every persisted artifact for one run.

    Partial runs are expected: stages that never completed simply contribute
    empty collections instead of failing the whole view.
    """
    require_database(db_path)
    manifest = read_run(db_path, run_id)
    synthesis = _optional(read_synthesis, db_path, run_id)
    validation = _optional(read_validation, db_path, run_id)
    ledger_records = read_ledger_records_for_run(db_path, run_id)
    final_brief = _rendered_brief(synthesis, ledger_records, validation)

    return RunDetail(
        manifest=manifest,
        outcome=_outcome(manifest, validation),
        planner=_optional(read_planner_output, db_path, run_id),
        retrievals=read_retrieval_attempts_for_run(db_path, run_id),
        snapshots=read_snapshots_for_run(db_path, run_id),
        provisionals=read_provisional_extractions(db_path, run_id),
        candidates=read_candidates_for_run(db_path, run_id),
        decisions=read_analyst_decisions_for_run(db_path, run_id),
        drafts=read_statement_drafts_for_run(db_path, run_id),
        reviews=read_statement_reviews_for_run(db_path, run_id),
        ledger_records=ledger_records,
        synthesis=synthesis,
        validation=validation,
        attempts=read_stage_model_attempts_for_run(db_path, run_id),
        final_brief=final_brief,
        recomputed_brief_hash=compute_sha256(final_brief) if final_brief is not None else None,
    )


def _rendered_brief(
    synthesis: SynthesisOutput | None,
    ledger_records: list[LedgerRecord],
    validation: ValidationResult | None,
) -> str | None:
    """Re-render a released brief deterministically from persisted artifacts.

    Mirrors the orchestrator's completed-run reconstruction: only a run whose
    validation passed has a releasable brief, and the text is produced by the
    same renderer the validator hashed.
    """
    if synthesis is None or validation is None or not validation.valid:
        return None
    return render_brief(synthesis, ledger_records)


def _outcome(manifest: RunManifest, validation: ValidationResult | None) -> Outcome:
    if manifest.status is RunStatus.COMPLETED:
        if validation is None:
            return "completed"
        return "released" if validation.valid else "blocked"
    if manifest.status is RunStatus.FAILED:
        return "failed"
    if manifest.status is RunStatus.CANCELLED:
        return "cancelled"
    if manifest.status is RunStatus.RUNNING:
        return "running"
    return "planned"


def _optional(
    reader: Callable[[str, UUID], _ArtifactT],
    db_path: str,
    run_id: UUID,
) -> _ArtifactT | None:
    """Call a store reader that raises KeyError when its artifact is absent."""
    try:
        return reader(db_path, run_id)
    except KeyError:
        return None
