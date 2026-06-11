"""Render an AuditResult: ANSI terminal report, Markdown, or JSON."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .models import AuditResult, Finding, GamingSeverity, Verdict

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"

_VERDICT_STYLE = {
    Verdict.VERIFIED: (_GREEN, "✓", "VERIFIED"),
    Verdict.STALE: (_YELLOW, "◐", "STALE"),
    Verdict.UNVERIFIED: (_YELLOW, "?", "UNVERIFIED"),
    Verdict.CONTRADICTED: (_RED, "✗", "CONTRADICTED"),
}

_SEVERITY_STYLE = {
    GamingSeverity.HIGH: (_RED, "HIGH"),
    GamingSeverity.MEDIUM: (_YELLOW, "MED"),
    GamingSeverity.LOW: (_DIM, "LOW"),
}


def _colors_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _paint(text: str, *styles: str, enabled: bool = True) -> str:
    if not enabled or not styles:
        return text
    return "".join(styles) + text + _RESET


def render_terminal(result: AuditResult, color: bool | None = None) -> str:
    color = _colors_enabled() if color is None else color
    session = result.session
    lines: list[str] = []
    out = lines.append

    title = session.slug or Path(session.path).stem[:12]
    out("")
    out(_paint("  agent-receipts", _BOLD, _CYAN, enabled=color)
        + _paint(" — claims vs. reality", _DIM, enabled=color))
    out(_paint(f"  session {title} · {len(session.events)} events"
               + (f" · {session.cwd}" if session.cwd else ""),
               _DIM, enabled=color))
    out("")

    if result.score is None:
        out("  no checkable claims found in this session — nothing to audit.")
        out("")
        return "\n".join(lines)

    score_style = _GREEN if result.score >= 80 else (
        _YELLOW if result.score >= 60 else _RED)
    out(f"  {_paint('RECEIPTS SCORE', _BOLD, enabled=color)}  "
        + _paint(f"{result.score}/100 ({result.grade})", _BOLD, score_style,
                 enabled=color))
    counts = result.counts()
    out(_paint(
        f"  {counts['verified']} verified · {counts['stale']} stale · "
        f"{counts['unverified']} unverified · {counts['contradicted']} contradicted"
        + (f" · {len(result.gaming_signals)} gaming signal(s)"
           if result.gaming_signals else ""),
        _DIM, enabled=color))
    out("")

    out(_paint("  CLAIMS", _BOLD, enabled=color))
    for finding in result.findings:
        style, symbol, label = _VERDICT_STYLE[finding.verdict]
        out(f"  {_paint(symbol + ' ' + label.ljust(12), style, enabled=color)}"
            f" “{finding.claim.quote[:110]}”")
        out(_paint(f"    └─ {finding.evidence}", _DIM, enabled=color))
    out("")

    if result.gaming_signals:
        out(_paint("  GAMING SIGNALS", _BOLD, enabled=color))
        for signal in result.gaming_signals:
            style, label = _SEVERITY_STYLE[signal.severity]
            out(f"  {_paint('⚠ ' + label.ljust(5), style, enabled=color)}"
                f" {signal.description}")
        out("")

    return "\n".join(lines)


def _finding_dict(finding: Finding) -> dict:
    return {
        "type": finding.claim.type.value,
        "quote": finding.claim.quote,
        "detail": finding.claim.detail,
        "verdict": finding.verdict.value,
        "evidence": finding.evidence,
        "claim_event": finding.claim.event_index,
        "evidence_event": finding.evidence_index,
    }


def render_json(result: AuditResult) -> str:
    return json.dumps({
        "transcript": result.session.path,
        "session_id": result.session.session_id,
        "cwd": result.session.cwd,
        "score": result.score,
        "grade": result.grade,
        "counts": result.counts(),
        "claims": [_finding_dict(f) for f in result.findings],
        "gaming_signals": [{
            "kind": s.kind,
            "severity": s.severity.value,
            "description": s.description,
            "event": s.event_index,
        } for s in result.gaming_signals],
    }, indent=2)


def render_markdown(result: AuditResult) -> str:
    lines = [
        "# agent-receipts audit",
        "",
        f"- **Transcript:** `{Path(result.session.path).name}`",
        f"- **Project:** `{result.session.cwd or 'unknown'}`",
        f"- **Score:** {result.score if result.score is not None else 'n/a'}"
        f"/100 ({result.grade})",
        "",
        "## Claims",
        "",
        "| Verdict | Claim | Evidence |",
        "|---|---|---|",
    ]
    for finding in result.findings:
        _, symbol, label = _VERDICT_STYLE[finding.verdict]
        quote = finding.claim.quote.replace("|", "\\|")
        evidence = finding.evidence.replace("|", "\\|")
        lines.append(f"| {symbol} {label} | {quote} | {evidence} |")
    if result.gaming_signals:
        lines += ["", "## Gaming signals", ""]
        for signal in result.gaming_signals:
            lines.append(f"- **{signal.severity.value.upper()}** — {signal.description}")
    lines.append("")
    return "\n".join(lines)
