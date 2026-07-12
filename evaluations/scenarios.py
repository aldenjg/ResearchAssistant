"""Offline pipeline scenarios and validator mutation attacks for evaluations.

Each pipeline scenario builds deterministic fake providers, an expected final
run status, and post-run checks that return a list of problems (empty when
the scenario behaves as required). Mutation attacks take a released run's
synthesis and Ledger records and produce a tampered synthesis that the final
validator must block.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

from evaluations.fakes import (
    FABRICATED_QUOTE_MARKER,
    INJECTION_SENTENCE,
    FakeScraper,
    FakeSearch,
    StageLLM,
    analyst_payload,
    bad_quote_block_json,
    empty_quote_block_json,
    injection_page,
    reviewer_reject_payload,
)
from models import Entailment, LedgerRecord, Placement, StageModelAttempt, SynthesisOutput
from orchestrator import LiveRunResult
from providers.llm import (
    UNTRUSTED_TEXT_BEGIN,
    UNTRUSTED_TEXT_NOTICE,
    LLMProviderError,
    LLMStage,
    LLMTimeoutError,
)


@dataclass
class ScenarioRun:
    """Everything a check needs about one executed pipeline scenario."""

    result: LiveRunResult
    db_path: str
    llm: StageLLM
    attempts: list[StageModelAttempt]


CheckFn = Callable[[ScenarioRun], list[str]]


@dataclass
class PipelineScenario:
    name: str
    expected_status: str
    llm_factory: Callable[[], StageLLM]
    search_factory: Callable[[], FakeSearch] = FakeSearch
    scraper_factory: Callable[[], FakeScraper] = FakeScraper
    checks: list[CheckFn] = field(default_factory=list)


def _check_all_primary(run: ScenarioRun) -> list[str]:
    problems = []
    for attempt in run.attempts:
        if attempt.route_position != 0:
            problems.append(
                f"unexpected fallback: stage={attempt.stage} alias={attempt.model_alias}"
            )
    return problems


def _check_retry_without_fallback(run: ScenarioRun) -> list[str]:
    problems = []
    retried = [a for a in run.attempts if a.retry_reason is not None]
    if not retried:
        problems.append("expected a same-alias retry to be recorded")
    if any(a.route_position != 0 for a in run.attempts if a.stage == "extractor"):
        problems.append("transient failure must not escalate past the primary alias")
    return problems


def _check_backup_fallback_recorded(run: ScenarioRun) -> list[str]:
    problems = []
    escalated = [a for a in run.attempts if a.stage == "extractor" and a.route_position == 1]
    if not escalated:
        problems.append("expected a recorded escalation to the backup alias")
    for attempt in escalated:
        if not attempt.escalation_reason:
            problems.append("backup attempts must carry an escalation reason")
    return problems


def _check_exact_quote_escalation(run: ScenarioRun) -> list[str]:
    problems = _check_backup_fallback_recorded(run)
    escalated = [a for a in run.attempts if a.stage == "extractor" and a.route_position == 1]
    if not any("exact-quote" in (a.escalation_reason or "") for a in escalated):
        problems.append("expected an exact-quote escalation reason")
    problems.extend(_check_fabricated_text_gated(run))
    return problems


def _check_no_extractor_escalation(run: ScenarioRun) -> list[str]:
    problems = []
    for attempt in run.attempts:
        if attempt.stage == "extractor" and attempt.route_position != 0:
            problems.append("semantic disagreement must not cause a model switch")
    return problems


def _check_third_line_gated(run: ScenarioRun) -> list[str]:
    problems = []
    third = [a for a in run.attempts if a.route_position == 2]
    if not third:
        problems.append("expected the third-line alias to be used")
    for attempt in third:
        if attempt.model_alias != "deepseek-v4-flash":
            problems.append("third-line extractor alias must be deepseek-v4-flash")
    problems.extend(_check_fabricated_text_gated(run))
    return problems


def _check_fabricated_text_gated(run: ScenarioRun) -> list[str]:
    from store import read_ledger_records_for_run

    problems = []
    for record in read_ledger_records_for_run(run.db_path, run.result.run_id):
        if FABRICATED_QUOTE_MARKER in record.approved_claim_text:
            problems.append("fabricated quotation reached the Ledger")
        if FABRICATED_QUOTE_MARKER in record.approved_factual_statement:
            problems.append("fabricated statement reached the Ledger")
    if run.result.final_brief is not None and FABRICATED_QUOTE_MARKER in run.result.final_brief:
        problems.append("fabricated text reached the released brief")
    return problems


def _check_reviewer_revision(run: ScenarioRun) -> list[str]:
    from store import read_statement_reviews_for_run

    reviews = read_statement_reviews_for_run(run.db_path, run.result.run_id)
    rejected = [review for review in reviews if not review.approved]
    problems = []
    if len(rejected) != 1:
        problems.append(f"expected exactly one Reviewer rejection, found {len(rejected)}")
    if run.result.ledger_count != 2:
        problems.append("revised statement should still enter the Ledger")
    return problems


def _check_reviewer_double_reject(run: ScenarioRun) -> list[str]:
    from store import read_statement_reviews_for_run

    reviews = read_statement_reviews_for_run(run.db_path, run.result.run_id)
    rejected = [review for review in reviews if not review.approved]
    problems = []
    if len(rejected) != 2:
        problems.append(f"expected two Reviewer rejections, found {len(rejected)}")
    if run.result.ledger_count != 1:
        problems.append("twice-rejected quote block must not enter the Ledger")
    return problems


def _check_analyst_rejection(run: ScenarioRun) -> list[str]:
    from store import read_analyst_decisions_for_run

    decisions = read_analyst_decisions_for_run(run.db_path, run.result.run_id)
    rejected = [decision for decision in decisions if not decision.approved]
    problems = []
    if not rejected:
        problems.append("expected at least one Analyst rejection")
    if run.result.ledger_count != 1:
        problems.append("rejected evidence must not enter the Ledger")
    return problems


def _check_injection_resisted(run: ScenarioRun) -> list[str]:
    problems = []
    if run.result.final_brief is None:
        problems.append("injection scenario should still release the legitimate evidence")
        return problems
    if INJECTION_SENTENCE in run.result.final_brief:
        problems.append("prompt-injection text reached the released brief")
    extractor_requests = run.llm.stage_requests(LLMStage.EXTRACTOR)
    for request in extractor_requests:
        if UNTRUSTED_TEXT_BEGIN not in request.input_payload:
            problems.append("extractor input was not labeled untrusted")
        if UNTRUSTED_TEXT_NOTICE not in request.input_payload:
            problems.append("extractor input was missing the untrusted notice")
    from store import read_ledger_records_for_run

    for record in read_ledger_records_for_run(run.db_path, run.result.run_id):
        if INJECTION_SENTENCE in record.approved_factual_statement:
            problems.append("prompt-injection text reached the Ledger")
    return problems


def _check_correlated_reviewer_fallback(run: ScenarioRun) -> list[str]:
    from store import read_ledger_records_for_run

    records = read_ledger_records_for_run(run.db_path, run.result.run_id)
    correlated = [
        record for record in records if record.analyst_model_name == record.reviewer_model_name
    ]
    if not correlated:
        return ["expected a same-model Analyst/Reviewer correlated case to be observable"]
    return []


def build_scenario(name: str) -> PipelineScenario:
    if name == "primary_success":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=StageLLM,
            checks=[_check_all_primary],
        )
    if name == "transient_retry":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(
                scripts={LLMStage.EXTRACTOR: [LLMTimeoutError("transient blip")]}
            ),
            checks=[_check_retry_without_fallback],
        )
    if name == "backup_fallback":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(
                scripts={LLMStage.EXTRACTOR: ["not json", '{"wrong": 1}']}
            ),
            checks=[_check_backup_fallback_recorded],
        )
    if name == "exact_quote_escalation":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(scripts={LLMStage.EXTRACTOR: [bad_quote_block_json()]}),
            checks=[_check_exact_quote_escalation],
        )
    if name == "semantic_no_escalation":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(scripts={LLMStage.EXTRACTOR: [empty_quote_block_json()]}),
            checks=[_check_no_extractor_escalation],
        )
    if name == "third_line_availability":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(
                scripts={
                    LLMStage.EXTRACTOR: [
                        LLMTimeoutError("t1"),
                        LLMTimeoutError("t2"),
                        LLMTimeoutError("t3"),
                        LLMTimeoutError("t4"),
                        bad_quote_block_json(),
                    ]
                }
            ),
            checks=[_check_third_line_gated],
        )
    if name == "reviewer_revision":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(scripts={LLMStage.REVIEWER: [reviewer_reject_payload()]}),
            checks=[_check_reviewer_revision],
        )
    if name == "reviewer_double_reject":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(
                scripts={LLMStage.REVIEWER: [reviewer_reject_payload(), reviewer_reject_payload()]}
            ),
            checks=[_check_reviewer_double_reject],
        )
    if name == "analyst_reject_low_scores":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(
                scripts={LLMStage.ANALYST: [analyst_payload(evidence_quality=2, claim_fit=2)]}
            ),
            checks=[_check_analyst_rejection],
        )
    if name == "prompt_injection":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=StageLLM,
            scraper_factory=lambda: FakeScraper(injection_page),
            checks=[_check_injection_resisted],
        )
    if name == "reviewer_fallback_same_model":
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(
                scripts={
                    LLMStage.REVIEWER: [
                        LLMTimeoutError("r1"),
                        LLMTimeoutError("r2"),
                    ]
                }
            ),
            checks=[_check_correlated_reviewer_fallback],
        )
    if name == "one_side_search_failure":
        return PipelineScenario(
            name=name,
            expected_status="failed",
            llm_factory=StageLLM,
            search_factory=lambda: FakeSearch(fail_stances={"opposing"}),
        )
    if name == "analyst_outage":
        return PipelineScenario(
            name=name,
            expected_status="failed",
            llm_factory=lambda: StageLLM(
                scripts={LLMStage.ANALYST: [LLMProviderError("analyst down")] * 6}
            ),
        )
    if name == "mutation_base":
        # One qualified-only Partial record and one Strong secondary record,
        # giving the mutation attacks both template classes to target.
        return PipelineScenario(
            name=name,
            expected_status="released",
            llm_factory=lambda: StageLLM(
                scripts={
                    LLMStage.ANALYST: [
                        analyst_payload(evidence_quality=4, claim_fit=3, entailment="Partial")
                    ]
                }
            ),
        )
    raise KeyError(f"unknown pipeline scenario: {name}")


# ---------------------------------------------------------------------------
# Mutation attacks against the final validator
# ---------------------------------------------------------------------------


def _mutate_first_item(
    synthesis: SynthesisOutput,
    mutator: Callable[[object], object],
) -> SynthesisOutput:
    section = synthesis.sections[0]
    item = mutator(section.items[0])
    new_section = section.model_copy(update={"items": [item, *section.items[1:]]})
    return synthesis.model_copy(update={"sections": [new_section, *synthesis.sections[1:]]})


def attack_altered_statement(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    return _mutate_first_item(
        synthesis,
        lambda item: item.model_copy(
            update={
                "approved_factual_statement": item.approved_factual_statement
                + " Extra unapproved words."
            }
        ),
    )


def attack_paraphrased_statement(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    return _mutate_first_item(
        synthesis,
        lambda item: item.model_copy(
            update={
                "approved_factual_statement": item.approved_factual_statement.replace(
                    "reported", "proved"
                )
            }
        ),
    )


def attack_placement_drift(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    return _mutate_first_item(
        synthesis,
        lambda item: item.model_copy(
            update={
                "placement": (
                    Placement.PRIMARY
                    if item.placement is not Placement.PRIMARY
                    else Placement.SECONDARY
                )
            }
        ),
    )


def attack_entailment_drift(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    return _mutate_first_item(
        synthesis,
        lambda item: item.model_copy(
            update={
                "entailment": (
                    Entailment.WEAK if item.entailment is not Entailment.WEAK else Entailment.STRONG
                )
            }
        ),
    )


def attack_wrong_reviewer_approval(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    return _mutate_first_item(
        synthesis, lambda item: item.model_copy(update={"reviewer_approval_id": uuid4()})
    )


def attack_unknown_ledger_claim(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    return _mutate_first_item(
        synthesis, lambda item: item.model_copy(update={"ledger_claim_id": uuid4()})
    )


def attack_unapproved_template(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    return _mutate_first_item(
        synthesis,
        lambda item: item.model_copy(update={"connective_template_id": "free_prose"}),
    )


def attack_qualified_promotion(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    """Give a qualified/warning item a plain evidence template."""
    for section_index, section in enumerate(synthesis.sections):
        for item_index, item in enumerate(section.items):
            if item.connective_template_id in {
                "partial_entailment",
                "weak_entailment",
                "scope_qualification",
                "reliability_qualification",
            }:
                plain = (
                    "supporting_evidence"
                    if item.stance.value == "supporting"
                    else "opposing_evidence"
                )
                mutated_item = item.model_copy(update={"connective_template_id": plain})
                items = list(section.items)
                items[item_index] = mutated_item
                new_section = section.model_copy(update={"items": items})
                sections = list(synthesis.sections)
                sections[section_index] = new_section
                return synthesis.model_copy(update={"sections": sections})
    raise AssertionError("mutation base run has no qualified/warning item to attack")


def attack_duplicate_claim_use(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    section = synthesis.sections[0]
    duplicated = section.model_copy(update={"items": [*section.items, section.items[0]]})
    return synthesis.model_copy(update={"sections": [duplicated, *synthesis.sections[1:]]})


def attack_hidden_prose_field(
    synthesis: SynthesisOutput, ledgers: list[LedgerRecord]
) -> SynthesisOutput:
    section = synthesis.sections[0]
    item = section.items[0].model_copy()
    object.__setattr__(item, "hidden_note", "Smuggled factual prose.")
    new_section = section.model_copy(update={"items": [item, *section.items[1:]]})
    return synthesis.model_copy(update={"sections": [new_section, *synthesis.sections[1:]]})


MUTATION_ATTACKS: dict[str, Callable[[SynthesisOutput, list[LedgerRecord]], SynthesisOutput]] = {
    "altered_statement": attack_altered_statement,
    "paraphrased_statement": attack_paraphrased_statement,
    "placement_drift": attack_placement_drift,
    "entailment_drift": attack_entailment_drift,
    "wrong_reviewer_approval": attack_wrong_reviewer_approval,
    "unknown_ledger_claim": attack_unknown_ledger_claim,
    "unapproved_template": attack_unapproved_template,
    "qualified_promotion": attack_qualified_promotion,
    "duplicate_claim_use": attack_duplicate_claim_use,
    "hidden_prose_field": attack_hidden_prose_field,
}
