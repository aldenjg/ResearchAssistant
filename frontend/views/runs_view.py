"""Read-only browser over persisted runs.

Every widget on this page reads.  Nothing here starts a run, calls a provider,
reaches the network, or writes to the database.
"""

from __future__ import annotations

import sys
from datetime import datetime
from html import escape
from pathlib import Path
from uuid import UUID

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from frontend import theme  # noqa: E402
from frontend.run_reader import (  # noqa: E402
    OUTCOME_TONES,
    DatabaseNotFoundError,
    RunCard,
    RunDetail,
    list_run_cards,
    load_run_detail,
)
from models import (  # noqa: E402
    CandidateQuoteBlock,
    LedgerRecord,
    RetrievalRecord,
    ScoreDecision,
    SourceSnapshot,
    StageModelAttempt,
    StatementReviewResult,
)

SELECTED_RUN_KEY = "selected_run_id"


def render(db_path: str) -> None:
    """Render the run list, or the detail page for the selected run."""
    selected = st.session_state.get(SELECTED_RUN_KEY)
    try:
        cards = list_run_cards(db_path)
    except DatabaseNotFoundError:
        _render_missing_database(db_path)
        return
    except KeyError as exc:
        _render_unreadable_database(str(exc))
        return

    known = {card.run_id for card in cards}
    if selected in known:
        _render_detail(db_path, UUID(selected))
        return
    if selected is not None:
        st.session_state.pop(SELECTED_RUN_KEY, None)
    _render_list(cards, db_path)


# ---------------------------------------------------------------------------
# Run list
# ---------------------------------------------------------------------------


def _render_list(cards: list[RunCard], db_path: str) -> None:
    theme.write(
        theme.page_head(
            "Run archive",
            "Persisted runs",
            "Every run this database has recorded, newest first. Each one is "
            "reconstructed from its own audited artifacts.",
        )
    )
    if not cards:
        theme.write(
            theme.empty_state(
                "No runs recorded yet",
                f'python cli.py run "Your claim here." --db {db_path}',
            )
        )
        return

    theme.write(theme.stat_grid(_archive_stats(cards)))
    theme.write(theme.section_head("Runs"))
    for card in cards:
        row, action = st.columns([9, 2], vertical_alignment="center")
        with row:
            theme.write(_run_card(card))
        with action:
            if st.button("Inspect →", key=f"open-{card.run_id}", use_container_width=True):
                st.session_state[SELECTED_RUN_KEY] = card.run_id
                st.rerun()


def _archive_stats(cards: list[RunCard]) -> list[str]:
    released = len([card for card in cards if card.outcome == "released"])
    ledger_total = sum(card.ledger_count for card in cards)
    return [
        theme.stat("Runs", str(len(cards))),
        theme.stat("Released", str(released)),
        theme.stat("Ledger records", str(ledger_total)),
        theme.stat("Latest", _short_date(cards[0].created_at), small=True),
    ]


def _run_card(card: RunCard) -> str:
    tone = OUTCOME_TONES[card.outcome]
    meta = (
        f'<div class="run-meta"><span>{escape(_short_uuid(card.run_id))}</span>'
        f"<span>stage · {escape(card.current_stage)}</span>"
        f"<span>ledger · {card.ledger_count}</span>"
        f"<span>{escape(_short_date(card.created_at))}</span></div>"
    )
    return (
        '<div class="run-card"><div class="run-card-top">'
        f"{theme.pill(card.outcome, tone)}</div>"
        f'<div class="run-claim">{escape(card.raw_claim)}</div>{meta}</div>'
    )


# ---------------------------------------------------------------------------
# Run detail
# ---------------------------------------------------------------------------


def _render_detail(db_path: str, run_id: UUID) -> None:
    detail = load_run_detail(db_path, run_id)

    back_col, resume_col = st.columns([1, 4])
    with back_col:
        if st.button("← All runs", key="back-to-runs"):
            st.session_state.pop(SELECTED_RUN_KEY, None)
            st.rerun()
    with resume_col:
        if detail.outcome in {"failed", "cancelled"}:
            _render_resume_button(run_id)

    theme.write(_verdict(detail))
    theme.write(theme.stat_grid(_detail_stats(detail)))
    theme.write(theme.section_head("Pipeline stages"))
    theme.write(
        theme.stage_timeline(
            detail.manifest.current_stage,
            stopped=detail.outcome in {"failed", "cancelled"},
        )
    )
    theme.write(theme.section_head("Evidence funnel"))
    theme.write(theme.funnel(detail.funnel_rows))

    brief_tab, ledger_tab, evidence_tab, decisions_tab, validation_tab, attempts_tab = st.tabs(
        ["Brief", "Ledger", "Evidence", "Decisions", "Validation", "Model attempts"]
    )
    with brief_tab:
        _render_brief(detail)
    with ledger_tab:
        _render_ledger(detail.ledger_records)
    with evidence_tab:
        _render_evidence(detail)
    with decisions_tab:
        _render_decisions(detail)
    with validation_tab:
        _render_validation(detail)
    with attempts_tab:
        _render_attempts(detail.attempts)


def _render_resume_button(run_id: UUID) -> None:
    """Hand an unfinished run to the launcher page for confirmation.

    Resuming spends credits, so this only navigates; the launcher owns the
    single confirmation step that actually starts a run.
    """
    from frontend.streamlit_app import LAUNCH_PAGE, NAV_REQUEST_KEY
    from frontend.views.launch_view import RESUME_REQUEST_KEY

    if st.button("Resume this run", key=f"resume-{run_id}"):
        st.session_state[RESUME_REQUEST_KEY] = str(run_id)
        st.session_state[NAV_REQUEST_KEY] = LAUNCH_PAGE
        st.session_state.pop(SELECTED_RUN_KEY, None)
        st.rerun()


def _verdict(detail: RunDetail) -> str:
    validation = detail.validation
    badges = [
        theme.badge(_short_uuid(str(detail.manifest.run_id))),
        theme.badge(f"stage · {detail.manifest.current_stage.value}"),
    ]
    if detail.ledger_records:
        badges.append(theme.badge(f"ledger · {len(detail.ledger_records)}", accent=True))

    meta_parts = [f"started {escape(_short_date(detail.manifest.created_at.isoformat()))}"]
    if detail.manifest.completed_at is not None:
        meta_parts.append(
            f"completed {escape(_short_date(detail.manifest.completed_at.isoformat()))}"
        )
    if validation is not None and validation.rendered_brief_hash is not None:
        meta_parts.append(f"hash {theme.hash_chip(validation.rendered_brief_hash)}")
    elif validation is not None:
        meta_parts.append(f"{len(validation.errors)} validator error(s)")

    return theme.verdict(
        tone=OUTCOME_TONES[detail.outcome],
        status_label=detail.outcome,
        claim=detail.manifest.raw_claim,
        badges=badges,
        meta=" · ".join(meta_parts),
    )


def _detail_stats(detail: RunDetail) -> list[str]:
    input_tokens = sum(attempt.input_tokens or 0 for attempt in detail.attempts)
    output_tokens = sum(attempt.output_tokens or 0 for attempt in detail.attempts)
    retries = len([attempt for attempt in detail.attempts if attempt.attempt_number > 1])
    fallbacks = len([attempt for attempt in detail.attempts if attempt.route_position > 0])
    return [
        theme.stat("Retrievals", str(len(detail.retrievals))),
        theme.stat("Snapshots", str(len(detail.snapshots))),
        theme.stat("Candidates", str(len(detail.candidates))),
        theme.stat("Ledger records", str(len(detail.ledger_records))),
        theme.stat(
            "Model attempts",
            str(len(detail.attempts)),
            note=f"{retries} retries · {fallbacks} fallbacks",
        ),
        theme.stat(
            "Tokens",
            f"{input_tokens + output_tokens:,}",
            note=f"{input_tokens:,} in · {output_tokens:,} out",
        ),
    ]


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def _render_brief(detail: RunDetail) -> None:
    if detail.final_brief is None:
        theme.write(
            theme.empty_state(
                "No released brief",
                "A brief exists only after the final validator passes.",
            )
        )
        return

    theme.write(theme.brief_html(detail.final_brief))
    validation = detail.validation
    persisted = validation.rendered_brief_hash if validation is not None else None
    if detail.brief_hash_matches:
        message = "Re-rendered text reproduces the validator's released hash."
    else:
        message = "Re-rendered text does not match the persisted validator hash."
    theme.write(theme.integrity_line(detail.brief_hash_matches, message))

    with st.expander("Exact released text and hashes"):
        st.code(detail.final_brief, language="markdown")
        st.code(
            f"persisted  {persisted}\nrecomputed {detail.recomputed_brief_hash}",
            language="text",
        )


def _render_ledger(records: list[LedgerRecord]) -> None:
    if not records:
        theme.write(
            theme.empty_state(
                "No Ledger records",
                "Only exact Reviewer-approved statements are admitted to the Ledger.",
            )
        )
        return
    for record in records:
        theme.write(_ledger_card(record))


def _ledger_card(record: LedgerRecord) -> str:
    badges = "".join(
        [
            theme.badge(record.stance.value, accent=True),
            theme.badge(f"placement · {record.placement.value}"),
            theme.badge(f"entailment · {record.entailment.value}"),
            theme.badge(f"ledger score · {record.ledger_score}"),
        ]
    )
    rows = [
        ("Source", theme.source_link(record.source_url)),
        ("Snapshot", theme.hash_chip(record.snapshot_sha256)),
        ("Ledger claim id", _mono(str(record.ledger_claim_id))),
        ("Reviewer approval", _mono(str(record.reviewer_approval_id))),
        ("Analyst", _mono(f"{record.analyst_model_name} · {record.analyst_prompt_version}")),
        ("Reviewer", _mono(f"{record.reviewer_model_name} · {record.reviewer_prompt_version}")),
        ("Admitted", _mono(_short_date(record.ledger_validated_at.isoformat()))),
    ]
    body = (
        f"<div>{badges}</div>"
        f"{theme.statement(record.approved_factual_statement)}"
        f"{theme.score_meters(record.evidence_quality, record.claim_fit)}"
        f"{theme.kv(rows)}"
    )
    return theme.card(body)


def _render_evidence(detail: RunDetail) -> None:
    theme.write(theme.section_head("Retrieval attempts"))
    if detail.retrievals:
        theme.write(_retrieval_table(detail.retrievals))
    else:
        theme.write(theme.empty_state("No retrieval attempts", "The run stopped before retrieval."))

    theme.write(theme.section_head("Trusted snapshots"))
    if detail.snapshots:
        for snapshot in detail.snapshots:
            _render_snapshot(snapshot)
    else:
        theme.write(theme.empty_state("No snapshots", "Nothing was scraped and frozen yet."))

    theme.write(theme.section_head("Gate-passing candidates"))
    if detail.candidates:
        for candidate in detail.candidates:
            theme.write(_candidate_card(candidate))
    else:
        theme.write(
            theme.empty_state(
                "No candidates passed the quotation gate",
                "Extractions must match their snapshot exactly to become candidates.",
            )
        )


def _retrieval_table(retrievals: list[RetrievalRecord]) -> str:
    rows: list[list[str]] = []
    for record in sorted(retrievals, key=lambda item: (item.query_round, item.search_rank)):
        status_css = "ok" if record.status.value == "retrieved" else "bad"
        rows.append(
            [
                str(record.query_round),
                str(record.search_rank),
                f'<span class="{status_css}">{escape(record.status.value)}</span>',
                escape(_truncate(record.query_text, 58)),
                f'<span title="{escape(record.resolved_url, quote=True)}">'
                f"{escape(_truncate(record.resolved_url, 52))}</span>",
            ]
        )
    return theme.table(["Round", "Rank", "Status", "Query", "Resolved URL"], rows)


def _render_snapshot(snapshot: SourceSnapshot) -> None:
    label = f"{_truncate(snapshot.source_url, 72)} · {snapshot.word_count:,} words"
    with st.expander(label):
        rows = [
            ("Snapshot id", _mono(str(snapshot.snapshot_id))),
            ("Content hash", theme.hash_chip(snapshot.snapshot_sha256)),
            ("Truncated", _mono(str(snapshot.truncated).lower())),
            ("Retrieved", _mono(_short_date(snapshot.retrieved_at.isoformat()))),
        ]
        theme.write(theme.kv(rows))
        st.text_area(
            "Normalized text",
            snapshot.normalized_text,
            height=260,
            disabled=True,
            key=f"snap-{snapshot.snapshot_id}",
        )


def _candidate_card(candidate: CandidateQuoteBlock) -> str:
    badges = "".join(
        [
            theme.badge(candidate.stance.value, accent=True),
            theme.badge(f"round · {candidate.query_round}"),
            theme.badge(f"rank · {candidate.search_rank}"),
            theme.badge(f"{candidate.raw_segment_word_count} words"),
            theme.badge(f"keywords · {candidate.claim_keyword_match_count}"),
            theme.badge(
                "statistical markers" if candidate.has_statistical_markers else "no markers"
            ),
        ]
    )
    offsets = ", ".join(
        f"{offset.start_char}–{offset.end_char}" for offset in candidate.segment_offsets
    )
    extractor = f"{candidate.extraction_model_name} · {candidate.extraction_prompt_version}"
    rows = [
        ("Source", theme.source_link(candidate.source_url)),
        ("Snapshot", theme.hash_chip(candidate.snapshot_sha256)),
        ("Segment offsets", _mono(offsets)),
        ("Extractor", _mono(extractor)),
    ]
    body = f"<div>{badges}</div>{theme.quote(candidate.extracted_quote_block)}{theme.kv(rows)}"
    return theme.card(body)


def _render_decisions(detail: RunDetail) -> None:
    theme.write(theme.section_head("Analyst decisions"))
    if detail.decisions:
        for decision in detail.decisions:
            theme.write(_decision_card(decision))
    else:
        theme.write(theme.empty_state("No Analyst decisions", "The run stopped before scoring."))

    theme.write(theme.section_head("Statement Reviewer results"))
    if detail.reviews:
        for review in detail.reviews:
            theme.write(_review_card(review))
    else:
        theme.write(theme.empty_state("No Reviewer results", "No draft reached the Reviewer."))


def _decision_card(decision: ScoreDecision) -> str:
    tone = "released" if decision.approved else "blocked"
    label = "approved" if decision.approved else "rejected"
    placement = decision.placement.value if decision.placement is not None else "none"
    head = (
        f"{theme.pill(label, tone)}"
        f"{theme.badge(f'placement · {placement}')}"
        f"{theme.badge(f'quote · {_short_uuid(str(decision.quote_block_id))}')}"
    )
    body = (
        f"<div>{head}</div>"
        f"{theme.score_meters(decision.evidence_quality, decision.claim_fit)}"
        f"{theme.rationale(decision.rationale)}"
    )
    return theme.card(body, quiet=True)


def _review_card(review: StatementReviewResult) -> str:
    tone = "released" if review.approved else "blocked"
    label = "approved" if review.approved else review.failure_code or "rejected"
    head = (
        f"{theme.pill(str(label), tone)}"
        f"{theme.badge(f'quote · {_short_uuid(str(review.quote_block_id))}')}"
        f"{theme.badge(f'draft · {_short_uuid(str(review.statement_draft_id))}')}"
    )
    statement = (
        theme.statement(review.approved_factual_statement)
        if review.approved_factual_statement is not None
        else ""
    )
    body = f"<div>{head}</div>{statement}{theme.rationale(review.rationale)}"
    return theme.card(body, quiet=True)


def _render_validation(detail: RunDetail) -> None:
    validation = detail.validation
    if validation is None:
        theme.write(
            theme.empty_state(
                "No validation result",
                "The run never reached the final renderer and validator.",
            )
        )
        return

    rows = [
        ("Validator config", _mono(validation.validator_config_version)),
        ("Validated at", _mono(_short_date(validation.validated_at.isoformat()))),
        ("Rendered hash", theme.hash_chip(validation.rendered_brief_hash)),
    ]
    tone = "released" if validation.valid else "blocked"
    label = "valid" if validation.valid else "blocked"
    theme.write(theme.card(f"<div>{theme.pill(label, tone)}</div>{theme.kv(rows)}"))
    for error in validation.errors:
        body = (
            f"<div>{theme.pill(error.code.value, 'blocked')}"
            f"{theme.badge(error.location)}</div>"
            f"{theme.rationale(error.message)}"
        )
        theme.write(theme.card(body, quiet=True))


def _render_attempts(attempts: list[StageModelAttempt]) -> None:
    if not attempts:
        theme.write(theme.empty_state("No model attempts", "No stage invoked a model."))
        return

    route_names = {0: "primary", 1: "backup", 2: "fallback"}
    rows: list[list[str]] = []
    for attempt in attempts:
        status_css = "ok" if attempt.status == "succeeded" else "bad"
        tokens = f"{attempt.input_tokens or 0:,}/{attempt.output_tokens or 0:,}"
        rows.append(
            [
                escape(attempt.stage),
                escape(_truncate(attempt.work_unit, 30)),
                escape(attempt.model_alias),
                escape(route_names.get(attempt.route_position, str(attempt.route_position))),
                str(attempt.attempt_number),
                f'<span class="{status_css}">{escape(attempt.status)}</span>',
                f"{attempt.latency_ms:,} ms",
                tokens,
            ]
        )
    theme.write(
        theme.table(
            ["Stage", "Work unit", "Alias", "Route", "Try", "Status", "Latency", "Tokens in/out"],
            rows,
        )
    )


# ---------------------------------------------------------------------------
# Error states and formatting helpers
# ---------------------------------------------------------------------------


def _render_missing_database(db_path: str) -> None:
    theme.write(
        theme.page_head(
            "Run archive",
            "No run database found",
            "Point the sidebar at an existing run database, or create one with a live run.",
        )
    )
    theme.write(
        theme.empty_state(
            f"{db_path} does not exist",
            'python cli.py run "Your claim here."',
        )
    )


def _render_unreadable_database(message: str) -> None:
    theme.write(
        theme.page_head(
            "Run archive",
            "Database is not readable",
            "The file exists but carries no run schema.",
        )
    )
    theme.write(theme.empty_state("Unreadable database", message))


def _mono(value: str) -> str:
    return f'<span class="mono">{escape(value)}</span>'


def _short_uuid(value: str) -> str:
    return value.split("-")[0]


def _short_date(iso_value: str) -> str:
    return datetime.fromisoformat(iso_value).strftime("%Y-%m-%d %H:%M")


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else f"{value[: limit - 1]}…"
