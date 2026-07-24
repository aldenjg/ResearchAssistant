"""Start and monitor live provider-backed runs from the frontend.

This module owns the only write path the frontend has: it drives the existing
`orchestrator.run_live()` on a worker thread through the same provider seam
`cli.py` uses, so every deterministic gate, budget, retry, and validator behaves
identically to a CLI run. Streamlit is deliberately not imported here so the
launcher stays testable headlessly with injected fake providers.

A live run makes outbound web requests and spends model credits. Callers are
responsible for asking the user first.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import Field

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cli  # noqa: E402
from models import StrictModel  # noqa: E402
from orchestrator import LiveRunResult, RunBudgets, inspect_run, run_live  # noqa: E402
from providers.llm import (  # noqa: E402
    LLMProvider,
    missing_llm_configuration,
    model_map_from_env,
)
from providers.scraper import ScraperProvider  # noqa: E402
from providers.search import (  # noqa: E402
    SearchProvider,
    missing_search_configuration,
    resolve_search_vendor,
)
from store import read_stage_model_attempts_for_run  # noqa: E402

ProviderFactory = Callable[[str | None], tuple[LLMProvider, SearchProvider, ScraperProvider]]

DEFAULT_MAX_MODEL_ATTEMPTS = RunBudgets().max_model_attempts


class PreflightReport(StrictModel):
    """Whether a live run can be configured, without exposing secret values."""

    vendor: str = Field(min_length=1)
    problems: list[str]
    model_map: dict[str, str]

    @property
    def ready(self) -> bool:
        return not self.problems


class LiveRunRequest(StrictModel):
    """One requested live run."""

    raw_claim: str = Field(min_length=1)
    db_path: str = Field(min_length=1)
    search_vendor: str | None = None
    max_model_attempts: int = Field(default=DEFAULT_MAX_MODEL_ATTEMPTS, ge=1)
    resume_run_id: UUID | None = None


class RunProgress(StrictModel):
    """Live snapshot of a run's persisted progress."""

    run_id: str = Field(min_length=1)
    status: str = Field(min_length=1)
    current_stage: str = Field(min_length=1)
    retrieval_count: int = Field(ge=0)
    snapshot_count: int = Field(ge=0)
    provisional_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    analyst_decision_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    ledger_count: int = Field(ge=0)
    model_attempt_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    has_synthesis: bool
    has_validation: bool

    @property
    def funnel_rows(self) -> list[tuple[str, int]]:
        return [
            ("Retrieval attempts", self.retrieval_count),
            ("Trusted snapshots", self.snapshot_count),
            ("Extractions", self.provisional_count),
            ("Gate-passing candidates", self.candidate_count),
            ("Analyst decisions", self.analyst_decision_count),
            ("Ledger records", self.ledger_count),
        ]


@dataclass
class _RunState:
    """Worker-thread result, guarded so the UI thread can read it safely."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    result: LiveRunResult | None = None
    error: str | None = None

    def set_result(self, result: LiveRunResult) -> None:
        with self.lock:
            self.result = result

    def set_error(self, message: str) -> None:
        with self.lock:
            self.error = message

    def read(self) -> tuple[LiveRunResult | None, str | None]:
        with self.lock:
            return self.result, self.error


@dataclass
class RunHandle:
    """Control surface for one in-flight or finished live run."""

    run_id: UUID
    db_path: str
    raw_claim: str
    max_model_attempts: int
    resumed: bool
    _thread: threading.Thread
    _cancel: threading.Event
    _state: _RunState

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def wait(self, timeout: float | None = None) -> None:
        """Block until the run finishes or *timeout* seconds elapse."""
        self._thread.join(timeout)

    def cancel(self) -> None:
        """Request cancellation; honored at the next stage boundary."""
        self._cancel.set()

    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def outcome(self) -> LiveRunResult | None:
        """The typed result once the run finished, else None."""
        return self._state.read()[0]

    def error(self) -> str | None:
        """A launcher-level failure (for example provider configuration)."""
        return self._state.read()[1]


def preflight(search_vendor: str | None = None) -> PreflightReport:
    """Report whether live configuration is complete, naming what is missing.

    Only reports presence: API key values are never read into the report.
    """
    problems = [*missing_llm_configuration(), *missing_search_configuration(search_vendor)]
    try:
        model_map = model_map_from_env()
    except Exception as exc:  # noqa: BLE001 - a bad map must not break the page
        problems.append(f"{type(exc).__name__}: {exc}")
        model_map = {}
    return PreflightReport(
        vendor=resolve_search_vendor(search_vendor),
        problems=problems,
        model_map=model_map,
    )


def start_run(
    request: LiveRunRequest,
    *,
    provider_factory: ProviderFactory | None = None,
) -> RunHandle:
    """Start a live run on a worker thread and return its control handle.

    The run ID is assigned before the thread starts so progress can be polled
    immediately. Resuming passes the existing run ID straight to `run_live()`,
    which continues from persisted artifacts without duplicating completed work.
    """
    run_id = request.resume_run_id or uuid4()
    cancel = threading.Event()
    state = _RunState()

    def worker() -> None:
        try:
            factory = provider_factory or cli._build_live_providers
            llm, search, scraper = factory(request.search_vendor)
            result = run_live(
                raw_claim=request.raw_claim,
                db_path=request.db_path,
                llm_provider=llm,
                search_provider=search,
                scraper_provider=scraper,
                budgets=RunBudgets(max_model_attempts=request.max_model_attempts),
                run_id=run_id,
                cancel_check=cancel.is_set,
            )
            state.set_result(result)
        except Exception as exc:  # noqa: BLE001 - surfaced on the handle, never swallowed
            state.set_error(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=worker, name=f"live-run-{run_id}", daemon=True)
    thread.start()
    return RunHandle(
        run_id=run_id,
        db_path=request.db_path,
        raw_claim=request.raw_claim,
        max_model_attempts=request.max_model_attempts,
        resumed=request.resume_run_id is not None,
        _thread=thread,
        _cancel=cancel,
        _state=state,
    )


def progress(db_path: str, run_id: UUID) -> RunProgress | None:
    """Read a run's persisted progress, or None before it exists.

    Polling never creates a database file: a path that does not exist yet simply
    reports no progress.
    """
    if not Path(db_path).is_file():
        return None
    try:
        inspection = inspect_run(db_path, run_id)
    except (KeyError, sqlite3.Error):
        return None

    attempts = read_stage_model_attempts_for_run(db_path, run_id)
    return RunProgress(
        run_id=str(run_id),
        status=inspection.status.value,
        current_stage=inspection.current_stage.value,
        retrieval_count=inspection.retrieval_count,
        snapshot_count=inspection.snapshot_count,
        provisional_count=inspection.provisional_count,
        candidate_count=inspection.candidate_count,
        analyst_decision_count=inspection.analyst_decision_count,
        review_count=inspection.review_count,
        ledger_count=inspection.ledger_count,
        model_attempt_count=len(attempts),
        input_tokens=sum(attempt.input_tokens or 0 for attempt in attempts),
        output_tokens=sum(attempt.output_tokens or 0 for attempt in attempts),
        has_synthesis=inspection.has_synthesis,
        has_validation=inspection.has_validation,
    )
