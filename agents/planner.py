"""Claim Planner stage: provider-backed planning with deterministic IDs.

The model interprets the claim and proposes query text only.  The system
supplies the exact claim text, assigns every identifier deterministically
after validation, and appends the required exclusion parameters itself.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from models import (
    REQUIRED_QUERY_EXCLUSIONS,
    AmbiguityRecord,
    ClaimDefinition,
    PlannerOutput,
    SearchQuery,
    Stance,
    StrictModel,
)
from providers.llm import (
    Clock,
    GenerationSettings,
    LLMProvider,
    LLMStage,
    PlannerLLMOutput,
    PlannerStageInput,
    PromptTemplate,
    StageInvocationResult,
    invoke_stage,
    load_prompt_template,
)
from utils import URL_NAMESPACE

PLANNER_ID_VERSION = "phase8-planner-id-v1"

_STRATEGY_BY_ROUND: dict[tuple[Stance, int], str] = {
    (Stance.SUPPORTING, 1): "direct_affirmation",
    (Stance.SUPPORTING, 2): "underlying_mechanism",
    (Stance.SUPPORTING, 3): "deep_dive_analysis",
    (Stance.OPPOSING, 1): "direct_refutation",
    (Stance.OPPOSING, 2): "limiting_conditions",
    (Stance.OPPOSING, 3): "confounding_factors",
}


class PlannerStageResult(StrictModel):
    planner_output: PlannerOutput | None = None
    invocation: StageInvocationResult[PlannerLLMOutput]

    @property
    def success(self) -> bool:
        return self.planner_output is not None


def plan_claim(
    provider: LLMProvider,
    *,
    run_id: UUID,
    raw_claim: str,
    model_alias: str,
    prompt: PromptTemplate | None = None,
    settings: GenerationSettings | None = None,
    max_attempts: int = 1,
    clock: Clock,
) -> PlannerStageResult:
    """Run the Planner stage and convert validated model output to PlannerOutput."""
    if raw_claim.strip() == "":
        raise ValueError("raw_claim must not be empty")

    prompt_template = prompt or load_prompt_template(LLMStage.PLANNER)
    stage_input = PlannerStageInput(run_id=run_id, raw_claim=raw_claim)
    invocation = invoke_stage(
        provider,
        run_id=run_id,
        stage=LLMStage.PLANNER,
        prompt=prompt_template,
        input_artifact=stage_input,
        input_artifact_ids=(run_id,),
        output_type=PlannerLLMOutput,
        model_alias=model_alias,
        settings=settings,
        max_attempts=max_attempts,
        clock=clock,
    )
    if not invocation.success or invocation.output is None:
        return PlannerStageResult(planner_output=None, invocation=invocation)

    planner_output = build_planner_output(
        run_id=run_id,
        raw_claim=raw_claim,
        model_output=invocation.output,
        prompt_version=invocation.prompt_version,
        model_name=invocation.pinned_model_snapshot or invocation.model_alias,
        clock=clock,
    )
    return PlannerStageResult(planner_output=planner_output, invocation=invocation)


def build_planner_output(
    *,
    run_id: UUID,
    raw_claim: str,
    model_output: PlannerLLMOutput,
    prompt_version: str,
    model_name: str,
    clock: Clock,
) -> PlannerOutput:
    """Convert validated planner model output into the typed PlannerOutput.

    All identifiers are derived deterministically here, after Pydantic
    validation of the model output succeeded; the model never creates IDs.
    """
    created_at = clock()
    claim_definition = ClaimDefinition(
        run_id=run_id,
        claim_text=raw_claim,
        population=model_output.population,
        jurisdiction=model_output.jurisdiction,
        time_period=model_output.time_period,
        comparison_baseline=model_output.comparison_baseline,
        intervention_or_exposure=model_output.intervention_or_exposure,
        causal_or_comparative_meaning=model_output.causal_or_comparative_meaning,
        created_at=created_at,
    )
    ambiguities = [
        AmbiguityRecord(
            run_id=run_id,
            ambiguity_id=uuid5(
                URL_NAMESPACE,
                f"{PLANNER_ID_VERSION}::{run_id}::ambiguity::{index}::{ambiguity.description}",
            ),
            description=ambiguity.description,
            impact=ambiguity.impact,
            created_at=created_at,
        )
        for index, ambiguity in enumerate(model_output.ambiguities)
    ]
    exclusion_parameters = " ".join(REQUIRED_QUERY_EXCLUSIONS)
    search_queries = [
        SearchQuery(
            run_id=run_id,
            query_id=uuid5(
                URL_NAMESPACE,
                f"{PLANNER_ID_VERSION}::{run_id}::query::{query.stance.value}::{query.query_round}",
            ),
            stance=query.stance,
            query_round=query.query_round,
            strategy=_STRATEGY_BY_ROUND[(query.stance, query.query_round)],
            query_text=query.query_text,
            exclusion_parameters=exclusion_parameters,
            created_at=created_at,
        )
        for query in sorted(
            model_output.queries,
            key=lambda query: (query.stance.value, query.query_round),
        )
    ]
    return PlannerOutput(
        run_id=run_id,
        claim_definition=claim_definition,
        ambiguities=ambiguities,
        search_queries=search_queries,
        planner_prompt_version=prompt_version,
        planner_model_name=model_name,
        planned_at=clock(),
    )
