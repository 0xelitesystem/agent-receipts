"""Detect test-gaming: changes that make checks pass by weakening them.

These are signals, not convictions. A human removing a genuinely bad
assertion looks the same as an agent deleting one to go green. Each
signal carries a severity and points at the exact event so the reviewer
can judge for themselves.

Signals are found in the main session and in every delegated transcript;
a signal from a sub-agent or workflow agent names where it happened.
"""

from __future__ import annotations

import re
from pathlib import Path

from .evidence import masking
from .models import Event, EventKind, GamingSeverity, GamingSignal, Session
from .redact import redact_secrets

_TEST_FILE = re.compile(
    r"(?:^|[\\/_.-])(?:test|spec)s?(?:[\\/_.-]|\.)|__tests__|conftest\.py",
    re.IGNORECASE,
)

_SKIP_MARKERS = re.compile(
    r"@pytest\.mark\.skip|@unittest\.skip|pytest\.skip\(|@pytest\.mark\.xfail"
    r"|\bit\.skip\(|\btest\.skip\(|\bdescribe\.skip\(|\bxit\(|\bxdescribe\(|\bxtest\("
    r"|#\[ignore\]|\bt\.Skip\(|\bmarked?\s+as\s+skip"
)

_ASSERT = re.compile(
    r"\bassert\b|\bexpect\(|\bassert[A-Z]\w*\(|\b(?:should|chai)\.|ASSERT_|EXPECT_"
)

_NO_VERIFY = re.compile(r"\bgit\s+commit\b[^\n|;&]*\s(?:--no-verify|-n)\b")

# A delete command followed, in the same shell segment, by a path that
# contains test/spec and then a source extension. Checked in linear time:
# one regex with adjacent unbounded quantifiers here backtracked cubically
# on a crafted command such as "rm/" * 800.
_SHELL_SEGMENT = re.compile(r"[\n|;&]")
_DELETE_CMD = re.compile(r"\b(?:rm|del|Remove-Item)\b", re.IGNORECASE)
_PATH_RUN = re.compile(r"[\w\\/.-]+")
_TEST_WORD = re.compile(r"test|spec", re.IGNORECASE)
_SOURCE_EXT = re.compile(r"\.(?:py|js|ts|tsx|go|rs|rb|java|cs)", re.IGNORECASE)


def _deletes_test_file(command: str) -> bool:
    for segment in _SHELL_SEGMENT.split(command):
        delete = _DELETE_CMD.search(segment)
        if not delete:
            continue
        for run in _PATH_RUN.finditer(segment, delete.end()):
            word = _TEST_WORD.search(run.group())
            if word and _SOURCE_EXT.search(run.group(), word.end()):
                return True
    return False


def _is_test_path(path: str) -> bool:
    return bool(path) and bool(_TEST_FILE.search(Path(path).as_posix()))


def _edit_pairs(event: Event) -> list[tuple[str, str]]:
    """(old, new) string pairs for an edit-shaped tool call."""
    if event.tool_name == "Edit":
        return [(str(event.tool_input.get("old_string", "")),
                 str(event.tool_input.get("new_string", "")))]
    if event.tool_name == "MultiEdit":
        edits = event.tool_input.get("edits")
        if isinstance(edits, list):
            return [(str(e.get("old_string", "")), str(e.get("new_string", "")))
                    for e in edits if isinstance(e, dict)]
    return []


def _quote(command: str) -> str:
    return " ".join(redact_secrets(command).split())[:80]


def signals_for_event(event: Event, source: str = "main") -> list[GamingSignal]:
    """Gaming signals raised by one tool call."""
    if event.kind is not EventKind.TOOL_CALL:
        return []
    index = event.index if event.source is None else None
    signals: list[GamingSignal] = []

    def add(kind: str, severity: GamingSeverity, description: str) -> None:
        signals.append(GamingSignal(kind=kind, severity=severity,
                                    description=description,
                                    event_index=index, source=source))

    command = event.command
    if command:
        if _NO_VERIFY.search(command):
            add("no_verify_commit", GamingSeverity.HIGH,
                "commit made with --no-verify (hooks bypassed)")
        masks = masking(command)
        swallowed = [m for m in masks if m[0] == "swallowed_failure"]
        if swallowed:
            add("swallowed_failure", GamingSeverity.HIGH,
                f"command masks its own failure: `{_quote(swallowed[0][2])}`")
        piped = [m for m in masks if m[0] == "masked_exit_code"]
        if piped:
            how = piped[0][1]
            lead = (f"check piped into `{how[2:]}`" if how.startswith("| ")
                    else f"check followed by `{how[2:]}`")
            add("masked_exit_code", GamingSeverity.MEDIUM,
                f"{lead}, so its exit code is lost: `{_quote(piped[0][2])}`")
        if _deletes_test_file(command):
            add("deleted_test_file", GamingSeverity.HIGH,
                "shell command deletes a test file")
        return signals

    if not (event.is_file_edit() and _is_test_path(event.file_path)):
        return signals
    name = Path(event.file_path).name

    for old, new in _edit_pairs(event):
        old_asserts = len(_ASSERT.findall(old))
        new_asserts = len(_ASSERT.findall(new))
        if old_asserts > new_asserts:
            add("weakened_assertions", GamingSeverity.MEDIUM,
                f"{name}: assertions reduced {old_asserts} → {new_asserts} in one edit")
        if _SKIP_MARKERS.search(new) and not _SKIP_MARKERS.search(old):
            add("added_skip", GamingSeverity.MEDIUM,
                f"{name}: skip/xfail marker added to a test")

    if event.tool_name == "Write":
        content = str(event.tool_input.get("content", ""))
        if len(content.strip()) == 0:
            add("emptied_test_file", GamingSeverity.HIGH,
                f"{name}: test file overwritten with empty content")
    return signals


def detect_gaming(session: Session) -> list[GamingSignal]:
    """Signals from the main session, then from each delegated transcript."""
    signals: list[GamingSignal] = []
    for event in session.events:
        signals.extend(signals_for_event(event))
    for source in session.sources:
        signals.extend(source.gaming)
    return signals
