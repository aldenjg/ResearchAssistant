from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

from pydantic import Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models import StrictModel  # noqa: E402
from orchestrator import FixturePipelineResult, run_fixture_pipeline  # noqa: E402

REPO_ROOT = PROJECT_ROOT
DEFAULT_FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
DEFAULT_DB_PATH = "live_runs.sqlite3"
RUNS_PAGE = "Runs"
FIXTURE_PAGE = "Fixture pipeline"
REQUIRED_FIXTURE_FILES = (
    "raw_claim.txt",
    "planner.json",
    "retrievals.json",
    "snapshots.json",
    "provisional_candidates.json",
    "analyst_decisions.json",
    "statement_drafts.json",
    "reviewer_decisions.json",
    "synthesis.json",
)


class FixtureOption(StrictModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)


class FrontendValidationError(StrictModel):
    code: str = Field(min_length=1)
    location: str = Field(min_length=1)
    message: str = Field(min_length=1)


class FrontendValidationSummary(StrictModel):
    valid: bool
    validator_config_version: str = Field(min_length=1)
    validated_at: str = Field(min_length=1)
    rendered_brief_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validation_artifact_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    errors: list[FrontendValidationError]


class FrontendArtifactCounts(StrictModel):
    retrievals: int = Field(ge=0)
    snapshots: int = Field(ge=0)
    provisional_candidates: int = Field(ge=0)
    candidates: int = Field(ge=0)
    candidate_batches: int = Field(ge=0)
    analyst_decisions: int = Field(ge=0)
    statement_drafts: int = Field(ge=0)
    reviewer_decisions: int = Field(ge=0)
    ledger_records: int = Field(ge=0)
    synthesis_sections: int = Field(ge=0)
    synthesis_items: int = Field(ge=0)
    audit_entries: int = Field(ge=0)


class FrontendRunMetadata(StrictModel):
    fixture_name: str = Field(min_length=1)
    fixture_dir: str = Field(min_length=1)
    output_dir: str = Field(min_length=1)
    db_path: str = Field(min_length=1)
    audit_path: str = Field(min_length=1)
    result_path: str = Field(min_length=1)
    planner_prompt_version: str = Field(min_length=1)
    planner_model_name: str = Field(min_length=1)
    synthesizer_prompt_version: str = Field(min_length=1)
    synthesizer_model_name: str = Field(min_length=1)


class FrontendAuditEntry(StrictModel):
    stage: str = Field(min_length=1)
    status: str = Field(min_length=1)
    artifact_ref: str = Field(min_length=1)
    artifact_count: int = Field(ge=0)
    artifact_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    outcome: str = Field(min_length=1)


class FrontendRunSummary(StrictModel):
    run_id: str = Field(min_length=1)
    raw_claim: str = Field(min_length=1)
    status: Literal["released", "blocked"]
    block_reason: str | None = None
    final_brief: str | None = None
    validation: FrontendValidationSummary
    counts: FrontendArtifactCounts
    metadata: FrontendRunMetadata
    audit_trail: list[FrontendAuditEntry]


def discover_fixture_runs(fixtures_dir: str | Path = DEFAULT_FIXTURES_DIR) -> list[FixtureOption]:
    root = Path(fixtures_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"fixture directory does not exist: {root}")

    fixture_options: list[FixtureOption] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        if path.is_dir() and not path.name.startswith(".") and _is_fixture_run_dir(path):
            fixture_options.append(FixtureOption(name=path.name, path=str(path)))
    return fixture_options


def run_fixture_for_frontend(
    fixture_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
) -> FrontendRunSummary:
    result = run_fixture_pipeline(fixture_dir, output_dir=output_dir)
    return summarize_fixture_result(result)


def summarize_fixture_result(result: FixturePipelineResult) -> FrontendRunSummary:
    validation_errors = [
        FrontendValidationError(
            code=error.code.value,
            location=error.location,
            message=error.message,
        )
        for error in result.validation_result.errors
    ]
    audit_entries = [
        FrontendAuditEntry(
            stage=entry.stage,
            status=entry.status,
            artifact_ref=entry.artifact_ref,
            artifact_count=entry.artifact_count,
            artifact_hash=entry.artifact_hash,
            outcome=entry.outcome,
        )
        for entry in result.audit_trail
    ]
    synthesis_item_count = sum(len(section.items) for section in result.synthesis_output.sections)
    validation_artifact_hash = audit_entries[-1].artifact_hash if audit_entries else None
    return FrontendRunSummary(
        run_id=str(result.run_id),
        raw_claim=result.raw_claim,
        status=result.status,
        block_reason=_block_reason(validation_errors) if result.status == "blocked" else None,
        final_brief=result.final_brief,
        validation=FrontendValidationSummary(
            valid=result.validation_result.valid,
            validator_config_version=result.validation_result.validator_config_version,
            validated_at=result.validation_result.validated_at.isoformat(),
            rendered_brief_hash=result.rendered_brief_hash,
            validation_artifact_hash=validation_artifact_hash,
            errors=validation_errors,
        ),
        counts=FrontendArtifactCounts(
            retrievals=len(result.retrievals),
            snapshots=len(result.snapshots),
            provisional_candidates=len(result.provisional_candidates),
            candidates=len(result.candidates),
            candidate_batches=len(result.candidate_batches),
            analyst_decisions=len(result.analyst_decisions),
            statement_drafts=len(result.statement_drafts),
            reviewer_decisions=len(result.reviewer_decisions),
            ledger_records=len(result.ledger_records),
            synthesis_sections=len(result.synthesis_output.sections),
            synthesis_items=synthesis_item_count,
            audit_entries=len(result.audit_trail),
        ),
        metadata=FrontendRunMetadata(
            fixture_name=Path(result.fixture_dir).name,
            fixture_dir=result.fixture_dir,
            output_dir=result.output_dir,
            db_path=result.db_path,
            audit_path=result.audit_path,
            result_path=result.result_path,
            planner_prompt_version=result.planner_output.planner_prompt_version,
            planner_model_name=result.planner_output.planner_model_name,
            synthesizer_prompt_version=result.synthesis_output.synthesizer_prompt_version,
            synthesizer_model_name=result.synthesis_output.synthesizer_model_name,
        ),
        audit_trail=audit_entries,
    )


def main() -> None:
    """Entry point: a two-page shell over the pipeline's own artifacts."""
    st = _load_streamlit()

    from frontend import theme
    from frontend.views import fixture_view, runs_view

    theme.apply_theme("Debate Research Agent System")

    with st.sidebar:
        theme.write(theme.brand())
        page = st.radio(
            "Page",
            (RUNS_PAGE, FIXTURE_PAGE),
            label_visibility="collapsed",
        )
        db_path = st.text_input("Run database", value=DEFAULT_DB_PATH)
        theme.write(
            '<div class="empty-hint">Read-only. Runs are started from the CLI:'
            "<br/>python cli.py run &quot;claim&quot;</div>"
        )

    if page == RUNS_PAGE:
        runs_view.render(db_path.strip() or DEFAULT_DB_PATH)
        return
    fixture_view.render()


def _is_fixture_run_dir(path: Path) -> bool:
    return all((path / filename).is_file() for filename in REQUIRED_FIXTURE_FILES)


def _block_reason(errors: list[FrontendValidationError]) -> str:
    if not errors:
        return "Blocked without a validation error."
    first = errors[0]
    return f"{first.code} at {first.location}: {first.message}"


def _load_streamlit() -> object:
    try:
        import streamlit as st
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Streamlit is not installed. Install project dependencies, then run "
            "`streamlit run frontend/streamlit_app.py` from the repository root."
        ) from exc
    return st


if __name__ == "__main__":
    main()
