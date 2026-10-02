"""Verify claims against ground truth.

Ground truth, in order of strength:
1. Tool-call results inside the transcripts: exit codes and output of
   the commands the agent, or an agent it delegated to, actually ran.
2. The filesystem and git state of the project right now.

Each claim gets one of four verdicts:
- VERIFIED:     a relevant check ran before the claim, succeeded, and no
                code was edited between the check and the claim.
- STALE:        the check succeeded, but code was edited afterwards and
                success was claimed without re-running it.
- UNVERIFIED:   nothing that could back the claim ran before it.
- CONTRADICTED: the most recent relevant check before the claim failed,
                or the claimed artifact does not exist.

Ordering across transcripts. Main-session events are ordered by their
position in the main transcript. A run in a delegated transcript backs
a claim only when its result came back before the claim (by timestamp)
and its agent had finished before the claim, so a check the main agent
could not yet know about never backs what it said. An edit is judged by
its own time only: an edit made before the claim by an agent that was
still working counts toward STALE, because the code had changed.

Background runs count from their completion notice, not from their
launch. A run whose exit code a pager or filter hid backs a claim only
when its output shows a pass marker; otherwise the claim is UNVERIFIED.

Relayed claims. A claim made in the same turn that a delegated result
came back in (or one that says "the agent reports...") passes on that
agent's report. Only the delivered agents, or every agent of a delivered
workflow, plus the main session, can back it; with no command result
there it is UNVERIFIED with its own reason.
"""

from __future__ import annotations

import bisect
import re
import subprocess
from pathlib import Path

from .evidence import CHECK_TYPES, masked_exit, stage_types
from .models import (
    Claim, ClaimType, Event, EventKind, Finding, Session, Source, Verdict,
)
from .redact import redact_secrets
from .shell import norm_path, run_directory

RELAYED_REASON = "relayed from an agent's report, no command result seen"
MASKED_REASON = "exit code hidden by a pipe and output does not show the result"

# Wording that passes on another agent's report.
_RELAY_WORDING = re.compile(
    r"\b(?:sub-?agents?|agents?|workflows?|workers?|verifiers?|reviewers?|auditors?)"
    r"\s+(?:says?|said|reports?|reported|claims?|claimed|confirms?|confirmed|returned)\b"
    r"|\baccording\s+to\s+the\s+(?:sub-?agent|agent|workflow|worker)",
    re.IGNORECASE,
)


def _command_failed(event: Event) -> bool:
    if event.is_error or (event.exit_code or 0) != 0:
        return True
    return event.failure_hint


def _short(command: str, limit: int = 80) -> str:
    command = " ".join(redact_secrets(command).split())
    return command if len(command) <= limit else command[: limit - 1] + "…"


def _where(event: Event) -> str:
    return "main" if event.source is None else event.source.describe()


def _how_masked(event: Event) -> str | None:
    return masked_exit(event.command) if event.command else None


def _inconclusive(event: Event) -> str | None:
    """The reason, when a pipe or a later command hid the exit code and the
    output shows no pass marker; else None."""
    how = _how_masked(event)
    if how is None or event.pass_hint:
        return None
    if how.startswith("| "):
        return MASKED_REASON
    return f"exit code hidden by `{how}` and output does not show the result"


def _masked_note(event: Event) -> str:
    """Note how the run's result was read when it was not a plain exit code."""
    how = _how_masked(event)
    if how is not None:
        where = "a pipe" if how.startswith("| ") else f"`{how}`"
        return f", exit code hidden by {where}, output shows a pass"
    if event.background:
        return ", ran in the background, from its completion notice"
    return ""


class Timeline:
    """Every evidence-bearing event of a session, in one time order."""

    def __init__(self, session: Session):
        self.session = session
        self._runs: dict[ClaimType, tuple[list[tuple], list[Event]]] = {}
        entries: dict[ClaimType, list[tuple[tuple, Event]]] = {}
        edits: list[tuple[tuple, Event]] = []
        for key, event in self._all_tool_calls():
            for claim_type in event.check_types:
                entries.setdefault(claim_type, []).append((key, event))
            if event.is_file_edit():
                edits.append((key, event))
        for claim_type, items in entries.items():
            items.sort(key=lambda pair: pair[0])
            self._runs[claim_type] = ([k for k, _ in items], [e for _, e in items])
        edits.sort(key=lambda pair: pair[0])
        self._edit_keys = [k for k, _ in edits]
        self._edits = [e for _, e in edits]
        # (index, line) of each prompt; older callers may set only prompt_starts.
        self._prompts = sorted(session.prompt_marks or
                               [(i, -1) for i in session.prompt_starts])

    def _all_tool_calls(self):
        for event in self.session.events:
            if event.kind is EventKind.TOOL_CALL:
                order = event.index if event.done_order is None else event.done_order
                yield (event.t, 0, order), event
        for n, source in enumerate(self.session.sources):
            for seq, event in enumerate(source.events):
                yield (event.t, 1, n, seq), event

    # -- the claim's view of the timeline ---------------------------------

    def claim_key(self, claim: Claim) -> tuple:
        event = self.session.events[claim.event_index]
        return (event.t, 0, claim.event_index)

    def relay_keys(self, claim: Claim) -> set[tuple[str, str]]:
        """Agents and runs whose results came back in the claim's turn.

        A delivery and the next prompt can share an event index (nothing
        happened between them), so they are ordered by line number: a
        delivery recorded before the prompt belongs to the previous turn.
        """
        pos = bisect.bisect_right(self._prompts, (claim.event_index, float("inf")))
        start = self._prompts[pos - 1] if pos else (-1, -1)
        return {d.key for d in self.session.deliveries
                if start < (d.index, d.seq) and d.index <= claim.event_index}

    def eligible(self, event: Event, claim_key: tuple,
                 restrict: set[tuple[str, str]] | None) -> bool:
        source: Source | None = event.source
        if source is None:
            return True
        if source.finished is None or source.finished > claim_key[0]:
            return False
        return restrict is None or source.matches(restrict)

    def runs_before(self, claim_type: ClaimType, claim_key: tuple,
                    restrict, after: tuple | None = None) -> list[Event]:
        """Eligible runs of a type with after < key < claim, newest first."""
        keys, events = self._runs.get(claim_type, ([], []))
        hi = bisect.bisect_left(keys, claim_key)
        lo = bisect.bisect_right(keys, after) if after is not None else 0
        return [events[i] for i in range(hi - 1, lo - 1, -1)
                if self.eligible(events[i], claim_key, restrict)]

    def last_run(self, claim_type, claim_key, restrict) -> tuple[tuple, Event] | None:
        keys, events = self._runs.get(claim_type, ([], []))
        for i in range(bisect.bisect_left(keys, claim_key) - 1, -1, -1):
            if self.eligible(events[i], claim_key, restrict):
                return keys[i], events[i]
        return None

    def edits_between(self, start: tuple, claim_key: tuple, restrict,
                      within: str | None = None) -> list[Event]:
        """Eligible edits after `start` and before the claim.

        With `within` (a folder from shell.norm_path), only edits to files
        inside it count; an edit whose path cannot be read counts.
        """
        lo = bisect.bisect_right(self._edit_keys, start)
        hi = bisect.bisect_left(self._edit_keys, claim_key)
        return [self._edits[i] for i in range(lo, hi)
                if _inside(self._edits[i], within)]

    def last_edit(self, claim_key, restrict) -> tuple[tuple, Event] | None:
        """The newest edit before the claim, by anyone: whether its agent had
        finished, or was the one the claim relays, does not matter for
        whether the code changed."""
        hi = bisect.bisect_left(self._edit_keys, claim_key)
        return (self._edit_keys[hi - 1], self._edits[hi - 1]) if hi else None

    def writes_before(self, claim_key, restrict) -> list[Event]:
        hi = bisect.bisect_left(self._edit_keys, claim_key)
        return [self._edits[i] for i in range(hi)
                if self.eligible(self._edits[i], claim_key, restrict)]


def _inside(edit: Event, folder: str | None) -> bool:
    if folder is None:
        return True
    path = norm_path(edit.file_path, norm_path(edit.cwd) if edit.cwd else None)
    return path is None or path == folder or path.startswith(folder.rstrip("/") + "/")


class _Context:
    """What one claim may be checked against."""

    def __init__(self, timeline: Timeline, claim: Claim):
        self.key = timeline.claim_key(claim)
        delivered = timeline.relay_keys(claim)
        self.restrict = delivered or None
        self.relayed = bool(delivered) or bool(_RELAY_WORDING.search(claim.quote))


def _unverified(claim: Claim, ctx: _Context, reason: str) -> Finding:
    return Finding(claim=claim, verdict=Verdict.UNVERIFIED,
                   evidence=RELAYED_REASON if ctx.relayed else reason,
                   relayed=ctx.relayed)


def _found(claim: Claim, ctx: _Context, verdict: Verdict, evidence: str,
           event: Event) -> Finding:
    where = _where(event)
    return Finding(
        claim=claim, verdict=verdict, evidence=f"{evidence} [in {where}]",
        evidence_index=event.index if event.source is None else None,
        evidence_source=where, relayed=ctx.relayed,
    )


def _verify_command_claim(timeline: Timeline, claim: Claim, ctx: _Context) -> Finding:
    last = timeline.last_run(claim.type, ctx.key, ctx.restrict)
    if last is None:
        return _unverified(claim, ctx,
                           "no command that could back this claim was run before it")
    run_key, run = last
    if _command_failed(run):
        if run.is_error or (run.exit_code or 0) != 0:
            reason = f"exit {run.exit_code if run.exit_code is not None else '?'}"
        else:
            reason = "output reports failures despite exit 0"
        return _found(claim, ctx, Verdict.CONTRADICTED,
                      f"most recent relevant run failed ({reason}): "
                      f"`{_short(run.command)}`", run)
    reason = _inconclusive(run)
    if reason:
        return _found(claim, ctx, Verdict.UNVERIFIED,
                      f"{reason}: `{_short(run.command)}`", run)
    if claim.type in CHECK_TYPES:
        # Only edits inside the folder the check ran in make it stale: a
        # parallel agent working on another repository does not.
        folder = run_directory(run.command, run.cwd,
                               lambda stage: claim.type in stage_types(stage))
        edits_after = timeline.edits_between(run_key, ctx.key, ctx.restrict, folder)
        if edits_after:
            files = {Path(e.file_path).name for e in edits_after if e.file_path}
            listed = ", ".join(sorted(files)[:3]) or f"{len(edits_after)} files"
            return _found(claim, ctx, Verdict.STALE,
                          f"`{_short(run.command)}` passed, but {listed} "
                          f"edited afterwards with no re-run before the claim", run)
    return _found(claim, ctx, Verdict.VERIFIED,
                  f"`{_short(run.command)}` succeeded (exit "
                  f"{run.exit_code if run.exit_code is not None else 0}"
                  f"{_masked_note(run)})", run)


def _is_remote_path(path: str | Path) -> bool:
    """UNC, device or NT-namespace path (\\\\host\\share, //host/share, \\??\\...).

    Paths come from the transcript, so they are untrusted. On Windows,
    stat-ing \\\\host\\share opens an SMB connection to that host and sends
    the user's NTLM credentials, so these are never touched.
    """
    text = str(path).replace("/", "\\")
    return text.startswith("\\\\") or text.startswith("\\??\\")


def _local_candidates(session: Session, claim: Claim,
                      writes: list[Event]) -> tuple[list[Path], bool]:
    """Paths safe to check on disk, and whether any were skipped as remote."""
    candidates: list[Path] = []
    skipped = False
    if _is_remote_path(session.cwd) or _is_remote_path(claim.detail):
        skipped = True
    else:
        joined = Path(session.cwd) / claim.detail
        if _is_remote_path(joined):
            skipped = True
        else:
            candidates.append(joined)
    for w in writes:
        if not w.file_path:
            continue
        if _is_remote_path(w.file_path) or _is_remote_path(Path(w.file_path)):
            skipped = True
        else:
            candidates.append(Path(w.file_path))
    return candidates, skipped


def _verify_file_created(timeline: Timeline, claim: Claim, ctx: _Context,
                         check_disk: bool) -> Finding:
    session = timeline.session
    name = Path(claim.detail).name
    writes = [
        e for e in timeline.writes_before(ctx.key, ctx.restrict)
        if e.tool_name in ("Write", "Edit", "NotebookEdit")
        and Path(e.file_path).name == name
    ]
    if not writes:
        return _unverified(claim, ctx,
                           f"no Write/Edit call for `{name}` appears before the claim")
    last = writes[-1]
    if check_disk and session.cwd:
        candidates, skipped = _local_candidates(session, claim, writes)
        on_disk = any(p.exists() for p in candidates)
        if not on_disk and skipped:
            return _found(claim, ctx, Verdict.VERIFIED,
                          f"{last.tool_name} call for `{name}` found; "
                          f"disk not checked (network or device path)", last)
        if not on_disk:
            return _found(claim, ctx, Verdict.CONTRADICTED,
                          f"`{claim.detail}` was written in-session but is not on disk now",
                          last)
    return _found(claim, ctx, Verdict.VERIFIED,
                  f"{last.tool_name} call for `{name}` found"
                  + (" and file exists on disk" if check_disk else ""), last)


def _verify_task_done(timeline: Timeline, claim: Claim, ctx: _Context) -> Finding:
    """'Done/fixed/working' is only as good as the checks run after the last edit."""
    last_edit = timeline.last_edit(ctx.key, ctx.restrict)
    if last_edit is None:
        return _unverified(claim, ctx,
                           "no file edits precede this claim; nothing to check it against")
    edit_key = last_edit[0]
    for claim_type in (ClaimType.TESTS_PASS, ClaimType.BUILD_OK, ClaimType.TYPECHECK_OK):
        runs = timeline.runs_before(claim_type, ctx.key, ctx.restrict, after=edit_key)
        if runs:
            run = runs[-1]  # the first check after the final edit
            if _command_failed(run):
                return _found(claim, ctx, Verdict.CONTRADICTED,
                              f"check after final edit failed: `{_short(run.command)}`", run)
            reason = _inconclusive(run)
            if reason:
                return _found(claim, ctx, Verdict.UNVERIFIED,
                              f"{reason}: `{_short(run.command)}`", run)
            return _found(claim, ctx, Verdict.VERIFIED,
                          f"verified after final edit by `{_short(run.command)}`", run)
    return _unverified(claim, ctx,
                       "no test/build/typecheck ran between the final edit and this claim")


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
    timeline = Timeline(session)
    findings: list[Finding] = []
    for claim in claims:
        ctx = _Context(timeline, claim)
        if claim.type is ClaimType.FILE_CREATED:
            findings.append(_verify_file_created(timeline, claim, ctx, check_disk))
        elif claim.type is ClaimType.TASK_DONE:
            findings.append(_verify_task_done(timeline, claim, ctx))
        else:
            findings.append(_verify_command_claim(timeline, claim, ctx))
    return findings
