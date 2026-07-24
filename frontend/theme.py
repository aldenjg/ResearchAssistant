"""Presentation primitives for the local Streamlit frontend.

Pure HTML builders live here so views stay declarative.  Every builder escapes
untrusted text (claims, statements, scraped quotes, URLs) before it reaches the
page.  Nothing in this module reads or writes pipeline state.
"""

from __future__ import annotations

import sys
from html import escape
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from models import Stage  # noqa: E402

CSS_PATH = Path(__file__).resolve().parent / "assets" / "app.css"

STAGE_LABELS: dict[Stage, str] = {
    Stage.CLAIM_PLANNER: "Planner",
    Stage.SUPPORTING_RESEARCHER: "Supporting",
    Stage.OPPOSING_RESEARCHER: "Opposing",
    Stage.EVIDENCE_ANALYST: "Analyst",
    Stage.STATEMENT_REVIEWER: "Reviewer",
    Stage.CLAIM_LEDGER: "Ledger",
    Stage.DEBATE_SYNTHESIZER: "Synthesizer",
    Stage.FINAL_RENDERER_VALIDATOR: "Validator",
}

_TONES = frozenset({"released", "blocked", "failed", "running", "neutral"})


def apply_theme(page_title: str) -> None:
    """Configure the page and inject the dossier stylesheet."""
    st.set_page_config(
        page_title=page_title,
        page_icon="◆",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(f"<style>{_load_css()}</style>", unsafe_allow_html=True)


def write(markup: str) -> None:
    """Render a builder's HTML into the page."""
    st.markdown(markup, unsafe_allow_html=True)


def _load_css() -> str:
    if not CSS_PATH.is_file():
        raise FileNotFoundError(f"stylesheet is missing: {CSS_PATH}")
    return CSS_PATH.read_text(encoding="utf-8")


def _tone(tone: str) -> str:
    if tone not in _TONES:
        raise ValueError(f"unknown tone: {tone}")
    return tone


def brand() -> str:
    return (
        '<div class="brand"><div class="brand-mark">DR</div><div class="brand-text">'
        '<div class="brand-title">Debate Research</div>'
        '<div class="brand-sub">Agent System</div></div></div>'
    )


def page_head(eyebrow: str, title: str, subtitle: str) -> str:
    return (
        f'<div class="page-head"><div class="eyebrow">{escape(eyebrow)}</div>'
        f'<h1 class="page-title">{escape(title)}</h1>'
        f'<div class="page-sub">{escape(subtitle)}</div></div>'
    )


def section_head(title: str) -> str:
    return (
        f'<div class="section-head"><div class="section-title">{escape(title)}</div>'
        '<div class="section-rule"></div></div>'
    )


def pill(label: str, tone: str) -> str:
    return f'<span class="pill pill-{_tone(tone)}"><span class="dot"></span>{escape(label)}</span>'


def badge(label: str, *, accent: bool = False) -> str:
    css = "badge badge-accent" if accent else "badge"
    return f'<span class="{css}">{escape(label)}</span>'


def hash_chip(value: str | None, *, keep: int = 8) -> str:
    if value is None:
        return '<span class="mono">none</span>'
    if len(value) <= keep * 2:
        shown = value
    else:
        shown = f"{value[:keep]}…{value[-keep:]}"
    return f'<span class="hash" title="{escape(value)}">{escape(shown)}</span>'


def stat(label: str, value: str, *, note: str = "", small: bool = False) -> str:
    value_css = "stat-value small" if small else "stat-value"
    note_html = f'<div class="stat-note">{escape(note)}</div>' if note else ""
    return (
        f'<div class="stat"><div class="stat-label">{escape(label)}</div>'
        f'<div class="{value_css}">{escape(value)}</div>{note_html}</div>'
    )


def stat_grid(cards: list[str]) -> str:
    return f'<div class="stat-grid">{"".join(cards)}</div>'


def verdict(
    *,
    tone: str,
    status_label: str,
    claim: str,
    badges: list[str],
    meta: str,
) -> str:
    badge_html = "".join(badges)
    return (
        f'<div class="verdict verdict-{_tone(tone)}">'
        f'<div class="verdict-row">{pill(status_label, tone)}{badge_html}</div>'
        f'<div class="verdict-claim">{escape(claim)}</div>'
        f'<div class="verdict-meta">{meta}</div></div>'
    )


def funnel(rows: list[tuple[str, int]]) -> str:
    """Proportional attrition bars; the first row is the 100% reference."""
    if not rows:
        return '<div class="funnel"><div class="funnel-label">no stages recorded</div></div>'
    top = max(rows[0][1], 1)
    last_index = len(rows) - 1
    parts: list[str] = []
    for index, (label, count) in enumerate(rows):
        width = min(100.0, (count / top) * 100.0)
        share = f"{(count / top) * 100:.0f}%" if top else "0%"
        terminal = " terminal" if index == last_index else ""
        parts.append(
            f'<div class="funnel-row"><div class="funnel-label">{escape(label)}</div>'
            f'<div class="funnel-track"><div class="funnel-bar{terminal}" '
            f'style="width:{width:.2f}%"></div></div>'
            f'<div class="funnel-value">{count}<span>{share}</span></div></div>'
        )
    return f'<div class="funnel">{"".join(parts)}</div>'


def score_meters(evidence_quality: int, claim_fit: int) -> str:
    return (
        '<div class="meters">'
        f"{_meter('Evidence quality', evidence_quality)}"
        f"{_meter('Claim fit', claim_fit)}"
        "</div>"
    )


def _meter(label: str, score: int) -> str:
    dots: list[str] = []
    for position in range(1, 6):
        if position > score:
            dots.append('<span class="meter-dot"></span>')
            continue
        strength = "high" if score >= 4 else "low" if score <= 2 else ""
        dots.append(f'<span class="meter-dot on {strength}"></span>')
    return (
        f'<div><div class="meter-label">{escape(label)}</div>'
        f'<div class="meter-dots">{"".join(dots)}'
        f'<span class="meter-num">{score}/5</span></div></div>'
    )


def stage_timeline(current_stage: Stage, *, stopped: bool) -> str:
    stages = list(Stage)
    current_index = stages.index(current_stage)
    parts: list[str] = []
    for index, stage in enumerate(stages):
        if index < current_index:
            css = "tl-step done"
        elif index == current_index:
            css = "tl-step current stopped" if stopped else "tl-step current"
        else:
            css = "tl-step"
        parts.append(
            f'<div class="{css}"><div class="tl-bar"></div>'
            f'<div class="tl-name">{escape(STAGE_LABELS[stage])}</div></div>'
        )
    return f'<div class="timeline">{"".join(parts)}</div>'


def kv(rows: list[tuple[str, str]]) -> str:
    """Definition list; values are pre-escaped HTML fragments from callers."""
    body = "".join(f"<dt>{escape(label)}</dt><dd>{value}</dd>" for label, value in rows)
    return f'<dl class="kv">{body}</dl>'


def card(body: str, *, quiet: bool = False) -> str:
    css = "card-quiet" if quiet else "card"
    return f'<div class="{css}">{body}</div>'


def statement(text: str) -> str:
    return f'<div class="statement">{escape(text)}</div>'


def quote(text: str) -> str:
    return f'<div class="quote">{escape(text)}</div>'


def rationale(text: str) -> str:
    return f'<div class="rationale">{escape(text)}</div>'


def source_link(url: str) -> str:
    safe = escape(url, quote=True)
    return f'<div class="src"><a href="{safe}" target="_blank" rel="noopener">{safe}</a></div>'


def table(headers: list[str], rows: list[list[str]]) -> str:
    """Rows carry pre-escaped HTML cells so callers can mark status colours."""
    head = "".join(f"<th>{escape(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return (
        f'<div class="tbl-wrap"><table class="tbl"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def brief_html(brief: str) -> str:
    """Wrap the renderer's fixed Markdown shape in styled tags.

    The released text is never altered; headings and bullets only gain tags.
    """
    parts: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            parts.append("<ul>" + "".join(f"<li>{item}</li>" for item in bullets) + "</ul>")
            bullets.clear()

    for line in brief.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            flush()
            parts.append(f"<h2>{escape(stripped[3:])}</h2>")
        elif stripped.startswith("# "):
            flush()
            parts.append(f"<h1>{escape(stripped[2:])}</h1>")
        elif stripped.startswith("- "):
            bullets.append(escape(stripped[2:]))
        elif stripped:
            flush()
            parts.append(f"<p>{escape(stripped)}</p>")
    flush()
    return f'<div class="brief">{"".join(parts)}</div>'


def empty_state(title: str, hint: str) -> str:
    return (
        f'<div class="empty"><div class="empty-title">{escape(title)}</div>'
        f'<div class="empty-hint">{escape(hint)}</div></div>'
    )


def integrity_line(matches: bool, message: str) -> str:
    css = "integrity" if matches else "integrity bad"
    mark = "✓" if matches else "✕"
    return f'<div class="{css}">{mark} {escape(message)}</div>'
