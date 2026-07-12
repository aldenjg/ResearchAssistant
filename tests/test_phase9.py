"""Phase 9 tests: real orchestration with controlled concurrency.

All tests are deterministic and offline: fake LLM/search/scraper providers
only, with a socket guard blocking real network access.
"""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

import cli
import orchestrator
from models import RunStatus, StageModelAttempt
from orchestrator import (
    BudgetExceededError,
    LiveRunResult,
    RunBudgets,
    inspect_run,
    run_live,
)
from providers.llm import (
    UNTRUSTED_TEXT_BEGIN,
    UNTRUSTED_TEXT_END,
    LLMProviderError,
    LLMStage,
    LLMTimeoutError,
    ProviderCapabilities,
    ProviderRequest,
    ProviderResponse,
)
from providers.scraper import ScrapeResponse, ScraperProviderError, ScraperTimeoutError
from providers.search import SearchProviderError, SearchResponse, SearchResult
from store import (
    init_db,
    insert_stage_model_attempt,
    read_ledger_records_for_run,
    read_run,
    read_stage_model_attempts_for_run,
    read_statement_reviews_for_run,
    read_validation,
)

CLAIM = "Remote work increased productivity."

QUOTE_SENTENCE = (
    "The study reported that remote work productivity increased by 42 percent across "
    "1200 surveyed firms during the review period, and the researchers noted that the "
    "measured gains persisted for 18 months in most regions, while the average firm "
    "reported sustained output improvements even though analysts cautioned that the "
    "sample may not represent every industry segment."
)
CLOSING_SENTENCE = "A closing remark follows the analysis."
DRAFT_STATEMENT = (
    "According to the study, surveyed firms reported a 42 percent productivity "
    "increase over 18 months."
)


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def _guard(*args: object, **kwargs: object) -> None:
        raise RuntimeError("network access attempted during offline Phase 9 tests")

    monkeypatch.setattr(socket.socket, "connect", _guard)


class SeqClock:
    """Deterministic, thread-safe monotonic clock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tick = 0
        self._base = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        with self._lock:
            self._tick += 1
            return self._base + timedelta(seconds=self._tick)


def uniform_page(url: str) -> str:
    return f"Context sentence opens the report. {QUOTE_SENTENCE} {CLOSING_SENTENCE}"


def per_round_page(url: str) -> str:
    token = url.split("example.com/", 1)[1].rsplit("/", 1)[0].replace("/", "-")
    return f"Context sentence {token} opens the report. {QUOTE_SENTENCE} {CLOSING_SENTENCE}"


class FakeSearch:
    def __init__(self, *, fail_stances: set[str] | None = None) -> None:
        self.fail_stances = fail_stances or set()
        self.requests: list[object] = []

    def search(self, request: object) -> SearchResponse:
        self.requests.append(request)
        text = request.query_text
        stance = "supporting" if text.startswith("supporting") else "opposing"
        if stance in self.fail_stances:
            raise SearchProviderError(f"{stance} search backend offline")
        round_match = re.search(r"\bq(\d)\b", text)
        assert round_match is not None, f"unexpected query text: {text}"
        query_round = round_match.group(1)
        return SearchResponse(
            results=[
                SearchResult(original_url=f"https://example.com/{stance}/{query_round}/{rank}")
                for rank in (1, 2, 3)
            ]
        )


class FakeScraper:
    def __init__(
        self,
        page_text: callable = uniform_page,
        *,
        timeout_urls: set[str] | None = None,
        fail_urls: set[str] | None = None,
    ) -> None:
        self.page_text = page_text
        self.timeout_urls = timeout_urls or set()
        self.fail_urls = fail_urls or set()
        self.calls: list[str] = []

    def scrape(self, request: object) -> ScrapeResponse:
        url = request.url
        self.calls.append(url)
        if url in self.timeout_urls:
            raise ScraperTimeoutError(f"scrape timed out: {url}")
        if url in self.fail_urls:
            raise ScraperProviderError(f"scrape failed: {url}")
        return ScrapeResponse(
            resolved_url=url,
            content_type="text/html",
            text=self.page_text(url),
        )


def planner_payload() -> str:
    queries = [
        {"stance": stance, "query_round": query_round, "query_text": f"{stance} q{query_round}"}
        for stance in ("supporting", "opposing")
        for query_round in (1, 2, 3)
    ]
    return json.dumps(
        {
            "population": "United States adults",
            "jurisdiction": "United States",
            "time_period": "2020 through 2025",
            "comparison_baseline": "the prior five years",
            "intervention_or_exposure": "remote work adoption",
            "causal_or_comparative_meaning": "asserted causal increase",
            "ambiguities": [],
            "queries": queries,
        }
    )


def analyst_payload() -> str:
    return json.dumps(
        {
            "evidence_quality": 4,
            "claim_fit": 4,
            "entailment": "Strong",
            "draft_statement": DRAFT_STATEMENT,
            "rationale": "Credible methodology and direct relevance.",
        }
    )


def reviewer_approve_payload() -> str:
    return json.dumps(
        {
            "fully_entailed": True,
            "qualifications_preserved": True,
            "neutral_framing": True,
            "claim_fit_scope_valid": True,
            "rationale": "All audit checks pass.",
        }
    )


def reviewer_reject_payload() -> str:
    return json.dumps(
        {
            "fully_entailed": False,
            "qualifications_preserved": True,
            "neutral_framing": True,
            "claim_fit_scope_valid": True,
            "rationale": "Statement adds unsupported inference.",
        }
    )


def synthesizer_payload() -> str:
    return json.dumps(
        {
            "title": "Debate Evidence Brief",
            "supporting_heading": "Supporting Evidence",
            "opposing_heading": "Opposing Evidence",
            "limitations_heading": "Limitations",
        }
    )


def good_quote_block_json() -> str:
    quote = f'[Context sentence opens the report.] "{QUOTE_SENTENCE}" [{CLOSING_SENTENCE}]'
    return json.dumps({"quote_blocks": [quote]})


def bad_quote_block_json() -> str:
    return json.dumps(
        {
            "quote_blocks": [
                '[Nope.] "This text is definitely absent from the snapshot." [Also nope.]'
            ]
        }
    )


class StageLLM:
    """Fake provider with per-stage scripts and valid default responses."""

    def __init__(self, scripts: dict[LLMStage, list[object]] | None = None) -> None:
        self.scripts = {stage: list(items) for stage, items in (scripts or {}).items()}
        self.requests: list[ProviderRequest] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_temperature=True, supports_structured_output=True)

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        queue = self.scripts.get(request.stage)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, str):
                return ProviderResponse(output_text=item, input_tokens=100, output_tokens=20)
            return item  # type: ignore[return-value]
        return ProviderResponse(
            output_text=self._default(request), input_tokens=100, output_tokens=20
        )

    def _default(self, request: ProviderRequest) -> str:
        if request.stage is LLMStage.PLANNER:
            return planner_payload()
        if request.stage is LLMStage.EXTRACTOR:
            payload = json.loads(request.input_payload)
            labeled = payload["labeled_snapshot_text"]
            text = labeled.split(UNTRUSTED_TEXT_BEGIN + "\n", 1)[1]
            text = text.rsplit("\n" + UNTRUSTED_TEXT_END, 1)[0]
            sentences = [s.strip() for s in re.findall(r"[^.!?]+[.!?]", text)]
            quote = f'[{sentences[0]}] "{sentences[1]}" [{sentences[2]}]'
            return json.dumps({"quote_blocks": [quote]})
        if request.stage is LLMStage.ANALYST:
            return analyst_payload()
        if request.stage is LLMStage.REVIEWER:
            return reviewer_approve_payload()
        return synthesizer_payload()

    def stage_requests(self, stage: LLMStage) -> list[ProviderRequest]:
        return [request for request in self.requests if request.stage is stage]


def execute(
    tmp_path: Path,
    *,
    llm: StageLLM | None = None,
    search: FakeSearch | None = None,
    scraper: FakeScraper | None = None,
    db_name: str = "live.sqlite3",
    **kwargs: object,
) -> tuple[LiveRunResult, str, StageLLM]:
    provider = llm or StageLLM()
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_path / db_name)
    result = run_live(
        raw_claim=CLAIM,
        db_path=db_path,
        llm_provider=provider,
        search_provider=search or FakeSearch(),
        scraper_provider=scraper or FakeScraper(),
        clock=kwargs.pop("clock", SeqClock()),
        **kwargs,
    )
    return result, db_path, provider


def attempts_for(db_path: str, run_id: UUID, stage: str) -> list[StageModelAttempt]:
    return [
        attempt
        for attempt in read_stage_model_attempts_for_run(db_path, run_id)
        if attempt.stage == stage
    ]


# ---------------------------------------------------------------------------
# Successful full orchestration
# ---------------------------------------------------------------------------


def test_successful_full_orchestration(tmp_path: Path) -> None:
    result, db_path, llm = execute(tmp_path, scraper=FakeScraper(per_round_page))
    assert result.status == "released"
    assert result.validation_result is not None and result.validation_result.valid
    assert result.rendered_brief_hash is not None
    assert result.final_brief is not None
    assert DRAFT_STATEMENT in result.final_brief
    assert result.ledger_count == 6  # three unique snapshots per side, one quote each
    manifest = read_run(db_path, result.run_id)
    assert manifest.status is RunStatus.COMPLETED
    assert manifest.completed_at is not None
    # Every stage ran on its MiMo-first primary with no fallback.
    for attempt in read_stage_model_attempts_for_run(db_path, result.run_id):
        assert attempt.route_position == 0
        assert attempt.status == "succeeded"
        assert attempt.escalation_reason is None


def test_released_brief_hash_is_deterministic(tmp_path: Path) -> None:
    run_id = UUID("11111111-2222-3333-4444-555555555555")
    first, _, _ = execute(tmp_path / "a", scraper=FakeScraper(per_round_page), run_id=run_id)
    second, _, _ = execute(tmp_path / "b", scraper=FakeScraper(per_round_page), run_id=run_id)
    assert first.status == "released" and second.status == "released"
    assert first.rendered_brief_hash == second.rendered_brief_hash


# ---------------------------------------------------------------------------
# Researcher failures and retrieval behavior
# ---------------------------------------------------------------------------


def test_one_researcher_failure_is_explicit(tmp_path: Path) -> None:
    result, db_path, _ = execute(tmp_path, search=FakeSearch(fail_stances={"opposing"}))
    assert result.status == "failed"
    assert "opposing researcher failed" in result.failure_reason
    assert read_run(db_path, result.run_id).status is RunStatus.FAILED


def test_both_researcher_failures_are_explicit(tmp_path: Path) -> None:
    result, _, _ = execute(tmp_path, search=FakeSearch(fail_stances={"supporting", "opposing"}))
    assert result.status == "failed"
    assert "both researchers failed" in result.failure_reason
    assert "supporting" in result.failure_reason and "opposing" in result.failure_reason


def test_partial_retrieval_success_continues(tmp_path: Path) -> None:
    scraper = FakeScraper(
        timeout_urls={"https://example.com/supporting/1/1"},
        fail_urls={"https://example.com/opposing/2/1"},
    )
    result, db_path, _ = execute(tmp_path, scraper=scraper)
    assert result.status == "released"
    inspection = inspect_run(db_path, result.run_id)
    assert inspection.retrieval_count == 18  # every intended attempt recorded
    assert inspection.snapshot_count >= 1


def test_equal_retrieval_budgets_for_both_sides(tmp_path: Path) -> None:
    search = FakeSearch()
    result, db_path, _ = execute(tmp_path, search=search)
    assert result.status == "released"
    assert len(search.requests) == 6
    assert all(request.limit == 3 for request in search.requests)
    with sqlite3.connect(db_path) as conn:
        counts = dict(
            conn.execute(
                """SELECT sq.stance, COUNT(*)
                   FROM retrieval_attempts ra JOIN search_queries sq
                     ON ra.query_id = sq.query_id
                   WHERE ra.run_id = ? GROUP BY sq.stance""",
                (str(result.run_id),),
            ).fetchall()
        )
    assert counts == {"supporting": 9, "opposing": 9}


def test_no_shared_sqlite_connection_across_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_connect = sqlite3.connect
    connect_threads: set[int] = set()

    def spying_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connect_threads.add(threading.get_ident())
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spying_connect)
    result, _, _ = execute(tmp_path)
    assert result.status == "released"
    # Researcher worker threads never open SQLite connections; the coordinator
    # persists typed artifacts after both workers finish.
    assert connect_threads == {threading.get_ident()}


# ---------------------------------------------------------------------------
# Stage failures
# ---------------------------------------------------------------------------


def test_extraction_failure_fails_run_explicitly(tmp_path: Path) -> None:
    # Permanent provider error on every extractor route alias for both
    # snapshots: 1 attempt per alias, 3 aliases, 2 snapshots.
    llm = StageLLM(scripts={LLMStage.EXTRACTOR: [LLMProviderError("extractor down")] * 6})
    result, db_path, _ = execute(tmp_path, llm=llm)
    assert result.status == "failed"
    assert "extraction produced no admissible candidates" in result.failure_reason
    assert read_run(db_path, result.run_id).status is RunStatus.FAILED


def test_analyst_failure_fails_run_explicitly(tmp_path: Path) -> None:
    llm = StageLLM(scripts={LLMStage.ANALYST: [LLMProviderError("analyst down")] * 6})
    result, _, _ = execute(tmp_path, llm=llm)
    assert result.status == "failed"
    assert "no approved statements entered the Ledger" in result.failure_reason


def test_reviewer_first_failure_then_approval(tmp_path: Path) -> None:
    llm = StageLLM(scripts={LLMStage.REVIEWER: [reviewer_reject_payload()]})
    result, db_path, _ = execute(tmp_path, llm=llm)
    assert result.status == "released"
    assert result.ledger_count == 2
    reviews = read_statement_reviews_for_run(db_path, result.run_id)
    assert len(reviews) == 3  # one rejection, one revision approval, one direct approval
    rejected = [review for review in reviews if not review.approved]
    assert len(rejected) == 1
    assert rejected[0].failure_code is not None


def test_reviewer_second_failure_rejects_quote_block(tmp_path: Path) -> None:
    llm = StageLLM(
        scripts={LLMStage.REVIEWER: [reviewer_reject_payload(), reviewer_reject_payload()]}
    )
    result, db_path, _ = execute(tmp_path, llm=llm)
    assert result.status == "released"
    assert result.ledger_count == 1  # the twice-rejected quote block never enters
    reviews = read_statement_reviews_for_run(db_path, result.run_id)
    assert sum(1 for review in reviews if not review.approved) == 2

    # If every candidate is twice-rejected, the run fails explicitly.
    llm_all = StageLLM(scripts={LLMStage.REVIEWER: [reviewer_reject_payload()] * 4})
    result_all, _, _ = execute(tmp_path, llm=llm_all, db_name="all-rejected.sqlite3")
    assert result_all.status == "failed"
    assert "no approved statements entered the Ledger" in result_all.failure_reason


def test_validator_rejection_blocks_release_without_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build = orchestrator.build_synthesis_output

    def tampering_build(*args: object, **kwargs: object):
        synthesis = real_build(*args, **kwargs)
        section = synthesis.sections[0]
        item = section.items[0]
        tampered_item = item.model_copy(
            update={
                "approved_factual_statement": item.approved_factual_statement
                + " Extra unapproved words."
            }
        )
        tampered_section = section.model_copy(update={"items": [tampered_item, *section.items[1:]]})
        return synthesis.model_copy(
            update={"sections": [tampered_section, *synthesis.sections[1:]]}
        )

    monkeypatch.setattr(orchestrator, "build_synthesis_output", tampering_build)
    result, db_path, _ = execute(tmp_path)
    assert result.status == "blocked"
    assert result.rendered_brief_hash is None
    assert result.final_brief is None
    assert result.validation_result is not None
    assert not result.validation_result.valid
    assert result.validation_result.rendered_brief_hash is None
    codes = {error.code.value for error in result.validation_result.errors}
    assert "altered_statement" in codes
    stored = read_validation(db_path, result.run_id)
    assert not stored.valid and stored.rendered_brief_hash is None
    assert read_run(db_path, result.run_id).status is RunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Retry, fallback, and escalation policy
# ---------------------------------------------------------------------------


def test_primary_transient_retry_before_fallback(tmp_path: Path) -> None:
    llm = StageLLM(scripts={LLMStage.EXTRACTOR: [LLMTimeoutError("blip")]})
    result, db_path, provider = execute(tmp_path, llm=llm)
    assert result.status == "released"
    extractor_aliases = [r.model_alias for r in provider.stage_requests(LLMStage.EXTRACTOR)]
    assert "mimo-v2.5-pro" not in extractor_aliases
    assert "deepseek-v4-flash" not in extractor_aliases
    attempts = attempts_for(db_path, result.run_id, "extractor")
    failed = [attempt for attempt in attempts if attempt.status == "failed"]
    assert len(failed) == 1
    assert "LLMTimeoutError" in failed[0].failure_reason
    retried = [attempt for attempt in attempts if attempt.retry_reason is not None]
    assert len(retried) == 1 and retried[0].attempt_number == 2
    assert all(attempt.route_position == 0 for attempt in attempts)


def test_primary_malformed_output_triggers_recorded_fallback(tmp_path: Path) -> None:
    llm = StageLLM(scripts={LLMStage.EXTRACTOR: ["this is not JSON", '{"wrong": 1}']})
    result, db_path, provider = execute(tmp_path, llm=llm)
    assert result.status == "released"
    extractor_aliases = [r.model_alias for r in provider.stage_requests(LLMStage.EXTRACTOR)]
    assert extractor_aliases.count("mimo-v2.5-pro") == 1
    attempts = attempts_for(db_path, result.run_id, "extractor")
    escalated = [attempt for attempt in attempts if attempt.route_position == 1]
    assert escalated
    assert all("objective invocation failure" in attempt.escalation_reason for attempt in escalated)
    assert any("invalid model output" in attempt.escalation_reason for attempt in escalated)


def test_extractor_objective_escalation_to_mimo_pro_on_exact_quote_failure(
    tmp_path: Path,
) -> None:
    llm = StageLLM(scripts={LLMStage.EXTRACTOR: [bad_quote_block_json()]})
    result, db_path, provider = execute(tmp_path, llm=llm)
    assert result.status == "released"
    assert result.ledger_count == 2
    attempts = attempts_for(db_path, result.run_id, "extractor")
    escalated = [attempt for attempt in attempts if attempt.route_position == 1]
    assert len(escalated) == 1
    assert "exact-quote failure" in escalated[0].escalation_reason
    # The fabricated quotation never became a candidate or Ledger record.
    for record in read_ledger_records_for_run(db_path, result.run_id):
        assert "definitely absent" not in record.approved_claim_text


def test_no_escalation_on_semantic_disagreement_alone(tmp_path: Path) -> None:
    # An empty extraction is a semantic judgment ("no evidence here"), not an
    # objective failure; the extractor must not escalate to a stronger model.
    llm = StageLLM(scripts={LLMStage.EXTRACTOR: [json.dumps({"quote_blocks": []})]})
    result, db_path, provider = execute(tmp_path, llm=llm)
    assert result.status == "released"
    assert result.ledger_count == 1
    extractor_aliases = {r.model_alias for r in provider.stage_requests(LLMStage.EXTRACTOR)}
    assert extractor_aliases == {"mimo-v2.5"}
    attempts = attempts_for(db_path, result.run_id, "extractor")
    assert all(attempt.escalation_reason is None for attempt in attempts)
    assert all(attempt.route_position == 0 for attempt in attempts)


def test_third_line_deepseek_is_availability_only_and_gated(tmp_path: Path) -> None:
    # Primary and backup both unavailable (timeouts) for one snapshot, then
    # DeepSeek returns a fabricated quotation: it must still pass every
    # deterministic gate, so no candidate or Ledger record may result from it.
    llm = StageLLM(
        scripts={
            LLMStage.EXTRACTOR: [
                LLMTimeoutError("t1"),
                LLMTimeoutError("t2"),
                LLMTimeoutError("t3"),
                LLMTimeoutError("t4"),
                bad_quote_block_json(),
            ]
        }
    )
    result, db_path, provider = execute(tmp_path, llm=llm)
    assert result.status == "released"
    assert result.ledger_count == 1  # only the other snapshot produced evidence
    attempts = attempts_for(db_path, result.run_id, "extractor")
    third_line = [attempt for attempt in attempts if attempt.route_position == 2]
    assert len(third_line) == 1
    assert third_line[0].model_alias == "deepseek-v4-flash"
    assert third_line[0].pinned_model_snapshot is None
    assert "objective invocation failure" in third_line[0].escalation_reason
    for record in read_ledger_records_for_run(db_path, result.run_id):
        assert "definitely absent" not in record.approved_claim_text


def test_third_line_not_used_for_quality_failures(tmp_path: Path) -> None:
    # Exact-quote failures on primary and backup are quality failures, not
    # availability failures, so the DeepSeek third line must not be used.
    llm = StageLLM(scripts={LLMStage.EXTRACTOR: [bad_quote_block_json(), bad_quote_block_json()]})
    result, db_path, provider = execute(tmp_path, llm=llm)
    assert result.status == "released"
    assert result.ledger_count == 1
    extractor_aliases = {r.model_alias for r in provider.stage_requests(LLMStage.EXTRACTOR)}
    assert "deepseek-v4-flash" not in extractor_aliases


def test_fallback_attempt_metadata_is_complete_and_restart_safe(tmp_path: Path) -> None:
    llm = StageLLM(scripts={LLMStage.EXTRACTOR: ["not json", "not json either"]})
    result, db_path, _ = execute(tmp_path, llm=llm)
    assert result.status == "released"
    attempts = read_stage_model_attempts_for_run(db_path, result.run_id)
    assert attempts
    for attempt in attempts:
        assert attempt.stage in {"planner", "extractor", "analyst", "reviewer", "synthesizer"}
        assert attempt.model_alias
        assert attempt.attempt_number >= 1
        assert attempt.route_position in {0, 1, 2}
        assert attempt.latency_ms >= 0
        assert attempt.started_at.tzinfo is not None
        if attempt.status == "failed":
            assert attempt.failure_reason
        if attempt.status == "succeeded":
            assert attempt.input_tokens == 100
            assert attempt.output_tokens == 20
    # The audit history survives a database reopen unchanged.
    reread = read_stage_model_attempts_for_run(db_path, result.run_id)
    assert [a.attempt_id for a in reread] == [a.attempt_id for a in attempts]


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def test_model_budget_exceeded_fails_run(tmp_path: Path) -> None:
    result, db_path, _ = execute(tmp_path, budgets=RunBudgets(max_model_attempts=1))
    assert result.status == "failed"
    assert "model attempt budget" in result.failure_reason
    assert read_run(db_path, result.run_id).status is RunStatus.FAILED


def test_retrieval_budget_below_balanced_depth_fails_run(tmp_path: Path) -> None:
    result, _, _ = execute(tmp_path, budgets=RunBudgets(max_retrieval_attempts=6))
    assert result.status == "failed"
    assert "retrieval budget" in result.failure_reason
    assert "balanced" in result.failure_reason


# ---------------------------------------------------------------------------
# Restart, idempotency, cancellation, and inspection
# ---------------------------------------------------------------------------


def test_restart_after_failure_resumes_without_duplicates(tmp_path: Path) -> None:
    run_id = uuid4()
    failing = StageLLM(scripts={LLMStage.ANALYST: [LLMProviderError("outage")] * 6})
    first, db_path, _ = execute(tmp_path, llm=failing, run_id=run_id)
    assert first.status == "failed"
    attempts_before = read_stage_model_attempts_for_run(db_path, run_id)
    failed_analyst_before = [
        a for a in attempts_before if a.stage == "analyst" and a.status == "failed"
    ]
    assert failed_analyst_before

    healthy = StageLLM()
    second = run_live(
        raw_claim=CLAIM,
        db_path=db_path,
        llm_provider=healthy,
        search_provider=FakeSearch(),
        scraper_provider=FakeScraper(),
        run_id=run_id,
        clock=SeqClock(),
    )
    assert second.status == "released"
    # Completed stages were not re-invoked.
    resumed_stages = {request.stage for request in healthy.requests}
    assert LLMStage.PLANNER not in resumed_stages
    assert LLMStage.EXTRACTOR not in resumed_stages
    # No duplicate snapshots, planner rows, or Ledger records.
    inspection = inspect_run(db_path, run_id)
    assert inspection.snapshot_count == 2
    assert inspection.ledger_count == 2
    with sqlite3.connect(db_path) as conn:
        planner_rows = conn.execute(
            "SELECT COUNT(*) FROM planner_outputs WHERE run_id = ?", (str(run_id),)
        ).fetchone()[0]
    assert planner_rows == 1
    # Attempt history from the failed run is preserved and extended.
    attempts_after = read_stage_model_attempts_for_run(db_path, run_id)
    assert len(attempts_after) > len(attempts_before)
    preserved = {a.attempt_id for a in attempts_before}
    assert preserved <= {a.attempt_id for a in attempts_after}


def test_duplicate_retry_of_completed_run_is_idempotent(tmp_path: Path) -> None:
    run_id = uuid4()
    first, db_path, _ = execute(tmp_path, run_id=run_id)
    assert first.status == "released"
    inspection_before = inspect_run(db_path, run_id)

    idle = StageLLM()
    second = run_live(
        raw_claim=CLAIM,
        db_path=db_path,
        llm_provider=idle,
        search_provider=FakeSearch(),
        scraper_provider=FakeScraper(),
        run_id=run_id,
        clock=SeqClock(),
    )
    assert second.status == "released"
    assert second.rendered_brief_hash == first.rendered_brief_hash
    assert idle.requests == []  # no model work is repeated
    inspection_after = inspect_run(db_path, run_id)
    assert inspection_after == inspection_before


def test_cancellation_between_stages(tmp_path: Path) -> None:
    run_id = uuid4()
    calls = {"count": 0}

    def cancel_after_planner() -> bool:
        calls["count"] += 1
        return calls["count"] >= 2  # second checkpoint sits before retrieval

    result, db_path, _ = execute(tmp_path, run_id=run_id, cancel_check=cancel_after_planner)
    assert result.status == "cancelled"
    assert result.failure_reason == "run cancelled between stages"
    assert result.rendered_brief_hash is None
    manifest = read_run(db_path, run_id)
    assert manifest.status is RunStatus.CANCELLED
    inspection = inspect_run(db_path, run_id)
    assert inspection.retrieval_count == 0  # cancelled before researchers started

    # A cancelled run can be resumed cleanly.
    resumed = run_live(
        raw_claim=CLAIM,
        db_path=db_path,
        llm_provider=StageLLM(),
        search_provider=FakeSearch(),
        scraper_provider=FakeScraper(),
        run_id=run_id,
        clock=SeqClock(),
    )
    assert resumed.status == "released"


def test_database_reopening_preserves_run_state(tmp_path: Path) -> None:
    result, db_path, _ = execute(tmp_path)
    assert result.status == "released"
    # Fresh connections read back the same typed artifacts.
    manifest = read_run(db_path, result.run_id)
    assert manifest.status is RunStatus.COMPLETED
    ledgers = read_ledger_records_for_run(db_path, result.run_id)
    assert len(ledgers) == result.ledger_count
    validation = read_validation(db_path, result.run_id)
    assert validation == result.validation_result
    inspection = inspect_run(db_path, result.run_id)
    assert inspection.has_synthesis and inspection.has_validation


def test_every_run_ends_with_explicit_status(tmp_path: Path) -> None:
    scenarios = {
        "released": execute(tmp_path, db_name="ok.sqlite3")[0],
        "failed": execute(
            tmp_path,
            db_name="fail.sqlite3",
            search=FakeSearch(fail_stances={"supporting"}),
        )[0],
    }
    for expected, result in scenarios.items():
        assert result.status == expected
    with pytest.raises(PydanticValidationError):
        LiveRunResult(
            run_id=uuid4(),
            status="failed",
            stage_reached="claim_planner",
            failure_reason=None,  # failed runs must carry an explicit reason
            ledger_count=0,
            model_attempts=[],
        )


def test_cli_inspect_run_command(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    result, db_path, _ = execute(tmp_path)
    exit_code = cli.main(["inspect-run", db_path, str(result.run_id)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "status: completed" in captured.out
    assert f"ledger_records: {result.ledger_count}" in captured.out
    assert cli.main(["inspect-run", db_path, str(uuid4())]) == 1
    assert cli.main(["inspect-run", db_path, "not-a-uuid"]) == 1


# ---------------------------------------------------------------------------
# Compatibility additions: StageModelAttempt model and store round trip
# ---------------------------------------------------------------------------


def test_stage_model_attempt_rejects_extra_and_invalid_fields(tmp_path: Path) -> None:
    now = datetime(2026, 7, 11, tzinfo=UTC)
    base = {
        "run_id": uuid4(),
        "attempt_id": uuid4(),
        "stage": "extractor",
        "work_unit": "extract::x",
        "model_alias": "mimo-v2.5",
        "route_position": 0,
        "attempt_number": 1,
        "status": "failed",
        "failure_reason": "timeout",
        "started_at": now,
        "completed_at": now,
        "latency_ms": 5,
    }
    attempt = StageModelAttempt(**base)
    assert attempt.status == "failed"
    with pytest.raises(PydanticValidationError):
        StageModelAttempt(**base, unexpected_field=1)
    with pytest.raises(PydanticValidationError):
        StageModelAttempt(**{**base, "failure_reason": None})
    with pytest.raises(PydanticValidationError):
        StageModelAttempt(**{**base, "escalation_reason": "not allowed on primary"})
    with pytest.raises(PydanticValidationError):
        StageModelAttempt(**{**base, "status": "succeeded"})


def test_stage_model_attempt_store_round_trip(tmp_path: Path) -> None:
    db_path = str(tmp_path / "attempts.sqlite3")
    init_db(db_path)
    result, live_db, _ = execute(tmp_path, db_name="seed.sqlite3")
    attempts = read_stage_model_attempts_for_run(live_db, result.run_id)
    assert attempts
    # Round-trip one attempt through a fresh database.
    from models import RunManifest, Stage

    now = datetime(2026, 7, 11, tzinfo=UTC)
    from store import insert_run

    insert_run(
        db_path,
        RunManifest(
            run_id=result.run_id,
            status=RunStatus.RUNNING,
            raw_claim=CLAIM,
            current_stage=Stage.CLAIM_PLANNER,
            created_at=now,
            updated_at=now,
        ),
    )
    insert_stage_model_attempt(db_path, attempts[0])
    reread = read_stage_model_attempts_for_run(db_path, result.run_id)
    assert reread == [attempts[0]]


def test_budget_error_is_orchestrator_error() -> None:
    assert issubclass(BudgetExceededError, orchestrator.OrchestratorError)
    assert RunStatus.CANCELLED.value == "cancelled"
