from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, TypeVar
from uuid import UUID, uuid4, uuid5

from pydantic import Field, TypeAdapter, model_validator
from pydantic import ValidationError as PydanticValidationError

from agents.analyst import (
    LedgerAdmissionRequest,
    admit_ledger_record,
    create_statement_draft,
    score_candidate,
)
from agents.opposingresearcher import retrieve_opposing
from agents.planner import build_planner_output
from agents.renderer import render_brief, validate_final_release
from agents.researcher import (
    filter_provisional_candidate,
    validate_snapshot_integrity,
)
from agents.reviewer import ReviewChecks, build_reviewer_input, review_statement
from agents.supportingresearcher import (
    TOTAL_INTENDED_ATTEMPTS,
    ResearcherRetrievalBatch,
    retrieve_supporting,
)
from agents.synthesizer import build_synthesis_output
from models import (
    CandidateBatch,
    CandidateQuoteBlock,
    Entailment,
    LedgerRecord,
    PlannerOutput,
    ProvisionalCandidate,
    RetrievalRecord,
    RunManifest,
    RunStatus,
    ScoreDecision,
    SearchQuery,
    SourceSnapshot,
    Stage,
    StageModelAttempt,
    StatementDraft,
    StatementReviewResult,
    StrictModel,
    SynthesisOutput,
    ValidationResult,
)
from providers.llm import (
    AnalystLLMOutput,
    AnalystStageInput,
    Clock,
    ExtractorLLMOutput,
    ExtractorStageInput,
    LLMProvider,
    LLMStage,
    ModelRoutingConfig,
    PlannerLLMOutput,
    PlannerStageInput,
    PromptTemplate,
    ReviewerLLMOutput,
    StageInvocationResult,
    SynthesizerLLMOutput,
    SynthesizerStageInput,
    default_model_routing,
    invoke_stage,
    label_untrusted_source_text,
    load_prompt_template,
)
from providers.scraper import RetryPolicy, ScraperProvider, ScraperProviderError
from providers.search import SearchProvider, SearchProviderError
from store import (
    init_db,
    insert_analyst_decision,
    insert_candidate,
    insert_ledger_record,
    insert_planner_output,
    insert_provisional_extraction,
    insert_retrieval_attempt,
    insert_run,
    insert_snapshot,
    insert_stage_model_attempt,
    insert_statement_draft,
    insert_statement_review,
    insert_synthesis,
    insert_validation,
    read_analyst_decision,
    read_analyst_decisions_for_run,
    read_candidate,
    read_candidates_for_run,
    read_ledger_record,
    read_ledger_records_for_run,
    read_planner_output,
    read_provisional_extractions,
    read_retrieval_attempt,
    read_retrieval_attempts_for_run,
    read_run,
    read_snapshot,
    read_snapshots_for_run,
    read_stage_model_attempts_for_run,
    read_statement_draft,
    read_statement_drafts_for_run,
    read_statement_review,
    read_statement_reviews_for_run,
    read_synthesis,
    read_validation,
    update_run,
)
from utils import URL_NAMESPACE, compute_sha256

DEFAULT_OUTPUT_DIR_NAME = ".phase6_output"
FIXTURE_DB_NAME = "fixture_pipeline.sqlite3"
AUDIT_FILE_NAME = "audit.json"
RESULT_FILE_NAME = "result.json"
POST_FILTER_VERSION = "phase6-fixture-post-filter-v1"
LEDGER_ID_VERSION = "phase6-fixture-ledger-id-v1"

_ModelT = TypeVar("_ModelT", bound=StrictModel)


class FixturePipelineError(RuntimeError):
    """Raised for malformed fixtures or unexpected fixture-pipeline failures."""


class AuditEntry(StrictModel):
    run_id: UUID
    stage: str = Field(min_length=1)
    status: Literal["loaded", "completed", "released", "blocked"]
    artifact_ref: str = Field(min_length=1)
    artifact_count: int = Field(ge=0)
    artifact_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: str = Field(min_length=1)


class FixturePipelineResult(StrictModel):
    run_id: UUID
    status: Literal["released", "blocked"]
    raw_claim: str = Field(min_length=1)
    fixture_dir: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    db_path: str = Field(min_length=1)
    audit_path: str = Field(min_length=1)
    result_path: str = Field(min_length=1)
    planner_output: PlannerOutput
    retrievals: list[RetrievalRecord]
    snapshots: list[SourceSnapshot]
    provisional_candidates: list[ProvisionalCandidate]
    candidates: list[CandidateQuoteBlock]
    candidate_batches: list[CandidateBatch]
    analyst_decisions: list[ScoreDecision]
    statement_drafts: list[StatementDraft]
    reviewer_decisions: list[StatementReviewResult]
    ledger_records: list[LedgerRecord]
    synthesis_output: SynthesisOutput
    validation_result: ValidationResult
    rendered_brief_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    final_brief: str | None = None
    audit_trail: list[AuditEntry]

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> FixturePipelineResult:
        if self.status == "released":
            if self.final_brief is None or self.rendered_brief_hash is None:
                raise ValueError("released fixture results require final brief and rendered hash")
            if not self.validation_result.valid:
                raise ValueError("released fixture results require valid validation")
        if self.status == "blocked":
            if self.final_brief is not None or self.rendered_brief_hash is not None:
                raise ValueError("blocked fixture results cannot include final brief or hash")
            if self.validation_result.valid:
                raise ValueError("blocked fixture results require invalid validation")
        return self


def run_fixture_pipeline(
    fixture_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
) -> FixturePipelineResult:
    fixture_path = Path(fixture_dir).resolve()
    if not fixture_path.is_dir():
        raise FixturePipelineError(f"fixture directory does not exist: {fixture_path}")

    raw_claim = _read_required_text(fixture_path / "raw_claim.txt").strip()
    if raw_claim == "":
        raise FixturePipelineError("raw_claim.txt must not be empty")

    planner = _load_model(fixture_path / "planner.json", PlannerOutput)
    retrievals = _load_model_list(fixture_path / "retrievals.json", RetrievalRecord)
    snapshots = _load_model_list(fixture_path / "snapshots.json", SourceSnapshot)
    provisionals = _load_model_list(
        fixture_path / "provisional_candidates.json",
        ProvisionalCandidate,
    )
    analyst_decisions = _load_model_list(
        fixture_path / "analyst_decisions.json",
        ScoreDecision,
    )
    statement_drafts = _load_model_list(
        fixture_path / "statement_drafts.json",
        StatementDraft,
    )
    reviewer_decisions = _load_model_list(
        fixture_path / "reviewer_decisions.json",
        StatementReviewResult,
    )
    synthesis = _load_model(fixture_path / "synthesis.json", SynthesisOutput)

    _validate_fixture_run_ids(
        raw_claim,
        planner,
        retrievals,
        snapshots,
        provisionals,
        analyst_decisions,
        statement_drafts,
        reviewer_decisions,
        synthesis,
    )

    output_path = (
        Path(output_dir).resolve()
        if output_dir is not None
        else fixture_path / DEFAULT_OUTPUT_DIR_NAME
    )
    output_path.mkdir(parents=True, exist_ok=True)
    db_path = output_path / FIXTURE_DB_NAME
    audit_path = output_path / AUDIT_FILE_NAME
    result_path = output_path / RESULT_FILE_NAME

    init_db(str(db_path))

    run_manifest = RunManifest(
        run_id=planner.run_id,
        status=RunStatus.COMPLETED,
        raw_claim=raw_claim,
        current_stage=Stage.FINAL_RENDERER_VALIDATOR,
        created_at=planner.planned_at,
        updated_at=synthesis.created_at,
        completed_at=synthesis.created_at,
    )
    _persist_model(
        str(db_path),
        run_manifest,
        insert_run,
        lambda: read_run(str(db_path), run_manifest.run_id),
        "run manifest",
    )
    _persist_model(
        str(db_path),
        planner,
        insert_planner_output,
        lambda: read_planner_output(str(db_path), planner.run_id),
        "planner output",
    )

    planner_queries = {query.query_id: query for query in planner.search_queries}
    _persist_retrievals(str(db_path), retrievals, planner_queries)
    _persist_snapshots(str(db_path), snapshots, retrievals)
    _persist_provisionals(str(db_path), provisionals, planner.run_id)

    candidates = _filter_candidates(planner, snapshots, provisionals)
    candidate_batches = _candidate_batches(planner.run_id, candidates, synthesis.created_at)
    for candidate in candidates:
        _persist_model(
            str(db_path),
            candidate,
            insert_candidate,
            lambda candidate=candidate: read_candidate(str(db_path), candidate.quote_block_id),
            "candidate",
        )

    for decision in analyst_decisions:
        _persist_model(
            str(db_path),
            decision,
            insert_analyst_decision,
            lambda decision=decision: read_analyst_decision(
                str(db_path),
                decision.run_id,
                decision.quote_block_id,
            ),
            "analyst decision",
        )
    for draft in statement_drafts:
        _persist_model(
            str(db_path),
            draft,
            insert_statement_draft,
            lambda draft=draft: read_statement_draft(str(db_path), draft.statement_draft_id),
            "statement draft",
        )
    for review in reviewer_decisions:
        _persist_model(
            str(db_path),
            review,
            insert_statement_review,
            lambda review=review: read_statement_review(
                str(db_path),
                review.run_id,
                review.statement_draft_id,
            ),
            "reviewer decision",
        )

    ledger_records = _admit_ledger_records(
        candidates,
        snapshots,
        analyst_decisions,
        statement_drafts,
        reviewer_decisions,
        synthesis,
    )
    for ledger in ledger_records:
        _persist_model(
            str(db_path),
            ledger,
            insert_ledger_record,
            lambda ledger=ledger: read_ledger_record(str(db_path), ledger.ledger_claim_id),
            "ledger record",
        )

    _persist_model(
        str(db_path),
        synthesis,
        insert_synthesis,
        lambda: read_synthesis(str(db_path), synthesis.run_id),
        "synthesis output",
    )

    validation = validate_final_release(
        synthesis,
        ledger_records,
        validated_at=synthesis.created_at,
    )
    _persist_model(
        str(db_path),
        validation,
        insert_validation,
        lambda: read_validation(str(db_path), validation.run_id),
        "validation result",
    )
    _assert_expected_counts(
        str(db_path),
        planner.run_id,
        retrieval_count=len(retrievals),
        snapshot_count=len(snapshots),
        provisional_count=len(provisionals),
        candidate_count=len(candidates),
        analyst_decision_count=len(analyst_decisions),
        draft_count=len(statement_drafts),
        review_count=len(reviewer_decisions),
        ledger_count=len(ledger_records),
    )

    final_brief = render_brief(synthesis, ledger_records) if validation.valid else None
    status: Literal["released", "blocked"] = "released" if validation.valid else "blocked"
    audit_trail = _build_audit_trail(
        run_id=planner.run_id,
        raw_claim=raw_claim,
        planner=planner,
        snapshots=snapshots,
        provisionals=provisionals,
        candidates=candidates,
        analyst_decisions=analyst_decisions,
        reviewer_decisions=reviewer_decisions,
        ledger_records=ledger_records,
        synthesis=synthesis,
        validation=validation,
    )
    result = FixturePipelineResult(
        run_id=planner.run_id,
        status=status,
        raw_claim=raw_claim,
        fixture_dir=str(fixture_path),
        output_dir=str(output_path),
        db_path=str(db_path),
        audit_path=str(audit_path),
        result_path=str(result_path),
        planner_output=planner,
        retrievals=retrievals,
        snapshots=snapshots,
        provisional_candidates=provisionals,
        candidates=candidates,
        candidate_batches=candidate_batches,
        analyst_decisions=analyst_decisions,
        statement_drafts=statement_drafts,
        reviewer_decisions=reviewer_decisions,
        ledger_records=ledger_records,
        synthesis_output=synthesis,
        validation_result=validation,
        rendered_brief_hash=validation.rendered_brief_hash,
        final_brief=final_brief,
        audit_trail=audit_trail,
    )

    _write_json_idempotent(
        audit_path,
        [entry.model_dump(mode="json") for entry in audit_trail],
    )
    _write_json_idempotent(result_path, result.model_dump(mode="json"))
    return result


def derive_fixture_ledger_claim_id(
    run_id: UUID,
    review: StatementReviewResult,
) -> UUID:
    if not review.approved or review.reviewer_approval_id is None:
        raise FixturePipelineError(
            "approved Reviewer decision is required for Ledger ID derivation"
        )
    if review.approved_factual_statement is None:
        raise FixturePipelineError("approved Reviewer decision is missing approved text")
    return uuid5(
        URL_NAMESPACE,
        (
            f"{LEDGER_ID_VERSION}::{run_id}::ledger::"
            f"{review.reviewer_approval_id}::{review.approved_factual_statement}"
        ),
    )


def _read_required_text(path: Path) -> str:
    if not path.is_file():
        raise FixturePipelineError(f"missing fixture file: {path}")
    return path.read_text(encoding="utf-8")


def _load_model(path: Path, model_type: type[_ModelT]) -> _ModelT:
    try:
        return model_type.model_validate_json(_read_required_text(path))
    except PydanticValidationError as exc:
        raise FixturePipelineError(f"invalid {path.name}: {exc}") from exc


def _load_model_list(path: Path, model_type: type[_ModelT]) -> list[_ModelT]:
    try:
        adapter = TypeAdapter(list[model_type])
        return adapter.validate_json(_read_required_text(path))
    except PydanticValidationError as exc:
        raise FixturePipelineError(f"invalid {path.name}: {exc}") from exc


def _validate_fixture_run_ids(
    raw_claim: str,
    planner: PlannerOutput,
    retrievals: Sequence[RetrievalRecord],
    snapshots: Sequence[SourceSnapshot],
    provisionals: Sequence[ProvisionalCandidate],
    analyst_decisions: Sequence[ScoreDecision],
    statement_drafts: Sequence[StatementDraft],
    reviewer_decisions: Sequence[StatementReviewResult],
    synthesis: SynthesisOutput,
) -> None:
    run_id = planner.run_id
    if planner.claim_definition.claim_text != raw_claim:
        raise FixturePipelineError("raw claim must match PlannerOutput claim_definition.claim_text")
    collections: tuple[tuple[str, Sequence[object]], ...] = (
        ("retrievals", retrievals),
        ("snapshots", snapshots),
        ("provisional candidates", provisionals),
        ("analyst decisions", analyst_decisions),
        ("statement drafts", statement_drafts),
        ("reviewer decisions", reviewer_decisions),
    )
    for label, artifacts in collections:
        for index, artifact in enumerate(artifacts):
            artifact_run_id = getattr(artifact, "run_id", None)
            if artifact_run_id != run_id:
                raise FixturePipelineError(f"{label}[{index}] run_id does not match planner")
    if synthesis.run_id != run_id:
        raise FixturePipelineError("SynthesisOutput run_id does not match planner")


def _persist_retrievals(
    db_path: str,
    retrievals: Sequence[RetrievalRecord],
    planner_queries: dict[UUID, SearchQuery],
) -> None:
    for retrieval in retrievals:
        query = planner_queries.get(retrieval.query_id)
        if query is None:
            raise FixturePipelineError("retrieval references an unknown planner query")
        if retrieval.query_round != query.query_round:
            raise FixturePipelineError("retrieval query_round does not match planner query")
        if retrieval.query_text != query.query_text:
            raise FixturePipelineError("retrieval query_text does not match planner query")
        _persist_model(
            db_path,
            retrieval,
            insert_retrieval_attempt,
            lambda retrieval=retrieval: read_retrieval_attempt(
                db_path,
                retrieval.retrieval_attempt_id,
            ),
            "retrieval attempt",
        )


def _persist_snapshots(
    db_path: str,
    snapshots: Sequence[SourceSnapshot],
    retrievals: Sequence[RetrievalRecord],
) -> None:
    retrieval_by_id = {retrieval.retrieval_attempt_id: retrieval for retrieval in retrievals}
    for snapshot in snapshots:
        validate_snapshot_integrity(snapshot)
        retrieval = retrieval_by_id.get(snapshot.retrieval_attempt_id)
        if retrieval is None:
            raise FixturePipelineError("snapshot references an unknown retrieval attempt")
        if snapshot.source_url != retrieval.source_url:
            raise FixturePipelineError("snapshot source_url does not match retrieval")
        _persist_model(
            db_path,
            snapshot,
            insert_snapshot,
            lambda snapshot=snapshot: read_snapshot(db_path, snapshot.snapshot_id),
            "snapshot",
        )


def _persist_provisionals(
    db_path: str,
    provisionals: Sequence[ProvisionalCandidate],
    run_id: UUID,
) -> None:
    existing = read_provisional_extractions(db_path, run_id)
    if existing:
        if _model_dump_list(existing) != _model_dump_list(provisionals):
            raise FixturePipelineError("existing provisional extractions differ from fixture")
        return
    for provisional in provisionals:
        insert_provisional_extraction(db_path, provisional)


def _filter_candidates(
    planner: PlannerOutput,
    snapshots: Sequence[SourceSnapshot],
    provisionals: Sequence[ProvisionalCandidate],
) -> list[CandidateQuoteBlock]:
    snapshot_by_id = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
    candidates: list[CandidateQuoteBlock] = []
    claim_keywords = _claim_keywords_from_planner(planner)
    for provisional in provisionals:
        snapshot = snapshot_by_id.get(provisional.snapshot_id)
        if snapshot is None:
            raise FixturePipelineError("provisional candidate references an unknown snapshot")
        result = filter_provisional_candidate(
            provisional,
            snapshot,
            claim_keywords=claim_keywords,
            post_filter_version=POST_FILTER_VERSION,
            post_filter_validated_at=provisional.extracted_at,
        )
        if not result.valid or result.candidate is None:
            raise FixturePipelineError(
                "fixture provisional candidate failed deterministic filtering: "
                f"{result.rejection_message}"
            )
        candidates.append(result.candidate)
    return sorted(candidates, key=lambda candidate: str(candidate.quote_block_id))


def _candidate_batches(
    run_id: UUID,
    candidates: Sequence[CandidateQuoteBlock],
    created_at: datetime,
) -> list[CandidateBatch]:
    grouped: dict[tuple[object, int], list[CandidateQuoteBlock]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.stance, candidate.query_round)].append(candidate)
    return [
        CandidateBatch(
            run_id=run_id,
            stance=stance,
            query_round=query_round,
            candidates=sorted(batch, key=lambda candidate: str(candidate.quote_block_id)),
            created_at=created_at,
        )
        for (stance, query_round), batch in sorted(
            grouped.items(),
            key=lambda item: (str(item[0][0]), item[0][1]),
        )
    ]


def _admit_ledger_records(
    candidates: Sequence[CandidateQuoteBlock],
    snapshots: Sequence[SourceSnapshot],
    analyst_decisions: Sequence[ScoreDecision],
    statement_drafts: Sequence[StatementDraft],
    reviewer_decisions: Sequence[StatementReviewResult],
    synthesis: SynthesisOutput,
) -> list[LedgerRecord]:
    snapshot_by_id = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
    candidate_by_id = {candidate.quote_block_id: candidate for candidate in candidates}
    decision_by_quote = {decision.quote_block_id: decision for decision in analyst_decisions}
    drafts_by_quote: dict[UUID, list[StatementDraft]] = defaultdict(list)
    reviews_by_quote: dict[UUID, list[StatementReviewResult]] = defaultdict(list)
    synthesis_items = _synthesis_items_by_ledger_id(synthesis)

    for draft in statement_drafts:
        drafts_by_quote[draft.quote_block_id].append(draft)
    for review in reviewer_decisions:
        reviews_by_quote[review.quote_block_id].append(review)

    extra_decisions = set(decision_by_quote) - set(candidate_by_id)
    if extra_decisions:
        raise FixturePipelineError("Analyst decisions reference unknown candidates")

    ledgers: list[LedgerRecord] = []
    for candidate in candidates:
        decision = decision_by_quote.get(candidate.quote_block_id)
        if decision is None:
            raise FixturePipelineError("candidate is missing fixture Analyst decision")
        if not decision.approved:
            continue
        snapshot = snapshot_by_id.get(candidate.snapshot_id)
        if snapshot is None:
            raise FixturePipelineError("candidate references an unknown snapshot")
        drafts = _sorted_drafts(drafts_by_quote.get(candidate.quote_block_id, []))
        reviews = _sorted_reviews(reviews_by_quote.get(candidate.quote_block_id, []))
        if not drafts or not reviews:
            raise FixturePipelineError("approved Analyst decision is missing Reviewer fixture data")
        final_review = reviews[-1]
        ledger_claim_id = derive_fixture_ledger_claim_id(candidate.run_id, final_review)
        synthesis_item = synthesis_items.get(ledger_claim_id)
        entailment = synthesis_item.entailment if synthesis_item is not None else Entailment.STRONG
        if final_review.approved_factual_statement is None:
            raise FixturePipelineError("approved Reviewer decision is missing approved statement")
        ledger = admit_ledger_record(
            LedgerAdmissionRequest(
                ledger_claim_id=ledger_claim_id,
                candidate=candidate,
                snapshot=snapshot,
                score_decision=decision,
                statement_drafts=drafts,
                review_results=reviews,
                approved_factual_statement=final_review.approved_factual_statement,
                entailment=entailment,
                ledger_validated_at=synthesis.created_at,
            )
        )
        ledgers.append(ledger)
    return sorted(ledgers, key=lambda ledger: str(ledger.ledger_claim_id))


def _synthesis_items_by_ledger_id(synthesis: SynthesisOutput) -> dict[UUID, object]:
    items: dict[UUID, object] = {}
    for section in synthesis.sections:
        for item in section.items:
            items[item.ledger_claim_id] = item
    return items


def _sorted_drafts(drafts: Sequence[StatementDraft]) -> list[StatementDraft]:
    return sorted(drafts, key=lambda draft: (draft.drafted_at, str(draft.statement_draft_id)))


def _sorted_reviews(reviews: Sequence[StatementReviewResult]) -> list[StatementReviewResult]:
    return sorted(reviews, key=lambda review: (review.reviewed_at, str(review.statement_draft_id)))


def _claim_keywords_from_planner(planner: PlannerOutput) -> tuple[str, ...]:
    text = " ".join(
        (
            planner.claim_definition.claim_text,
            planner.claim_definition.population,
            planner.claim_definition.intervention_or_exposure,
        )
    )
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "for",
        "in",
        "of",
        "or",
        "the",
        "to",
    }
    words = [word.strip(".,;:!?()[]{}\"'").casefold() for word in text.replace("-", " ").split()]
    keywords = tuple(
        dict.fromkeys(word for word in words if len(word) > 2 and word not in stop_words)
    )
    if not keywords:
        raise FixturePipelineError("PlannerOutput did not yield deterministic claim keywords")
    return keywords


def _persist_model(
    db_path: str,
    model: _ModelT,
    insert_fn: Callable[[str, _ModelT], None],
    read_existing: Callable[[], _ModelT],
    label: str,
) -> None:
    try:
        existing = read_existing()
    except KeyError:
        try:
            insert_fn(db_path, model)
        except sqlite3.IntegrityError as exc:
            raise FixturePipelineError(f"could not persist {label}: {exc}") from exc
        return
    _assert_same_model(existing, model, label)


def _assert_same_model(existing: StrictModel, expected: StrictModel, label: str) -> None:
    if existing.model_dump(mode="json") != expected.model_dump(mode="json"):
        raise FixturePipelineError(f"existing {label} differs from fixture artifact")


def _assert_expected_counts(
    db_path: str,
    run_id: UUID,
    *,
    retrieval_count: int,
    snapshot_count: int,
    provisional_count: int,
    candidate_count: int,
    analyst_decision_count: int,
    draft_count: int,
    review_count: int,
    ledger_count: int,
) -> None:
    expected = {
        "retrieval_attempts": retrieval_count,
        "snapshots": snapshot_count,
        "provisional_extractions": provisional_count,
        "candidates": candidate_count,
        "analyst_decisions": analyst_decision_count,
        "statement_drafts": draft_count,
        "statement_review_attempts": review_count,
        "ledger_records": ledger_count,
    }
    with sqlite3.connect(db_path) as conn:
        for table, count in expected.items():
            actual = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()[0]
            if actual != count:
                raise FixturePipelineError(
                    f"{table} has {actual} records for run {run_id}; expected {count}"
                )


def _build_audit_trail(
    *,
    run_id: UUID,
    raw_claim: str,
    planner: PlannerOutput,
    snapshots: Sequence[SourceSnapshot],
    provisionals: Sequence[ProvisionalCandidate],
    candidates: Sequence[CandidateQuoteBlock],
    analyst_decisions: Sequence[ScoreDecision],
    reviewer_decisions: Sequence[StatementReviewResult],
    ledger_records: Sequence[LedgerRecord],
    synthesis: SynthesisOutput,
    validation: ValidationResult,
) -> list[AuditEntry]:
    validation_status: Literal["released", "blocked"] = (
        "released" if validation.valid else "blocked"
    )
    validation_outcome = (
        f"released with rendered hash {validation.rendered_brief_hash}"
        if validation.valid
        else f"blocked with {len(validation.errors)} validation error(s)"
    )
    return [
        _audit(
            run_id,
            "raw_fixture_input",
            "loaded",
            "raw_claim.txt",
            1,
            compute_sha256(raw_claim),
            "raw claim loaded",
        ),
        _audit(
            run_id,
            Stage.CLAIM_PLANNER.value,
            "completed",
            "planner.json",
            len(planner.search_queries),
            _model_hash(planner),
            "typed PlannerOutput loaded",
        ),
        _audit(
            run_id,
            "fixture_snapshots",
            "completed",
            "snapshots.json",
            len(snapshots),
            _models_hash(snapshots),
            "fixture snapshots validated",
        ),
        _audit(
            run_id,
            "fixture_provisional_candidates",
            "completed",
            "provisional_candidates.json",
            len(provisionals),
            _models_hash(provisionals),
            "fixture provisional candidates loaded",
        ),
        _audit(
            run_id,
            "deterministic_candidate_filter",
            "completed",
            "CandidateQuoteBlock",
            len(candidates),
            _models_hash(candidates),
            "provisional candidates passed deterministic filtering",
        ),
        _audit(
            run_id,
            Stage.EVIDENCE_ANALYST.value,
            "completed",
            "analyst_decisions.json",
            len(analyst_decisions),
            _models_hash(analyst_decisions),
            "fixture Analyst decisions loaded",
        ),
        _audit(
            run_id,
            Stage.STATEMENT_REVIEWER.value,
            "completed",
            "reviewer_decisions.json",
            len(reviewer_decisions),
            _models_hash(reviewer_decisions),
            "fixture Reviewer decisions loaded",
        ),
        _audit(
            run_id,
            Stage.CLAIM_LEDGER.value,
            "completed",
            "LedgerRecord",
            len(ledger_records),
            _models_hash(ledger_records),
            "Reviewer-approved statements admitted to the Ledger",
        ),
        _audit(
            run_id,
            Stage.DEBATE_SYNTHESIZER.value,
            "completed",
            "synthesis.json",
            1,
            _model_hash(synthesis),
            "fixture SynthesisOutput loaded",
        ),
        _audit(
            run_id,
            Stage.FINAL_RENDERER_VALIDATOR.value,
            validation_status,
            "ValidationResult",
            1,
            _model_hash(validation),
            validation_outcome,
        ),
    ]


def _audit(
    run_id: UUID,
    stage: str,
    status: Literal["loaded", "completed", "released", "blocked"],
    artifact_ref: str,
    artifact_count: int,
    artifact_hash: str | None,
    outcome: str,
) -> AuditEntry:
    return AuditEntry(
        run_id=run_id,
        stage=stage,
        status=status,
        artifact_ref=artifact_ref,
        artifact_count=artifact_count,
        artifact_hash=artifact_hash,
        outcome=outcome,
    )


def _write_json_idempotent(path: Path, payload: object) -> None:
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != encoded:
            raise FixturePipelineError(f"existing output differs from deterministic result: {path}")
        return
    path.write_text(encoded, encoding="utf-8")


def _model_hash(model: StrictModel) -> str:
    return _json_hash(model.model_dump(mode="json"))


def _models_hash(models: Sequence[StrictModel]) -> str:
    return _json_hash(_model_dump_list(models))


def _model_dump_list(models: Sequence[StrictModel]) -> list[dict[str, object]]:
    return [model.model_dump(mode="json") for model in models]


def _json_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


# ===========================================================================
# Phase 9: real orchestration with controlled concurrency
# ===========================================================================

LIVE_POST_FILTER_VERSION = "phase9-live-post-filter-v1"
LIVE_LEDGER_ID_VERSION = "phase9-ledger-id-v1"
LIVE_APPROVAL_ID_VERSION = "phase9-approval-id-v1"
LIVE_DRAFT_ID_VERSION = "phase9-draft-id-v1"
STAGE_ALIAS_MAX_ATTEMPTS = 2
RESEARCHER_MAX_WORKERS = 2

_AVAILABILITY_FAILURE_PREFIXES = ("LLMTimeoutError", "LLMTransientError")
_QUOTE_FAILURE_MARKERS = (
    "does not appear in snapshot",
    "bracket",
    "segment offsets",
    'must match [context] "segments" [context]',
)


class OrchestratorError(RuntimeError):
    """Raised when a live run cannot continue; produces an explicit failed run."""


class BudgetExceededError(OrchestratorError):
    """Raised when the model-attempt or retrieval budget is exhausted."""


class _RunCancelledSignal(Exception):
    """Internal control-flow signal for cancellation between stages."""


class RunBudgets(StrictModel):
    """Per-run model and retrieval budgets.

    ``max_retrieval_attempts`` must cover the full balanced depth of 18
    intended attempts; the orchestrator never runs one side with a smaller
    budget than the other.
    """

    max_model_attempts: int = Field(default=120, ge=1)
    max_retrieval_attempts: int = Field(default=TOTAL_INTENDED_ATTEMPTS, ge=1)


class LiveRunResult(StrictModel):
    run_id: UUID
    status: Literal["released", "blocked", "failed", "cancelled"]
    stage_reached: Stage
    failure_reason: str | None = None
    ledger_count: int = Field(ge=0)
    validation_result: ValidationResult | None = None
    rendered_brief_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    final_brief: str | None = None
    model_attempts: list[StageModelAttempt]

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> LiveRunResult:
        if self.status == "released":
            if (
                self.validation_result is None
                or not self.validation_result.valid
                or self.rendered_brief_hash is None
                or self.final_brief is None
            ):
                raise ValueError("released runs require valid validation, hash, and brief")
        if self.status == "blocked":
            if self.validation_result is None or self.validation_result.valid:
                raise ValueError("blocked runs require an invalid validation result")
            if self.rendered_brief_hash is not None or self.final_brief is not None:
                raise ValueError("blocked runs cannot carry a rendered hash or brief")
        if self.status in {"failed", "cancelled"}:
            if self.failure_reason is None:
                raise ValueError("failed and cancelled runs require an explicit reason")
            if self.rendered_brief_hash is not None or self.final_brief is not None:
                raise ValueError("failed and cancelled runs cannot carry a hash or brief")
        return self


class RunInspection(StrictModel):
    """Typed snapshot of a possibly partial run for inspection."""

    run_id: UUID
    status: RunStatus
    current_stage: Stage
    raw_claim: str = Field(min_length=1)
    retrieval_count: int = Field(ge=0)
    snapshot_count: int = Field(ge=0)
    provisional_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    analyst_decision_count: int = Field(ge=0)
    draft_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    ledger_count: int = Field(ge=0)
    model_attempt_count: int = Field(ge=0)
    has_synthesis: bool
    has_validation: bool


@dataclass
class _LiveContext:
    db_path: str
    run_id: UUID
    raw_claim: str
    llm: LLMProvider
    search: SearchProvider
    scraper: ScraperProvider
    routing: ModelRoutingConfig
    budgets: RunBudgets
    clock: Clock
    cancel_check: Callable[[], bool]
    scrape_retry_policy: RetryPolicy
    prompts: dict[LLMStage, PromptTemplate]
    attempts_used: int = 0
    stage_reached: Stage = Stage.CLAIM_PLANNER
    extraction_failures: list[str] = field(default_factory=list)
    candidate_failures: list[str] = field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_live(
    *,
    raw_claim: str,
    db_path: str,
    llm_provider: LLMProvider,
    search_provider: SearchProvider,
    scraper_provider: ScraperProvider,
    routing: ModelRoutingConfig | None = None,
    budgets: RunBudgets | None = None,
    run_id: UUID | None = None,
    clock: Clock | None = None,
    cancel_check: Callable[[], bool] | None = None,
    scrape_retry_policy: RetryPolicy | None = None,
    prompts_dir: str | Path | None = None,
) -> LiveRunResult:
    """Run the complete provider-backed pipeline for one claim.

    Restarting with the same ``run_id`` and ``db_path`` resumes from persisted
    artifacts without duplicating snapshots, Ledger records, or completed
    model work; the audited model-attempt history accumulates insert-only
    across restarts.
    """
    if raw_claim.strip() == "":
        raise ValueError("raw_claim must not be empty")
    init_db(db_path)
    effective_run_id = run_id or uuid4()
    now = clock or _utc_now

    ctx = _LiveContext(
        db_path=db_path,
        run_id=effective_run_id,
        raw_claim=raw_claim.strip(),
        llm=llm_provider,
        search=search_provider,
        scraper=scraper_provider,
        routing=routing or default_model_routing(),
        budgets=budgets or RunBudgets(),
        clock=now,
        cancel_check=cancel_check or (lambda: False),
        scrape_retry_policy=scrape_retry_policy or RetryPolicy(),
        prompts={stage: load_prompt_template(stage, prompts_dir=prompts_dir) for stage in LLMStage},
    )

    manifest = _load_or_create_run(ctx)
    if manifest.status is RunStatus.COMPLETED:
        return _reconstruct_completed_result(ctx)
    ctx.attempts_used = len(read_stage_model_attempts_for_run(db_path, ctx.run_id))

    try:
        _checkpoint(ctx, Stage.CLAIM_PLANNER)
        planner = _stage_planner(ctx)
        _checkpoint(ctx, Stage.SUPPORTING_RESEARCHER)
        retrievals, snapshots = _stage_retrieval(ctx, planner)
        _checkpoint(ctx, Stage.OPPOSING_RESEARCHER)
        candidates = _stage_extraction(ctx, planner, retrievals, snapshots)
        _checkpoint(ctx, Stage.EVIDENCE_ANALYST)
        ledgers = _stage_analysis_review_ledger(ctx, planner, candidates, snapshots)
        _checkpoint(ctx, Stage.DEBATE_SYNTHESIZER)
        synthesis = _stage_synthesis(ctx, planner, ledgers)
        _checkpoint(ctx, Stage.FINAL_RENDERER_VALIDATOR)
        return _stage_validate_release(ctx, synthesis, ledgers)
    except _RunCancelledSignal:
        _set_run_state(ctx, RunStatus.CANCELLED)
        return _terminal_result(ctx, "cancelled", "run cancelled between stages")
    except (OrchestratorError, SearchProviderError, ScraperProviderError) as exc:
        _set_run_state(ctx, RunStatus.FAILED)
        return _terminal_result(ctx, "failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - every run must end with explicit status
        _set_run_state(ctx, RunStatus.FAILED)
        return _terminal_result(
            ctx, "failed", f"unexpected orchestrator failure: {type(exc).__name__}: {exc}"
        )


def inspect_run(db_path: str, run_id: UUID) -> RunInspection:
    """Typed partial-run inspection over persisted artifacts."""
    manifest = read_run(db_path, run_id)
    counts: dict[str, int] = {}
    with sqlite3.connect(db_path) as conn:
        for table in (
            "retrieval_attempts",
            "snapshots",
            "provisional_extractions",
            "candidates",
            "analyst_decisions",
            "statement_drafts",
            "statement_review_attempts",
            "ledger_records",
            "stage_model_attempts",
        ):
            counts[table] = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()[0]
        has_synthesis = (
            conn.execute(
                "SELECT COUNT(*) FROM synthesis_attempts WHERE run_id = ?", (str(run_id),)
            ).fetchone()[0]
            > 0
        )
        has_validation = (
            conn.execute(
                "SELECT COUNT(*) FROM validation_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()[0]
            > 0
        )
    return RunInspection(
        run_id=run_id,
        status=manifest.status,
        current_stage=manifest.current_stage,
        raw_claim=manifest.raw_claim,
        retrieval_count=counts["retrieval_attempts"],
        snapshot_count=counts["snapshots"],
        provisional_count=counts["provisional_extractions"],
        candidate_count=counts["candidates"],
        analyst_decision_count=counts["analyst_decisions"],
        draft_count=counts["statement_drafts"],
        review_count=counts["statement_review_attempts"],
        ledger_count=counts["ledger_records"],
        model_attempt_count=counts["stage_model_attempts"],
        has_synthesis=has_synthesis,
        has_validation=has_validation,
    )


# ---------------------------------------------------------------------------
# Run lifecycle helpers
# ---------------------------------------------------------------------------


def _load_or_create_run(ctx: _LiveContext) -> RunManifest:
    try:
        manifest = read_run(ctx.db_path, ctx.run_id)
    except KeyError:
        created = ctx.clock()
        manifest = RunManifest(
            run_id=ctx.run_id,
            status=RunStatus.RUNNING,
            raw_claim=ctx.raw_claim,
            current_stage=Stage.CLAIM_PLANNER,
            created_at=created,
            updated_at=created,
        )
        insert_run(ctx.db_path, manifest)
        return manifest
    if manifest.raw_claim != ctx.raw_claim:
        raise ValueError(
            f"run {ctx.run_id} already exists for a different claim; refusing to overwrite"
        )
    return manifest


def _checkpoint(ctx: _LiveContext, stage: Stage) -> None:
    """Cancellation check and manifest update between stages."""
    if ctx.cancel_check():
        raise _RunCancelledSignal()
    ctx.stage_reached = stage
    manifest = read_run(ctx.db_path, ctx.run_id)
    update_run(
        ctx.db_path,
        manifest.model_copy(
            update={
                "status": RunStatus.RUNNING,
                "current_stage": stage,
                "updated_at": ctx.clock(),
            }
        ),
    )


def _set_run_state(ctx: _LiveContext, status: RunStatus) -> None:
    manifest = read_run(ctx.db_path, ctx.run_id)
    completed_at = ctx.clock() if status is RunStatus.COMPLETED else None
    update_run(
        ctx.db_path,
        manifest.model_copy(
            update={
                "status": status,
                "current_stage": ctx.stage_reached,
                "updated_at": ctx.clock(),
                "completed_at": completed_at,
            }
        ),
    )


def _terminal_result(
    ctx: _LiveContext,
    status: Literal["failed", "cancelled"],
    reason: str,
) -> LiveRunResult:
    return LiveRunResult(
        run_id=ctx.run_id,
        status=status,
        stage_reached=ctx.stage_reached,
        failure_reason=reason,
        ledger_count=len(read_ledger_records_for_run(ctx.db_path, ctx.run_id)),
        model_attempts=read_stage_model_attempts_for_run(ctx.db_path, ctx.run_id),
    )


def _reconstruct_completed_result(ctx: _LiveContext) -> LiveRunResult:
    try:
        validation = read_validation(ctx.db_path, ctx.run_id)
    except KeyError as exc:
        raise OrchestratorError(
            f"run {ctx.run_id} is marked completed but has no validation result"
        ) from exc
    ledgers = read_ledger_records_for_run(ctx.db_path, ctx.run_id)
    attempts = read_stage_model_attempts_for_run(ctx.db_path, ctx.run_id)
    if validation.valid:
        synthesis = read_synthesis(ctx.db_path, ctx.run_id)
        brief = render_brief(synthesis, ledgers)
        return LiveRunResult(
            run_id=ctx.run_id,
            status="released",
            stage_reached=Stage.FINAL_RENDERER_VALIDATOR,
            ledger_count=len(ledgers),
            validation_result=validation,
            rendered_brief_hash=validation.rendered_brief_hash,
            final_brief=brief,
            model_attempts=attempts,
        )
    return LiveRunResult(
        run_id=ctx.run_id,
        status="blocked",
        stage_reached=Stage.FINAL_RENDERER_VALIDATOR,
        ledger_count=len(ledgers),
        validation_result=validation,
        model_attempts=attempts,
    )


def _persist_live(
    db_path: str,
    model: _ModelT,
    insert_fn: Callable[[str, _ModelT], None],
    read_existing: Callable[[], _ModelT],
    label: str,
) -> None:
    try:
        _persist_model(db_path, model, insert_fn, read_existing, label)
    except FixturePipelineError as exc:
        raise OrchestratorError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Routed model invocation with audited retry, fallback, and budgets
# ---------------------------------------------------------------------------


def _routed_invoke(
    ctx: _LiveContext,
    *,
    stage: LLMStage,
    work_unit: str,
    input_artifact: StrictModel,
    input_artifact_ids: Sequence[UUID],
    output_type: type,
    gate: Callable[[StageInvocationResult], str | None] | None = None,
    third_line_availability_only: bool = False,
) -> tuple[StageInvocationResult | None, str | None]:
    """Walk the stage's ordered model route with full audit records.

    The current alias is retried (by ``invoke_stage``) only for timeout,
    transient, or malformed/validation failures. Escalation to the next alias
    happens only for an objective recorded failure: an exhausted invocation or
    an objective ``gate`` rejection such as an exact-quote failure. Semantic
    disagreement never causes a model switch. When
    ``third_line_availability_only`` is set, the third alias is used only
    after an availability (timeout/transient) failure of the second.
    """
    route = ctx.routing.route_for(stage)
    last_failure: str | None = None
    last_was_availability = False
    escalation_reason: str | None = None

    for position, alias in enumerate(route.ordered_aliases):
        if position == 2 and third_line_availability_only and not last_was_availability:
            return None, (
                f"third-line fallback for {stage.value} is availability-only; "
                f"stopping after: {last_failure}"
            )
        remaining = ctx.budgets.max_model_attempts - ctx.attempts_used
        if remaining <= 0:
            raise BudgetExceededError(
                f"model attempt budget of {ctx.budgets.max_model_attempts} exhausted "
                f"at stage {stage.value} ({work_unit})"
            )
        invocation = invoke_stage(
            ctx.llm,
            run_id=ctx.run_id,
            stage=stage,
            prompt=ctx.prompts[stage],
            input_artifact=input_artifact,
            input_artifact_ids=input_artifact_ids,
            output_type=output_type,
            model_alias=alias,
            max_attempts=min(STAGE_ALIAS_MAX_ATTEMPTS, remaining),
            clock=ctx.clock,
        )
        _record_invocation_attempts(ctx, stage, work_unit, position, invocation, escalation_reason)

        if invocation.success:
            gate_reason = gate(invocation) if gate is not None else None
            if gate_reason is None:
                return invocation, None
            last_failure = gate_reason
            last_was_availability = False
            escalation_reason = gate_reason
            continue

        final_failure = invocation.attempts[-1].failure_reason or "unknown failure"
        last_failure = final_failure
        last_was_availability = final_failure.startswith(_AVAILABILITY_FAILURE_PREFIXES)
        escalation_reason = f"objective invocation failure on {alias}: {final_failure}"

    return None, last_failure


def _record_invocation_attempts(
    ctx: _LiveContext,
    stage: LLMStage,
    work_unit: str,
    route_position: int,
    invocation: StageInvocationResult,
    escalation_reason: str | None,
) -> None:
    for attempt in invocation.attempts:
        latency_ms = int((attempt.completed_at - attempt.started_at).total_seconds() * 1000)
        record = StageModelAttempt(
            run_id=ctx.run_id,
            attempt_id=uuid4(),
            stage=stage.value,
            work_unit=work_unit,
            model_alias=attempt.model_alias,
            pinned_model_snapshot=attempt.pinned_model_snapshot,
            route_position=route_position,
            attempt_number=attempt.attempt_number,
            status=attempt.status,
            failure_reason=attempt.failure_reason,
            retry_reason=attempt.retry_reason,
            escalation_reason=escalation_reason if route_position > 0 else None,
            started_at=attempt.started_at,
            completed_at=attempt.completed_at,
            latency_ms=max(latency_ms, 0),
            input_tokens=invocation.input_tokens if attempt.status == "succeeded" else None,
            output_tokens=invocation.output_tokens if attempt.status == "succeeded" else None,
        )
        insert_stage_model_attempt(ctx.db_path, record)
        ctx.attempts_used += 1


def _model_name_for(invocation: StageInvocationResult) -> str:
    return invocation.pinned_model_snapshot or invocation.model_alias


# ---------------------------------------------------------------------------
# Stage: Planner
# ---------------------------------------------------------------------------


def _stage_planner(ctx: _LiveContext) -> PlannerOutput:
    try:
        return read_planner_output(ctx.db_path, ctx.run_id)
    except KeyError:
        pass
    stage_input = PlannerStageInput(run_id=ctx.run_id, raw_claim=ctx.raw_claim)
    invocation, failure = _routed_invoke(
        ctx,
        stage=LLMStage.PLANNER,
        work_unit="planner",
        input_artifact=stage_input,
        input_artifact_ids=(ctx.run_id,),
        output_type=PlannerLLMOutput,
    )
    if invocation is None or invocation.output is None:
        raise OrchestratorError(f"planner stage failed: {failure}")
    assert isinstance(invocation.output, PlannerLLMOutput)
    planner = build_planner_output(
        run_id=ctx.run_id,
        raw_claim=ctx.raw_claim,
        model_output=invocation.output,
        prompt_version=invocation.prompt_version,
        model_name=_model_name_for(invocation),
        clock=ctx.clock,
    )
    _persist_live(
        ctx.db_path,
        planner,
        insert_planner_output,
        lambda: read_planner_output(ctx.db_path, ctx.run_id),
        "planner output",
    )
    return planner


# ---------------------------------------------------------------------------
# Stage: Researchers (two synchronous workers, no shared SQLite connections)
# ---------------------------------------------------------------------------


def _stage_retrieval(
    ctx: _LiveContext,
    planner: PlannerOutput,
) -> tuple[list[RetrievalRecord], list[SourceSnapshot]]:
    existing = read_retrieval_attempts_for_run(ctx.db_path, ctx.run_id)
    if existing:
        return existing, read_snapshots_for_run(ctx.db_path, ctx.run_id)

    if ctx.budgets.max_retrieval_attempts < TOTAL_INTENDED_ATTEMPTS:
        raise BudgetExceededError(
            f"retrieval budget of {ctx.budgets.max_retrieval_attempts} is below the "
            f"required {TOTAL_INTENDED_ATTEMPTS} balanced attempts; refusing to run "
            "either side at reduced depth"
        )

    # Workers receive typed inputs and return typed batches. They never touch
    # SQLite; the coordinator persists serialized results after both finish.
    with ThreadPoolExecutor(max_workers=RESEARCHER_MAX_WORKERS) as pool:
        supporting_future = pool.submit(
            retrieve_supporting,
            planner,
            ctx.search,
            ctx.scraper,
            retry_policy=ctx.scrape_retry_policy,
            clock=ctx.clock,
        )
        opposing_future = pool.submit(
            retrieve_opposing,
            planner,
            ctx.search,
            ctx.scraper,
            retry_policy=ctx.scrape_retry_policy,
            clock=ctx.clock,
        )
        supporting_error: Exception | None = None
        opposing_error: Exception | None = None
        supporting_batch: ResearcherRetrievalBatch | None = None
        opposing_batch: ResearcherRetrievalBatch | None = None
        try:
            supporting_batch = supporting_future.result()
        except Exception as exc:  # noqa: BLE001 - side failure must be explicit
            supporting_error = exc
        try:
            opposing_batch = opposing_future.result()
        except Exception as exc:  # noqa: BLE001 - side failure must be explicit
            opposing_error = exc

    if supporting_error is not None and opposing_error is not None:
        raise OrchestratorError(
            f"both researchers failed: supporting: {supporting_error}; opposing: {opposing_error}"
        )
    if supporting_error is not None:
        raise OrchestratorError(f"supporting researcher failed: {supporting_error}")
    if opposing_error is not None:
        raise OrchestratorError(f"opposing researcher failed: {opposing_error}")
    assert supporting_batch is not None and opposing_batch is not None

    retrievals: list[RetrievalRecord] = []
    snapshots: list[SourceSnapshot] = []
    # The two workers deduplicate within their own stance only; the same URL
    # can legitimately appear in both stances' search results. The snapshot
    # ID is derived from run, resolved URL, and content hash, so a repeated
    # ID across batches with identical content is the same source document:
    # keep the first snapshot and skip the duplicate. Differing content under
    # one ID is a hard integrity failure.
    seen_snapshots: dict[UUID, SourceSnapshot] = {}
    for batch in (supporting_batch, opposing_batch):
        for outcome in batch.outcomes:
            record = outcome.retrieval
            _persist_live(
                ctx.db_path,
                record,
                insert_retrieval_attempt,
                lambda record=record: read_retrieval_attempt(
                    ctx.db_path, record.retrieval_attempt_id
                ),
                "retrieval attempt",
            )
            retrievals.append(record)
        for snapshot in batch.snapshots:
            validate_snapshot_integrity(snapshot)
            existing = seen_snapshots.get(snapshot.snapshot_id)
            if existing is not None:
                if (
                    existing.snapshot_sha256 != snapshot.snapshot_sha256
                    or existing.normalized_text != snapshot.normalized_text
                ):
                    raise OrchestratorError(
                        "snapshot ID collision with differing content between researchers"
                    )
                continue  # cross-stance duplicate of the identical source document
            _persist_live(
                ctx.db_path,
                snapshot,
                insert_snapshot,
                lambda snapshot=snapshot: read_snapshot(ctx.db_path, snapshot.snapshot_id),
                "snapshot",
            )
            seen_snapshots[snapshot.snapshot_id] = snapshot
            snapshots.append(snapshot)
    return retrievals, snapshots


# ---------------------------------------------------------------------------
# Stage: Extraction with the extractor escalation policy
# ---------------------------------------------------------------------------


def _stage_extraction(
    ctx: _LiveContext,
    planner: PlannerOutput,
    retrievals: Sequence[RetrievalRecord],
    snapshots: Sequence[SourceSnapshot],
) -> list[CandidateQuoteBlock]:
    if read_provisional_extractions(ctx.db_path, ctx.run_id):
        candidates = read_candidates_for_run(ctx.db_path, ctx.run_id)
        if not candidates:
            raise OrchestratorError(
                "extraction previously produced no admissible candidates for this run"
            )
        return candidates

    queries_by_id = {query.query_id: query for query in planner.search_queries}
    retrieval_by_attempt = {record.retrieval_attempt_id: record for record in retrievals}
    claim_keywords = _claim_keywords_from_planner(planner)
    claim_text = planner.claim_definition.claim_text

    provisionals: list[ProvisionalCandidate] = []
    candidates: list[CandidateQuoteBlock] = []

    for snapshot in sorted(snapshots, key=lambda snap: str(snap.snapshot_id)):
        retrieval = retrieval_by_attempt.get(snapshot.retrieval_attempt_id)
        if retrieval is None:
            raise OrchestratorError("snapshot references an unknown retrieval attempt")
        query = queries_by_id.get(retrieval.query_id)
        if query is None:
            raise OrchestratorError("retrieval references an unknown planner query")

        stage_input = ExtractorStageInput(
            run_id=ctx.run_id,
            stance=query.stance,
            snapshot_id=snapshot.snapshot_id,
            claim_text=claim_text,
            labeled_snapshot_text=label_untrusted_source_text(snapshot.normalized_text),
            truncated=snapshot.truncated,
        )
        gate = _make_extraction_gate(ctx, snapshot, retrieval, query, claim_keywords)
        invocation, failure = _routed_invoke(
            ctx,
            stage=LLMStage.EXTRACTOR,
            work_unit=f"extract::{snapshot.snapshot_id}",
            input_artifact=stage_input,
            input_artifact_ids=(snapshot.snapshot_id,),
            output_type=ExtractorLLMOutput,
            gate=gate,
            third_line_availability_only=True,
        )
        if invocation is None or invocation.output is None:
            ctx.extraction_failures.append(f"snapshot {snapshot.snapshot_id}: {failure}")
            continue
        assert isinstance(invocation.output, ExtractorLLMOutput)
        snapshot_provisionals, snapshot_candidates = _filter_extraction_output(
            ctx, snapshot, retrieval, query, claim_keywords, invocation
        )
        provisionals.extend(snapshot_provisionals)
        candidates.extend(snapshot_candidates)

    if not candidates:
        details = "; ".join(ctx.extraction_failures) or "no quote blocks passed the filter"
        raise OrchestratorError(f"extraction produced no admissible candidates: {details}")

    for provisional in provisionals:
        insert_provisional_extraction(ctx.db_path, provisional)
    for candidate in sorted(candidates, key=lambda cand: str(cand.quote_block_id)):
        _persist_live(
            ctx.db_path,
            candidate,
            insert_candidate,
            lambda candidate=candidate: read_candidate(ctx.db_path, candidate.quote_block_id),
            "candidate",
        )
    return sorted(candidates, key=lambda cand: str(cand.quote_block_id))


def _build_provisional(
    ctx: _LiveContext,
    snapshot: SourceSnapshot,
    retrieval: RetrievalRecord,
    query: SearchQuery,
    quote_block: str,
    invocation: StageInvocationResult,
) -> ProvisionalCandidate:
    return ProvisionalCandidate(
        run_id=ctx.run_id,
        stance=query.stance,
        source_url=snapshot.source_url,
        retrieval_attempt_id=snapshot.retrieval_attempt_id,
        query_id=query.query_id,
        query_round=retrieval.query_round,
        search_rank=retrieval.search_rank,
        snapshot_id=snapshot.snapshot_id,
        snapshot_sha256=snapshot.snapshot_sha256,
        extracted_quote_block=quote_block,
        extraction_prompt_version=invocation.prompt_version,
        extraction_model_name=_model_name_for(invocation),
        extracted_at=ctx.clock(),
    )


def _make_extraction_gate(
    ctx: _LiveContext,
    snapshot: SourceSnapshot,
    retrieval: RetrievalRecord,
    query: SearchQuery,
    claim_keywords: tuple[str, ...],
) -> Callable[[StageInvocationResult], str | None]:
    def gate(invocation: StageInvocationResult) -> str | None:
        output = invocation.output
        assert isinstance(output, ExtractorLLMOutput)
        if not output.quote_blocks:
            # An empty extraction is a semantic judgment, never an escalation.
            return None
        passes = 0
        quote_failures = 0
        for quote_block in output.quote_blocks:
            provisional = _build_provisional(
                ctx, snapshot, retrieval, query, quote_block, invocation
            )
            result = filter_provisional_candidate(
                provisional,
                snapshot,
                claim_keywords=claim_keywords,
                post_filter_version=LIVE_POST_FILTER_VERSION,
                post_filter_validated_at=ctx.clock(),
            )
            if result.valid:
                passes += 1
            elif result.rejection_message is not None and any(
                marker in result.rejection_message for marker in _QUOTE_FAILURE_MARKERS
            ):
                quote_failures += 1
        if passes == 0 and quote_failures > 0:
            return (
                "objective: exact-quote failure - extracted segments were not found "
                "verbatim in the trusted snapshot"
            )
        return None

    return gate


def _filter_extraction_output(
    ctx: _LiveContext,
    snapshot: SourceSnapshot,
    retrieval: RetrievalRecord,
    query: SearchQuery,
    claim_keywords: tuple[str, ...],
    invocation: StageInvocationResult,
) -> tuple[list[ProvisionalCandidate], list[CandidateQuoteBlock]]:
    output = invocation.output
    assert isinstance(output, ExtractorLLMOutput)
    provisionals: list[ProvisionalCandidate] = []
    candidates: list[CandidateQuoteBlock] = []
    for quote_block in output.quote_blocks:
        provisional = _build_provisional(ctx, snapshot, retrieval, query, quote_block, invocation)
        provisionals.append(provisional)
        result = filter_provisional_candidate(
            provisional,
            snapshot,
            claim_keywords=claim_keywords,
            post_filter_version=LIVE_POST_FILTER_VERSION,
            post_filter_validated_at=ctx.clock(),
        )
        if result.valid and result.candidate is not None:
            candidates.append(result.candidate)
    return provisionals, candidates


# ---------------------------------------------------------------------------
# Stage: Analyst, Reviewer (one revision), and Ledger admission
# ---------------------------------------------------------------------------


def _stage_analysis_review_ledger(
    ctx: _LiveContext,
    planner: PlannerOutput,
    candidates: Sequence[CandidateQuoteBlock],
    snapshots: Sequence[SourceSnapshot],
) -> list[LedgerRecord]:
    snapshot_by_id = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
    claim_text = planner.claim_definition.claim_text

    existing_decisions = {
        decision.quote_block_id: decision
        for decision in read_analyst_decisions_for_run(ctx.db_path, ctx.run_id)
    }
    drafts_by_quote: dict[UUID, list[StatementDraft]] = defaultdict(list)
    for draft in read_statement_drafts_for_run(ctx.db_path, ctx.run_id):
        drafts_by_quote[draft.quote_block_id].append(draft)
    reviews_by_quote: dict[UUID, list[StatementReviewResult]] = defaultdict(list)
    for review in read_statement_reviews_for_run(ctx.db_path, ctx.run_id):
        reviews_by_quote[review.quote_block_id].append(review)
    ledger_by_quote = {
        record.quote_block_id: record
        for record in read_ledger_records_for_run(ctx.db_path, ctx.run_id)
    }

    ledgers: list[LedgerRecord] = []
    for candidate in sorted(candidates, key=lambda cand: str(cand.quote_block_id)):
        quote_block_id = candidate.quote_block_id
        snapshot = snapshot_by_id.get(candidate.snapshot_id)
        if snapshot is None:
            raise OrchestratorError("candidate references an unknown snapshot")

        if quote_block_id in ledger_by_quote:
            ledgers.append(ledger_by_quote[quote_block_id])
            continue

        existing_decision = existing_decisions.get(quote_block_id)
        existing_reviews = _sorted_reviews(reviews_by_quote.get(quote_block_id, []))
        if existing_decision is not None:
            if not existing_decision.approved:
                continue
            if len(existing_reviews) >= 2 and not existing_reviews[-1].approved:
                continue  # terminal Reviewer rejection recorded before restart
            ledger = _resume_candidate(
                ctx,
                candidate,
                snapshot,
                claim_text,
                existing_decision,
                _sorted_drafts(drafts_by_quote.get(quote_block_id, [])),
                existing_reviews,
            )
            if ledger is not None:
                ledgers.append(ledger)
            continue

        ledger = _process_candidate(ctx, candidate, snapshot, claim_text)
        if ledger is not None:
            ledgers.append(ledger)

    if not ledgers:
        details = "; ".join(ctx.candidate_failures) or "all candidates were rejected"
        raise OrchestratorError(f"no approved statements entered the Ledger: {details}")
    return sorted(ledgers, key=lambda record: str(record.ledger_claim_id))


def _invoke_analyst(
    ctx: _LiveContext,
    candidate: CandidateQuoteBlock,
    claim_text: str,
    work_unit: str,
) -> tuple[AnalystLLMOutput | None, StageInvocationResult | None, str | None]:
    stage_input = AnalystStageInput(
        run_id=ctx.run_id,
        quote_block_id=candidate.quote_block_id,
        claim_text=claim_text,
        labeled_quote_block=label_untrusted_source_text(candidate.extracted_quote_block),
        truncated=candidate.truncated,
    )
    invocation, failure = _routed_invoke(
        ctx,
        stage=LLMStage.ANALYST,
        work_unit=work_unit,
        input_artifact=stage_input,
        input_artifact_ids=(candidate.quote_block_id,),
        output_type=AnalystLLMOutput,
    )
    if invocation is None or invocation.output is None:
        return None, None, failure
    output = invocation.output
    assert isinstance(output, AnalystLLMOutput)
    return output, invocation, None


def _review_draft(
    ctx: _LiveContext,
    candidate: CandidateQuoteBlock,
    draft: StatementDraft,
) -> tuple[StatementReviewResult | None, str | None]:
    reviewer_input = build_reviewer_input(candidate, draft)
    invocation, failure = _routed_invoke(
        ctx,
        stage=LLMStage.REVIEWER,
        work_unit=f"reviewer::{draft.statement_draft_id}",
        input_artifact=reviewer_input,
        input_artifact_ids=(draft.statement_draft_id,),
        output_type=ReviewerLLMOutput,
    )
    if invocation is None or invocation.output is None:
        return None, failure
    output = invocation.output
    assert isinstance(output, ReviewerLLMOutput)
    checks = ReviewChecks(
        fully_entailed=output.fully_entailed,
        qualifications_preserved=output.qualifications_preserved,
        neutral_framing=output.neutral_framing,
        claim_fit_scope_valid=output.claim_fit_scope_valid,
    )
    approved = (
        output.fully_entailed
        and output.qualifications_preserved
        and output.neutral_framing
        and output.claim_fit_scope_valid
    )
    approval_id = (
        uuid5(
            URL_NAMESPACE,
            f"{LIVE_APPROVAL_ID_VERSION}::{ctx.run_id}::{draft.statement_draft_id}",
        )
        if approved
        else None
    )
    review = review_statement(
        draft,
        reviewer_input,
        checks,
        reviewer_prompt_version=invocation.prompt_version,
        reviewer_model_name=_model_name_for(invocation),
        reviewed_at=ctx.clock(),
        reviewer_approval_id=approval_id,
    )
    return review, None


def _derive_draft_id(ctx: _LiveContext, quote_block_id: UUID, revision: int, text: str) -> UUID:
    return uuid5(
        URL_NAMESPACE,
        f"{LIVE_DRAFT_ID_VERSION}::{ctx.run_id}::{quote_block_id}::{revision}::{text}",
    )


def _process_candidate(
    ctx: _LiveContext,
    candidate: CandidateQuoteBlock,
    snapshot: SourceSnapshot,
    claim_text: str,
) -> LedgerRecord | None:
    quote_block_id = candidate.quote_block_id
    analyst_output, invocation, failure = _invoke_analyst(
        ctx, candidate, claim_text, f"analyst::{quote_block_id}"
    )
    if analyst_output is None or invocation is None:
        ctx.candidate_failures.append(f"analyst failed for {quote_block_id}: {failure}")
        return None

    decision = score_candidate(
        run_id=ctx.run_id,
        quote_block_id=quote_block_id,
        evidence_quality=analyst_output.evidence_quality,
        claim_fit=analyst_output.claim_fit,
        rationale=analyst_output.rationale,
        analyst_prompt_version=invocation.prompt_version,
        analyst_model_name=_model_name_for(invocation),
        scored_at=ctx.clock(),
    )
    if not decision.approved:
        _persist_decision(ctx, decision)
        ctx.candidate_failures.append(f"analyst rejected {quote_block_id}: {decision.rationale}")
        return None

    first_draft = create_statement_draft(
        candidate=candidate,
        score_decision=decision,
        statement_draft_id=_derive_draft_id(ctx, quote_block_id, 1, analyst_output.draft_statement),
        draft_statement=analyst_output.draft_statement,
        drafted_at=ctx.clock(),
    )
    first_review, review_failure = _review_draft(ctx, candidate, first_draft)
    if first_review is None:
        ctx.candidate_failures.append(
            f"reviewer unavailable for {quote_block_id}: {review_failure}"
        )
        return None

    drafts = [first_draft]
    reviews = [first_review]
    entailment = analyst_output.entailment

    if not first_review.approved:
        revised = _revise_and_review(ctx, candidate, claim_text, decision)
        if revised is None:
            _persist_decision(ctx, decision)
            _persist_draft(ctx, first_draft)
            _persist_review(ctx, first_review)
            return None
        revision_draft, revision_review, revision_entailment = revised
        drafts.append(revision_draft)
        reviews.append(revision_review)
        entailment = revision_entailment

    _persist_decision(ctx, decision)
    for draft in drafts:
        _persist_draft(ctx, draft)
    for review in reviews:
        _persist_review(ctx, review)

    final_review = reviews[-1]
    if not final_review.approved:
        ctx.candidate_failures.append(
            f"reviewer rejected {quote_block_id} after one revision ({final_review.failure_code})"
        )
        return None
    return _admit_and_persist_ledger(
        ctx, candidate, snapshot, decision, drafts, reviews, entailment
    )


def _revise_and_review(
    ctx: _LiveContext,
    candidate: CandidateQuoteBlock,
    claim_text: str,
    decision: ScoreDecision,
) -> tuple[StatementDraft, StatementReviewResult, Entailment] | None:
    quote_block_id = candidate.quote_block_id
    revision_output, revision_invocation, failure = _invoke_analyst(
        ctx, candidate, claim_text, f"analyst-revision::{quote_block_id}"
    )
    if revision_output is None or revision_invocation is None:
        ctx.candidate_failures.append(f"analyst revision failed for {quote_block_id}: {failure}")
        return None
    revision_draft = create_statement_draft(
        candidate=candidate,
        score_decision=decision,
        statement_draft_id=_derive_draft_id(
            ctx, quote_block_id, 2, revision_output.draft_statement
        ),
        draft_statement=revision_output.draft_statement,
        drafted_at=ctx.clock(),
    )
    revision_review, review_failure = _review_draft(ctx, candidate, revision_draft)
    if revision_review is None:
        ctx.candidate_failures.append(
            f"reviewer unavailable for revision of {quote_block_id}: {review_failure}"
        )
        return None
    return revision_draft, revision_review, revision_output.entailment


def _resume_candidate(
    ctx: _LiveContext,
    candidate: CandidateQuoteBlock,
    snapshot: SourceSnapshot,
    claim_text: str,
    decision: ScoreDecision,
    drafts: list[StatementDraft],
    reviews: list[StatementReviewResult],
) -> LedgerRecord | None:
    """Resume a candidate whose decision was persisted before a restart."""
    quote_block_id = candidate.quote_block_id
    if len(reviews) == 1 and not reviews[0].approved and drafts:
        revised = _revise_and_review(ctx, candidate, claim_text, decision)
        if revised is None:
            return None
        revision_draft, revision_review, entailment = revised
        _persist_draft(ctx, revision_draft)
        _persist_review(ctx, revision_review)
        drafts = [*drafts, revision_draft]
        reviews = [*reviews, revision_review]
        if not revision_review.approved:
            ctx.candidate_failures.append(
                f"reviewer rejected {quote_block_id} after one revision "
                f"({revision_review.failure_code})"
            )
            return None
        return _admit_and_persist_ledger(
            ctx, candidate, snapshot, decision, drafts, reviews, entailment
        )
    if reviews and reviews[-1].approved:
        # Rare crash window: approval persisted but Ledger admission did not
        # complete. Recover entailment with a fresh analyst invocation; every
        # deterministic admission gate still applies.
        analyst_output, _, failure = _invoke_analyst(
            ctx, candidate, claim_text, f"analyst-recovery::{quote_block_id}"
        )
        if analyst_output is None:
            ctx.candidate_failures.append(
                f"analyst recovery failed for {quote_block_id}: {failure}"
            )
            return None
        return _admit_and_persist_ledger(
            ctx, candidate, snapshot, decision, drafts, reviews, analyst_output.entailment
        )
    ctx.candidate_failures.append(
        f"candidate {quote_block_id} was left in an unrecoverable partial state"
    )
    return None


def _admit_and_persist_ledger(
    ctx: _LiveContext,
    candidate: CandidateQuoteBlock,
    snapshot: SourceSnapshot,
    decision: ScoreDecision,
    drafts: list[StatementDraft],
    reviews: list[StatementReviewResult],
    entailment: Entailment,
) -> LedgerRecord | None:
    final_review = reviews[-1]
    if not final_review.approved or final_review.approved_factual_statement is None:
        raise OrchestratorError("ledger admission requires an approved final review")
    ledger_claim_id = uuid5(
        URL_NAMESPACE,
        (
            f"{LIVE_LEDGER_ID_VERSION}::{ctx.run_id}::ledger::"
            f"{final_review.reviewer_approval_id}::{final_review.approved_factual_statement}"
        ),
    )
    try:
        ledger = admit_ledger_record(
            LedgerAdmissionRequest(
                ledger_claim_id=ledger_claim_id,
                candidate=candidate,
                snapshot=snapshot,
                score_decision=decision,
                statement_drafts=drafts,
                review_results=reviews,
                approved_factual_statement=final_review.approved_factual_statement,
                entailment=entailment,
                ledger_validated_at=ctx.clock(),
            )
        )
    except (ValueError, PydanticValidationError) as exc:
        ctx.candidate_failures.append(f"ledger admission blocked {candidate.quote_block_id}: {exc}")
        return None
    _persist_live(
        ctx.db_path,
        ledger,
        insert_ledger_record,
        lambda: read_ledger_record(ctx.db_path, ledger.ledger_claim_id),
        "ledger record",
    )
    return ledger


def _persist_decision(ctx: _LiveContext, decision: ScoreDecision) -> None:
    _persist_live(
        ctx.db_path,
        decision,
        insert_analyst_decision,
        lambda: read_analyst_decision(ctx.db_path, decision.run_id, decision.quote_block_id),
        "analyst decision",
    )


def _persist_draft(ctx: _LiveContext, draft: StatementDraft) -> None:
    _persist_live(
        ctx.db_path,
        draft,
        insert_statement_draft,
        lambda: read_statement_draft(ctx.db_path, draft.statement_draft_id),
        "statement draft",
    )


def _persist_review(ctx: _LiveContext, review: StatementReviewResult) -> None:
    _persist_live(
        ctx.db_path,
        review,
        insert_statement_review,
        lambda: read_statement_review(ctx.db_path, review.run_id, review.statement_draft_id),
        "statement review",
    )


# ---------------------------------------------------------------------------
# Stage: Synthesizer, deterministic Renderer, and final Validator
# ---------------------------------------------------------------------------


def _stage_synthesis(
    ctx: _LiveContext,
    planner: PlannerOutput,
    ledgers: Sequence[LedgerRecord],
) -> SynthesisOutput:
    try:
        return read_synthesis(ctx.db_path, ctx.run_id)
    except KeyError:
        pass
    stage_input = SynthesizerStageInput(
        run_id=ctx.run_id,
        claim_text=planner.claim_definition.claim_text,
        approved_statement_count=len(ledgers),
    )
    invocation, failure = _routed_invoke(
        ctx,
        stage=LLMStage.SYNTHESIZER,
        work_unit="synthesizer",
        input_artifact=stage_input,
        input_artifact_ids=(ctx.run_id,),
        output_type=SynthesizerLLMOutput,
    )
    if invocation is None or invocation.output is None:
        raise OrchestratorError(f"synthesizer stage failed: {failure}")
    output = invocation.output
    assert isinstance(output, SynthesizerLLMOutput)
    synthesis = build_synthesis_output(
        run_id=ctx.run_id,
        title=output.title,
        claim_definition=planner.claim_definition.claim_text,
        ledger_records=list(ledgers),
        created_at=ctx.clock(),
        synthesizer_prompt_version=invocation.prompt_version,
        synthesizer_model_name=_model_name_for(invocation),
        supporting_heading=output.supporting_heading,
        opposing_heading=output.opposing_heading,
        limitations_heading=output.limitations_heading,
    )
    _persist_live(
        ctx.db_path,
        synthesis,
        insert_synthesis,
        lambda: read_synthesis(ctx.db_path, ctx.run_id),
        "synthesis output",
    )
    return synthesis


def _stage_validate_release(
    ctx: _LiveContext,
    synthesis: SynthesisOutput,
    ledgers: Sequence[LedgerRecord],
) -> LiveRunResult:
    try:
        validation = read_validation(ctx.db_path, ctx.run_id)
    except KeyError:
        validation = validate_final_release(
            synthesis,
            list(ledgers),
            validated_at=ctx.clock(),
        )
        _persist_live(
            ctx.db_path,
            validation,
            insert_validation,
            lambda: read_validation(ctx.db_path, ctx.run_id),
            "validation result",
        )
    ctx.stage_reached = Stage.FINAL_RENDERER_VALIDATOR
    _set_run_state(ctx, RunStatus.COMPLETED)
    attempts = read_stage_model_attempts_for_run(ctx.db_path, ctx.run_id)
    if validation.valid:
        brief = render_brief(synthesis, list(ledgers))
        return LiveRunResult(
            run_id=ctx.run_id,
            status="released",
            stage_reached=Stage.FINAL_RENDERER_VALIDATOR,
            ledger_count=len(ledgers),
            validation_result=validation,
            rendered_brief_hash=validation.rendered_brief_hash,
            final_brief=brief,
            model_attempts=attempts,
        )
    return LiveRunResult(
        run_id=ctx.run_id,
        status="blocked",
        stage_reached=Stage.FINAL_RENDERER_VALIDATOR,
        ledger_count=len(ledgers),
        validation_result=validation,
        model_attempts=attempts,
    )
