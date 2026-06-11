"""Detect test-gaming: changes that make checks pass by weakening them.

These are signals, not convictions — a human removing a genuinely bad
assertion looks the same as an agent deleting one to go green. Each
signal carries a severity and points at the exact event so the reviewer
can judge for themselves.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import Event, EventKind, GamingSeverity, GamingSignal, Session

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

_SWALLOW_FAILURE = re.compile(
    r"\|\|\s*true\b|\|\|\s*exit\s+0\b|;\s*exit\s+0\s*$"
    r"|--passWithNoTests\b|2>\s*/dev/null\s*\|\|"
)

_NO_VERIFY = re.compile(r"\bgit\s+commit\b[^\n|;&]*\s(?:--no-verify|-n)\b")

_DELETE_TEST = re.compile(
    r"\b(?:rm|del|Remove-Item)\b[^\n|;&]*"
    r"[\w\\/.-]*(?:test|spec)s?[\w\\/.-]*\.(?:py|js|ts|tsx|go|rs|rb|java|cs)",
    re.IGNORECASE,
)


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


def detect_gaming(session: Session) -> list[GamingSignal]:
    signals: list[GamingSignal] = []

    for event in session.events:
        if event.kind is not EventKind.TOOL_CALL:
            continue

        command = event.command
        if command:
            if _NO_VERIFY.search(command):
                signals.append(GamingSignal(
                    kind="no_verify_commit", severity=GamingSeverity.HIGH,
                    description="commit made with --no-verify (hooks bypassed)",
                    event_index=event.index,
                ))
            if _SWALLOW_FAILURE.search(command):
                signals.append(GamingSignal(
                    kind="swallowed_failure", severity=GamingSeverity.HIGH,
                    description=f"command masks its own failure: "
                                f"`{' '.join(command.split())[:80]}`",
                    event_index=event.index,
                ))
            if _DELETE_TEST.search(command):
                signals.append(GamingSignal(
                    kind="deleted_test_file", severity=GamingSeverity.HIGH,
                    description="shell command deletes a test file",
                    event_index=event.index,
                ))
            continue

        if not (event.is_file_edit() and _is_test_path(event.file_path)):
            continue
        name = Path(event.file_path).name

        for old, new in _edit_pairs(event):
            old_asserts = len(_ASSERT.findall(old))
            new_asserts = len(_ASSERT.findall(new))
            if old_asserts > new_asserts:
                signals.append(GamingSignal(
                    kind="weakened_assertions", severity=GamingSeverity.MEDIUM,
                    description=f"{name}: assertions reduced "
                                f"{old_asserts} → {new_asserts} in one edit",
                    event_index=event.index,
                ))
            if _SKIP_MARKERS.search(new) and not _SKIP_MARKERS.search(old):
                signals.append(GamingSignal(
                    kind="added_skip", severity=GamingSeverity.MEDIUM,
                    description=f"{name}: skip/xfail marker added to a test",
                    event_index=event.index,
                ))

        if event.tool_name == "Write":
            content = str(event.tool_input.get("content", ""))
            if len(content.strip()) == 0:
                signals.append(GamingSignal(
                    kind="emptied_test_file", severity=GamingSeverity.HIGH,
                    description=f"{name}: test file overwritten with empty content",
                    event_index=event.index,
                ))

    return signals
