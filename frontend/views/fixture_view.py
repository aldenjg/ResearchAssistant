"""Deterministic fixture pipeline page.

The pipeline call is unchanged Phase 6 behavior: the same fixture discovery,
the same `run_fixture_pipeline` execution, the same typed summary.  Only the
presentation differs.
"""

from __future__ import annotations

import sys
from html import escape
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from frontend import theme  # noqa: E402
from frontend.streamlit_app import (  # noqa: E402
    FrontendRunSummary,
    discover_fixture_runs,
    run_fixture_for_frontend,
)

SUMMARY_KEY = "phase7a_summary"


def render() -> None:
    """Render the fixture runner and the summary of the last fixture run."""
    theme.write(
        theme.page_head(
            "Offline pipeline",
            "Fixture pipeline",
            "Run the deterministic fixture pipeline end to end. No network, no "
            "model calls: identical inputs always produce an identical brief hash.",
        )
    )

    fixture_options = discover_fixture_runs()
    if not fixture_options:
        theme.write(
            theme.empty_state(
                "No fixture runs found",
                "Expected fixture directories under tests/fixtures/",
            )
        )
        return

    selector, action = st.columns([4, 1], vertical_alignment="bottom")
    with selector:
        selected = st.selectbox(
            "Fixture run",
            fixture_options,
            format_func=lambda option: option.name,
        )
    with action:
        run_clicked = st.button("Run pipeline", type="primary", use_container_width=True)

    if run_clicked:
        with st.spinner("Running fixture pipeline..."):
            summary = run_fixture_for_frontend(selected.path)
        st.session_state[SUMMARY_KEY] = summary.model_dump(mode="json")

    payload = st.session_state.get(SUMMARY_KEY)
    if payload is None:
        theme.write(
            theme.empty_state(
                "No fixture run yet",
                "Pick a fixture and run the pipeline to see its release verdict.",
            )
        )
        return
    _render_summary(FrontendRunSummary.model_validate(payload))


def _render_summary(summary: FrontendRunSummary) -> None:
    theme.write(_verdict(summary))
    theme.write(theme.stat_grid(_stats(summary)))

    theme.write(theme.section_head("Final brief"))
    if summary.final_brief is not None:
        theme.write(theme.brief_html(summary.final_brief))
        with st.expander("Exact released text and hash"):
            st.code(summary.final_brief, language="markdown")
            st.code(summary.validation.rendered_brief_hash or "none", language="text")
    else:
        theme.write(
            theme.empty_state(
                "Release blocked",
                summary.block_reason or "The final validator rejected this run.",
            )
        )

    if summary.validation.errors:
        theme.write(theme.section_head("Validator errors"))
        for error in summary.validation.errors:
            body = (
                f"<div>{theme.pill(error.code, 'blocked')}{theme.badge(error.location)}</div>"
                f"{theme.rationale(error.message)}"
            )
            theme.write(theme.card(body, quiet=True))

    theme.write(theme.section_head("Artifact funnel"))
    theme.write(theme.funnel(_funnel_rows(summary)))

    theme.write(theme.section_head("Provenance"))
    theme.write(theme.card(theme.kv(_metadata_rows(summary))))

    theme.write(theme.section_head("Audit trail"))
    theme.write(_audit_table(summary))


def _verdict(summary: FrontendRunSummary) -> str:
    released = summary.status == "released"
    tone = "released" if released else "blocked"
    badges = [
        theme.badge(summary.metadata.fixture_name, accent=True),
        theme.badge(f"run · {summary.run_id.split('-')[0]}"),
    ]
    meta = f"validator {escape(summary.validation.validator_config_version)}"
    if summary.validation.rendered_brief_hash is not None:
        meta = f"{meta} · hash {theme.hash_chip(summary.validation.rendered_brief_hash)}"
    else:
        meta = f"{meta} · {len(summary.validation.errors)} error(s)"
    return theme.verdict(
        tone=tone,
        status_label=summary.status,
        claim=summary.raw_claim,
        badges=badges,
        meta=meta,
    )


def _stats(summary: FrontendRunSummary) -> list[str]:
    counts = summary.counts
    return [
        theme.stat("Snapshots", str(counts.snapshots)),
        theme.stat("Candidates", str(counts.candidates)),
        theme.stat("Ledger records", str(counts.ledger_records)),
        theme.stat(
            "Synthesis items",
            str(counts.synthesis_items),
            note=f"{counts.synthesis_sections} sections",
        ),
        theme.stat("Audit entries", str(counts.audit_entries)),
    ]


def _funnel_rows(summary: FrontendRunSummary) -> list[tuple[str, int]]:
    counts = summary.counts
    return [
        ("Retrieval attempts", counts.retrievals),
        ("Trusted snapshots", counts.snapshots),
        ("Extractions", counts.provisional_candidates),
        ("Candidates", counts.candidates),
        ("Analyst decisions", counts.analyst_decisions),
        ("Ledger records", counts.ledger_records),
    ]


def _metadata_rows(summary: FrontendRunSummary) -> list[tuple[str, str]]:
    metadata = summary.metadata
    return [
        ("Planner", _mono(f"{metadata.planner_model_name} · {metadata.planner_prompt_version}")),
        (
            "Synthesizer",
            _mono(f"{metadata.synthesizer_model_name} · {metadata.synthesizer_prompt_version}"),
        ),
        ("Validated at", _mono(summary.validation.validated_at)),
        ("Validation artifact", theme.hash_chip(summary.validation.validation_artifact_hash)),
        ("Database", _mono(metadata.db_path)),
        ("Audit log", _mono(metadata.audit_path)),
        ("Result", _mono(metadata.result_path)),
    ]


_FAILING_AUDIT_STATUSES = frozenset({"blocked", "failed", "rejected", "error"})


def _audit_table(summary: FrontendRunSummary) -> str:
    rows: list[list[str]] = []
    for entry in summary.audit_trail:
        status_css = "bad" if entry.status in _FAILING_AUDIT_STATUSES else "ok"
        rows.append(
            [
                escape(entry.stage),
                f'<span class="{status_css}">{escape(entry.status)}</span>',
                escape(entry.artifact_ref),
                str(entry.artifact_count),
                theme.hash_chip(entry.artifact_hash, keep=6),
                escape(entry.outcome),
            ]
        )
    return theme.table(["Stage", "Status", "Artifact", "Count", "Hash", "Outcome"], rows)


def _mono(value: str) -> str:
    return f'<span class="mono">{escape(value)}</span>'
