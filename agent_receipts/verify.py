"""Verify claims against ground truth.

Ground truth, in order of strength:
1. Tool-call results inside the transcript itself — exit codes and output
   of the commands the agent actually ran.
2. The filesystem and git state of the project right now.

Each claim gets one of four verdicts:
- VERIFIED:     a relevant check ran before the claim, succeeded, and no
                code was edited between the check and the claim.
- STALE:        the check succeeded, but the agent edited code afterwards
                and claimed success without re-running it.
- UNVERIFIED:   the agent never ran anything that could back the claim.
- CONTRADICTED: the most recent relevant check before the claim failed,
                or the claimed artifact does not exist.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .models import (
    Claim, ClaimType, Event, EventKind, Finding, Session, Verdict,
)

# Command shapes that count as evidence for each claim type.
_COMMAND_EVIDENCE: dict[ClaimType, re.Pattern] = {
    ClaimType.TESTS_PASS: re.compile(
        r"\bpytest\b|\bpython(?:3)?(?:\.exe)?\s+-m\s+(?:pytest|unittest)\b"
        r"|\bnpm\s+(?:run\s+)?test\b|\b(?:npx|yarn|pnpm|bun)\s+(?:run\s+)?"
        r"(?:test|jest|vitest|mocha|playwright\s+test)\b"
        r"|\bjest\b|\bvitest\b|\bgo\s+test\b|\bcargo\s+(?:test|nextest)\b"
        r"|\brspec\b|\bphpunit\b|\bdotnet\s+test\b|\bmvn\s+test\b"
        r"|\bgradlew?\s+test\b|\bmix\s+test\b|\bswift\s+test\b|\bctest\b",
        re.IGNORECASE,
    ),
    ClaimType.BUILD_OK: re.compile(
        r"\bnpm\s+run\s+build\b|\b(?:npx|yarn|pnpm|bun)\s+(?:run\s+)?build\b"
        r"|\bcargo\s+build\b|\bgo\s+build\b|\bmake\b|\bdotnet\s+build\b"
        r"|\bmvn\s+(?:package|compile|install)\b|\bgradlew?\s+(?:build|assemble)\b"
        r"|\bvite\s+build\b|\bwebpack\b|\btsc\b.*(?:-b|--build)"
        r"|\bdocker\s+build\b|\bpython(?:3)?(?:\.exe)?\s+-m\s+build\b",
        re.IGNORECASE,
    ),
    ClaimType.LINT_OK: re.compile(
        r"\beslint\b|\bruff\s+(?:check|format)\b|\bflake8\b|\bpylint\b"
        r"|\bgolangci-lint\b|\bclippy\b|\bcargo\s+clippy\b|\bnpm\s+run\s+lint\b"
        r"|\b(?:npx|yarn|pnpm)\s+(?:run\s+)?lint\b|\bbiome\s+(?:check|lint)\b",
        re.IGNORECASE,
    ),
    ClaimType.TYPECHECK_OK: re.compile(
        r"\bmypy\b|\bpyright\b|\btsc\b|\bnpm\s+run\s+type-?check\b"
        r"|\b(?:npx|yarn|pnpm)\s+(?:run\s+)?type-?check\b|\bty\s+check\b",
        re.IGNORECASE,
    ),
    ClaimType.COMMIT_MADE: re.compile(r"\bgit\s+commit\b", re.IGNORECASE),
    ClaimType.PUSHED: re.compile(r"\bgit\s+push\b", re.IGNORECASE),
}

# "N failed" with N > 0, or framework-specific hard failure markers.
_FAILURE_IN_OUTPUT = re.compile(
    r"\b([1-9]\d*)\s+fail(?:ed|ures?)\b"
    r"|\bFAILED\b|\bTests?\s+failed\b|\bBUILD\s+FAILED\b"
    r"|test result: FAILED"
    ,
    re.IGNORECASE,
)


def _command_failed(event: Event) -> bool:
    if event.is_error or (event.exit_code or 0) != 0:
        return True
    return bool(_FAILURE_IN_OUTPUT.search(event.output))


def _evidence_runs(session: Session, pattern: re.Pattern) -> list[Event]:
    return [
        e for e in session.events
        if e.kind is EventKind.TOOL_CALL and pattern.search(e.command)
    ]


def _edits_between(session: Session, start: int, end: int) -> list[Event]:
    return [
        e for e in session.events
        if start < e.index < end
        and e.kind is EventKind.TOOL_CALL and e.is_file_edit()
    ]


def _short(command: str, limit: int = 80) -> str:
    command = " ".join(command.split())
    return command if len(command) <= limit else command[: limit - 1] + "…"


def _verify_command_claim(session: Session, claim: Claim,
                          pattern: re.Pattern) -> Finding:
    runs = [e for e in _evidence_runs(session, pattern)
            if e.index < claim.event_index]
    if not runs:
        return Finding(
            claim=claim, verdict=Verdict.UNVERIFIED,
            evidence="no command that could back this claim was run before it",
        )
    last = runs[-1]
    if _command_failed(last):
        if last.is_error or (last.exit_code or 0) != 0:
            reason = f"exit {last.exit_code if last.exit_code is not None else '?'}"
        else:
            reason = "output reports failures despite exit 0"
        return Finding(
            claim=claim, verdict=Verdict.CONTRADICTED,
            evidence=f"most recent relevant run failed ({reason}): "
                     f"`{_short(last.command)}`",
            evidence_index=last.index,
        )
    edits_after = _edits_between(session, last.index, claim.event_index)
    if edits_after and claim.type in (
        ClaimType.TESTS_PASS, ClaimType.BUILD_OK,
        ClaimType.LINT_OK, ClaimType.TYPECHECK_OK,
    ):
        files = {Path(e.file_path).name for e in edits_after if e.file_path}
        listed = ", ".join(sorted(files)[:3]) or f"{len(edits_after)} files"
        return Finding(
            claim=claim, verdict=Verdict.STALE,
            evidence=(
                f"`{_short(last.command)}` passed, but {listed} "
                f"edited afterwards with no re-run before the claim"
            ),
            evidence_index=last.index,
        )
    return Finding(
        claim=claim, verdict=Verdict.VERIFIED,
        evidence=f"`{_short(last.command)}` succeeded (exit "
                 f"{last.exit_code if last.exit_code is not None else 0})",
        evidence_index=last.index,
    )


def _verify_file_created(session: Session, claim: Claim,
                         check_disk: bool) -> Finding:
    name = Path(claim.detail).name
    writes = [
        e for e in session.events
        if e.kind is EventKind.TOOL_CALL and e.index < claim.event_index
        and e.tool_name in ("Write", "Edit", "NotebookEdit")
        and Path(e.file_path).name == name
    ]
    if not writes:
        return Finding(
            claim=claim, verdict=Verdict.UNVERIFIED,
            evidence=f"no Write/Edit call for `{name}` appears before the claim",
        )
    if check_disk and session.cwd:
        on_disk = (Path(session.cwd) / claim.detail).exists() or any(
            Path(w.file_path).exists() for w in writes if w.file_path
        )
        if not on_disk:
            return Finding(
                claim=claim, verdict=Verdict.CONTRADICTED,
                evidence=f"`{claim.detail}` was written in-session but is not on disk now",
                evidence_index=writes[-1].index,
            )
    return Finding(
        claim=claim, verdict=Verdict.VERIFIED,
        evidence=f"{writes[-1].tool_name} call for `{name}` found"
                 + (" and file exists on disk" if check_disk else ""),
        evidence_index=writes[-1].index,
    )


def _verify_task_done(session: Session, claim: Claim) -> Finding:
    """'Done/fixed/working' is only as good as the checks run after the last edit."""
    edits = [e for e in session.events
             if e.kind is EventKind.TOOL_CALL and e.is_file_edit()
             and e.index < claim.event_index]
    if not edits:
        return Finding(
            claim=claim, verdict=Verdict.UNVERIFIED,
            evidence="no file edits precede this claim; nothing to check it against",
        )
    last_edit = edits[-1].index
    check_patterns = (
        _COMMAND_EVIDENCE[ClaimType.TESTS_PASS],
        _COMMAND_EVIDENCE[ClaimType.BUILD_OK],
        _COMMAND_EVIDENCE[ClaimType.TYPECHECK_OK],
    )
    for pattern in check_patterns:
        for run in _evidence_runs(session, pattern):
            if last_edit < run.index < claim.event_index:
                if _command_failed(run):
                    return Finding(
                        claim=claim, verdict=Verdict.CONTRADICTED,
                        evidence=f"check after final edit failed: `{_short(run.command)}`",
                        evidence_index=run.index,
                    )
                return Finding(
                    claim=claim, verdict=Verdict.VERIFIED,
                    evidence=f"verified after final edit by `{_short(run.command)}`",
                    evidence_index=run.index,
                )
    return Finding(
        claim=claim, verdict=Verdict.UNVERIFIED,
        evidence="no test/build/typecheck ran between the final edit and this claim",
    )


def _git_head_subjects(cwd: str, limit: int = 20) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "log", f"-{limit}", "--format=%H %s"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        return out.stdout.splitlines() if out.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        return []


def verify_claims(session: Session, claims: list[Claim],
                  check_disk: bool = True) -> list[Finding]:
    findings: list[Finding] = []
    for claim in claims:
        if claim.type in _COMMAND_EVIDENCE:
            findings.append(
                _verify_command_claim(session, claim, _COMMAND_EVIDENCE[claim.type]))
        elif claim.type is ClaimType.FILE_CREATED:
            findings.append(_verify_file_created(session, claim, check_disk))
        elif claim.type is ClaimType.TASK_DONE:
            findings.append(_verify_task_done(session, claim))
    return findings
