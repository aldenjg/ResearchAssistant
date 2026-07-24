"""Start a live research run and watch it progress.

This is the only page that spends money: starting a run makes live web searches,
scrapes real pages, and calls the configured model endpoint. Every control here
states that before it acts.
"""

from __future__ import annotations

import sys
import time
from html import escape
from pathlib import Path
from uuid import UUID

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from frontend import theme  # noqa: E402
from frontend.run_launcher import (  # noqa: E402
    DEFAULT_MAX_MODEL_ATTEMPTS,
    LiveRunRequest,
    PreflightReport,
    RunHandle,
    RunProgress,
    preflight,
    progress,
    start_run,
)
from frontend.streamlit_app import NAV_REQUEST_KEY, RUNS_PAGE  # noqa: E402
from frontend.views.runs_view import SELECTED_RUN_KEY  # noqa: E402
from models import Stage  # noqa: E402
from orchestrator import LiveRunResult  # noqa: E402
from providers.search import SEARCH_VENDORS  # noqa: E402
from store import read_run  # noqa: E402

ACTIVE_RUN_KEY = "active_run_handle"
RESUME_REQUEST_KEY = "resume_run_request"
POLL_SECONDS = 1.5

_OUTCOME_TONES = {
    "released": "released",
    "blocked": "blocked",
    "failed": "failed",
    "cancelled": "neutral",
}

_RESUME_NOTE = (
    "Resuming continues from persisted artifacts. Completed stages, snapshots, "
    "and Ledger records are reused, not repeated."
)

_SPEND_NOTE = (
    "Starting makes outbound web requests and calls your model endpoint. The "
    "recorded reference run used 22 model attempts and about 53,000 tokens. "
    "Cancelling is honored at the next stage boundary, so a stage already in "
    "flight finishes first."
)

_RUNNING_NOTE = (
    "Live searches, scrapes, and model calls are in flight. Leaving this page "
    "does not stop the run; it finishes and persists on its own."
)


def render(db_path: str) -> None:
    """Render the live-run launcher, or the monitor for the active run."""
    handle = st.session_state.get(ACTIVE_RUN_KEY)
    if isinstance(handle, RunHandle):
        _render_monitor(handle)
        return

    theme.write(
        theme.page_head(
            "Live run",
            "Research a new claim",
            "Runs the full pipeline against the live web: six planned searches, "
            "18 retrieval attempts, extraction, scoring, review, and the final "
            "release gate.",
        )
    )

    report = _render_preflight(db_path)
    resume_id = st.session_state.get(RESUME_REQUEST_KEY)
    if resume_id is not None:
        _render_resume_form(db_path, report, UUID(resume_id))
        return
    _render_start_form(db_path, report)


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def _render_preflight(db_path: str) -> PreflightReport:
    vendor = st.session_state.get("launch_vendor")
    report = preflight(vendor)

    theme.write(theme.section_head("Configuration"))
    rows = [
        ("Search vendor", _mono(report.vendor)),
        ("Run database", _mono(db_path)),
    ]
    for alias, model_name in sorted(report.model_map.items()):
        rows.append((f"Model · {alias}", _mono(model_name)))
    status = (
        theme.pill("ready", "released") if report.ready else theme.pill("not configured", "blocked")
    )
    theme.write(theme.card(f"<div>{status}</div>{theme.kv(rows)}"))

    for problem in report.problems:
        theme.write(theme.card(theme.rationale(problem), quiet=True))
    if not report.ready:
        theme.write(
            theme.empty_state(
                "Live runs are unavailable until the configuration above is complete",
                "Set the missing variables in .env (see .env.example), then reload.",
            )
        )
    return report


# ---------------------------------------------------------------------------
# Start and resume forms
# ---------------------------------------------------------------------------


def _render_start_form(db_path: str, report: PreflightReport) -> None:
    theme.write(theme.section_head("Claim"))
    claim = st.text_area(
        "Claim text",
        placeholder="Remote work increased productivity in the United States.",
        height=110,
        help="The exact claim to research. It is stored verbatim with the run.",
    )

    vendor_col, budget_col = st.columns(2)
    with vendor_col:
        st.selectbox(
            "Search vendor",
            sorted(SEARCH_VENDORS),
            index=sorted(SEARCH_VENDORS).index(report.vendor)
            if report.vendor in SEARCH_VENDORS
            else 0,
            key="launch_vendor",
        )
    with budget_col:
        attempts = st.number_input(
            "Model-attempt budget",
            min_value=1,
            max_value=500,
            value=DEFAULT_MAX_MODEL_ATTEMPTS,
            step=10,
            help="The run fails explicitly when this budget is exhausted.",
        )

    _render_spend_notice()
    acknowledged = st.checkbox(
        "I understand this run makes live web and model calls that cost money.",
        key="launch_ack",
    )

    disabled = not report.ready or not acknowledged or not claim.strip()
    if st.button("Start run", type="primary", disabled=disabled):
        _launch(
            LiveRunRequest(
                raw_claim=claim.strip(),
                db_path=db_path,
                search_vendor=st.session_state.get("launch_vendor"),
                max_model_attempts=int(attempts),
            )
        )


def _render_resume_form(db_path: str, report: PreflightReport, run_id: UUID) -> None:
    try:
        manifest = read_run(db_path, run_id)
    except KeyError:
        st.session_state.pop(RESUME_REQUEST_KEY, None)
        theme.write(theme.empty_state("That run no longer exists", str(run_id)))
        return

    theme.write(theme.section_head("Resume run"))
    theme.write(
        theme.card(
            f"<div>{theme.pill('resume', 'running')}"
            f"{theme.badge(str(run_id).split('-')[0])}"
            f"{theme.badge(f'stopped at · {manifest.current_stage.value}')}</div>"
            f"{theme.statement(manifest.raw_claim)}"
            f"{theme.rationale(_RESUME_NOTE)}"
        )
    )

    _render_spend_notice()
    acknowledged = st.checkbox(
        "I understand resuming makes live web and model calls that cost money.",
        key="resume_ack",
    )

    resume_col, cancel_col = st.columns([1, 4])
    with resume_col:
        if st.button(
            "Resume run",
            type="primary",
            disabled=not report.ready or not acknowledged,
        ):
            st.session_state.pop(RESUME_REQUEST_KEY, None)
            _launch(
                LiveRunRequest(
                    raw_claim=manifest.raw_claim,
                    db_path=db_path,
                    search_vendor=st.session_state.get("launch_vendor"),
                    resume_run_id=run_id,
                )
            )
    with cancel_col:
        if st.button("Never mind"):
            st.session_state.pop(RESUME_REQUEST_KEY, None)
            st.rerun()


def _render_spend_notice() -> None:
    theme.write(
        theme.card(
            f"<div>{theme.pill('spends credits', 'blocked')}</div>{theme.rationale(_SPEND_NOTE)}",
            quiet=True,
        )
    )


def _launch(request: LiveRunRequest) -> None:
    st.session_state[ACTIVE_RUN_KEY] = start_run(request)
    st.rerun()


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


def _render_monitor(handle: RunHandle) -> None:
    running = handle.is_running()
    snapshot = progress(handle.db_path, handle.run_id)
    outcome = handle.outcome()
    error = handle.error()

    theme.write(
        theme.page_head(
            "Live run",
            "Run in progress" if running else "Run finished",
            handle.raw_claim,
        )
    )

    if error is not None:
        theme.write(
            theme.card(
                f"<div>{theme.pill('launch failed', 'failed')}</div>{theme.rationale(error)}"
            )
        )
    elif outcome is not None:
        theme.write(_outcome_card(handle, outcome))
    else:
        theme.write(_running_card(handle))

    if snapshot is not None:
        theme.write(theme.stat_grid(_progress_stats(handle, snapshot)))
        theme.write(theme.section_head("Pipeline stages"))
        theme.write(
            theme.stage_timeline(
                Stage(snapshot.current_stage),
                stopped=not running and (outcome is None or outcome.status != "released"),
            )
        )
        theme.write(theme.section_head("Evidence funnel"))
        theme.write(theme.funnel(snapshot.funnel_rows))

    _render_monitor_controls(handle, running)

    if running:
        time.sleep(POLL_SECONDS)
        st.rerun()


def _render_monitor_controls(handle: RunHandle, running: bool) -> None:
    if running:
        if handle.cancel_requested():
            theme.write(
                theme.card(
                    theme.rationale(
                        "Cancellation requested. The stage in flight finishes first; "
                        "the run stops at the next stage boundary."
                    ),
                    quiet=True,
                )
            )
            return
        if st.button("Cancel run"):
            handle.cancel()
            st.rerun()
        return

    open_col, again_col = st.columns([1, 4])
    with open_col:
        if st.button("Open in Runs", type="primary"):
            st.session_state[SELECTED_RUN_KEY] = str(handle.run_id)
            st.session_state[NAV_REQUEST_KEY] = RUNS_PAGE
            st.session_state.pop(ACTIVE_RUN_KEY, None)
            st.rerun()
    with again_col:
        if st.button("Start another run"):
            st.session_state.pop(ACTIVE_RUN_KEY, None)
            st.rerun()


def _running_card(handle: RunHandle) -> str:
    badges = [theme.badge(str(handle.run_id).split("-")[0])]
    if handle.resumed:
        badges.append(theme.badge("resumed", accent=True))
    return theme.card(
        f"<div>{theme.pill('running', 'running')}{''.join(badges)}</div>"
        f"{theme.rationale(_RUNNING_NOTE)}"
    )


def _outcome_card(handle: RunHandle, outcome: LiveRunResult) -> str:
    tone = _OUTCOME_TONES.get(outcome.status, "neutral")
    badges = [theme.badge(str(handle.run_id).split("-")[0])]
    if handle.resumed:
        badges.append(theme.badge("resumed", accent=True))

    detail = ""
    if outcome.failure_reason is not None:
        detail = theme.rationale(outcome.failure_reason)
    elif outcome.rendered_brief_hash is not None:
        detail = (
            f'<div class="verdict-meta">hash {theme.hash_chip(outcome.rendered_brief_hash)}</div>'
        )
    elif outcome.validation_result is not None and not outcome.validation_result.valid:
        detail = theme.rationale(
            f"{len(outcome.validation_result.errors)} validator error(s) blocked release."
        )
    return theme.card(f"<div>{theme.pill(outcome.status, tone)}{''.join(badges)}</div>{detail}")


def _progress_stats(handle: RunHandle, snapshot: RunProgress) -> list[str]:
    return [
        theme.stat("Stage", snapshot.current_stage.replace("_", " "), small=True),
        theme.stat("Snapshots", str(snapshot.snapshot_count)),
        theme.stat("Candidates", str(snapshot.candidate_count)),
        theme.stat("Ledger records", str(snapshot.ledger_count)),
        theme.stat(
            "Model attempts",
            str(snapshot.model_attempt_count),
            note=f"budget {handle.max_model_attempts}",
        ),
        theme.stat(
            "Tokens",
            f"{snapshot.input_tokens + snapshot.output_tokens:,}",
            note=f"{snapshot.input_tokens:,} in · {snapshot.output_tokens:,} out",
        ),
    ]


def _mono(value: str) -> str:
    return f'<span class="mono">{escape(value)}</span>'
