"""Find and read the transcripts a session delegated work to.

Claude Code keeps a session's sub-agent and workflow transcripts next
to the main one, in a folder named after the session:

    <project>/<session>.jsonl                                   main
    <project>/<session>/subagents/agent-<id>.jsonl              sub-agent
    <project>/<session>/subagents/agent-<id>.meta.json          its description
    <project>/<session>/subagents/workflows/<run>/agent-<id>.jsonl   workflow agent
    <project>/<session>/subagents/workflows/<run>/journal.jsonl      run journal

Only those folders are listed (never a recursive walk), symlinked folders
are not followed, and compressed (.zst) transcripts are skipped. A
`.jsonl.superseded-<n>` or `.orphaned-` copy keeps the class of the
file it copies.

Each transcript is streamed in binary, line by line, and only lines that
can matter are decoded: an assistant line with a tool_use block, or a
user line that answers a tool call we are waiting on. Everything kept is
small: the command, the target path, the exit status and whether the
output reported failures. Tool output itself is dropped as soon as it
has been read. Gaming signals are found while streaming, because they
need the edit content that is not kept.

A shell command sent to the background is held back until its
<task-notification> is seen in the same transcript; one that never
completes there is no evidence. When the agent finished is the latest
timestamp on any of its lines (read with a byte search, not a decode);
a transcript whose last line is cut off is still being written, so its
agent has not finished.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .evidence import FAILURE_IN_OUTPUT, PASS_IN_OUTPUT, check_types
from .gaming import signals_for_event
from .models import Event, EventKind, Session, Source
from .parser import (
    background_done, background_id, complete_background, epoch, exit_code_of,
    notice_text,
)
from .redact import redact_secrets

_AGENT_FILE = re.compile(
    r"^(?:\.orphaned-)?agent-([0-9A-Za-z_-]{1,128})\.jsonl(?:\.superseded-[^/\\]+)?$")
_RUN_DIR = re.compile(r"^wf_[0-9A-Za-z_-]{1,128}$")
_EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_SHELL_TOOLS = ("Bash", "PowerShell")
_RESULT_ID = re.compile(rb'"tool_use_id"\s*:\s*"([^"\\]{1,256})"')
_TOOL_USE = b'"tool_use"'
_NOTICE = b"task-notification"
_TIMESTAMP_KEY = b'"timestamp"'
_TIMESTAMP_VALUE = re.compile(rb'\s*:\s*"([0-9T:.+Z -]{10,48})"')
_META_LIMIT = 64 * 1024
_LABEL_LIMIT = 60
_LABEL_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f؜‎‏‪-‮⁦-⁩]")


def session_dir(main_path: str | Path) -> Path:
    """The folder that holds a main transcript's delegated transcripts."""
    path = Path(main_path)
    return path.with_name(path.name[: -len(".jsonl")] if path.name.endswith(".jsonl")
                          else path.stem)


def _dirs(path: Path) -> list[os.DirEntry]:
    try:
        with os.scandir(path) as it:
            return sorted((e for e in it if e.is_dir(follow_symlinks=False)),
                          key=lambda e: e.name)
    except OSError:
        return []


def _agent_files(path: Path) -> list[tuple[str, os.DirEntry]]:
    found = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                match = _AGENT_FILE.match(entry.name)
                if match and entry.is_file(follow_symlinks=False):
                    found.append((match.group(1), entry))
    except OSError:
        return []
    return sorted(found, key=lambda pair: pair[1].name)


def discover_sources(main_path: str | Path) -> list[Source]:
    """Every sub-agent and workflow-agent transcript of a session."""
    root = session_dir(main_path) / "subagents"
    sources: list[Source] = []
    for agent_id, entry in _agent_files(root):
        sources.append(Source(kind="subagent", path=entry.path, agent_id=agent_id,
                              size=entry.stat(follow_symlinks=False).st_size))
    for run in _dirs(root / "workflows"):
        if not _RUN_DIR.match(run.name):
            continue
        for agent_id, entry in _agent_files(Path(run.path)):
            sources.append(Source(kind="workflow_agent", path=entry.path,
                                  agent_id=agent_id, run_id=run.name,
                                  size=entry.stat(follow_symlinks=False).st_size))
    return sources


def _label(source: Source) -> str:
    """The agent's description from its .meta.json, redacted and trimmed."""
    name = Path(source.path).name
    stem = name[: name.index(".jsonl")]
    meta = Path(source.path).with_name(stem + ".meta.json")
    try:
        if meta.stat().st_size > _META_LIMIT:
            return ""
        with open(meta, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    text = data.get("description") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return ""
    text = _LABEL_UNSAFE.sub(" ", redact_secrets(" ".join(text.split())))
    return text if len(text) <= _LABEL_LIMIT else text[: _LABEL_LIMIT - 1] + "…"


def _result_text(block: dict, tool_use_result) -> str:
    parts: list[str] = []
    content = block.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        parts.extend(str(i.get("text", "")) for i in content
                     if isinstance(i, dict) and i.get("type") == "text")
    if isinstance(tool_use_result, dict):
        parts.extend(v for v in (tool_use_result.get("stdout"),
                                 tool_use_result.get("stderr"))
                     if isinstance(v, str) and v and v not in parts)
    return "\n".join(p for p in parts if p)


def _line_time(line: bytes) -> bytes | None:
    """The raw value of the last "timestamp" key on a line, without decoding it."""
    at = line.rfind(_TIMESTAMP_KEY)
    if at < 0:
        return None
    match = _TIMESTAMP_VALUE.match(line, at + len(_TIMESTAMP_KEY))
    return match.group(1) if match else None


def _complete(line: bytes, background: dict, source: Source) -> None:
    """Pair a completion notice with the background command it reports on."""
    try:
        record = json.loads(line)
    except ValueError:
        return
    if not isinstance(record, dict):
        return
    notice = notice_text(record)
    done = background_done(notice) if notice else None
    if done is None:
        return
    ids, code = done
    launch = next((background[i] for i in ids if i in background), None)
    if launch is None:
        return
    for key in [k for k, v in background.items() if v is launch]:
        del background[key]
    timestamp = str(record.get("timestamp", ""))
    seconds = epoch(timestamp)
    if seconds is None:
        return
    complete_background(launch, code, seconds, timestamp)
    source.events.append(launch)


def read_source(source: Source) -> Source:
    """Stream one delegated transcript into source.events and source.gaming."""
    pending: dict[str, Event] = {}
    background: dict[str, Event] = {}  # tool id / task id -> launch awaiting its notice
    where = None
    last_line = b""
    latest: float | None = None
    seen_raw: set[bytes] = set()
    try:
        fh = open(source.path, "rb")
    except OSError:
        return source
    with fh:
        for line in fh:
            if len(line) < 3:
                continue
            last_line = line
            raw = _line_time(line)
            if raw is not None and raw not in seen_raw:
                if len(seen_raw) > 4096:
                    seen_raw.clear()
                seen_raw.add(raw)
                seconds = epoch(raw.decode("ascii", "replace"))
                if seconds is not None and (latest is None or seconds > latest):
                    latest = seconds
            if _TOOL_USE not in line:
                if background and _NOTICE in line:
                    _complete(line, background, source)
                    continue
                if not pending or b'"tool_use_id"' not in line:
                    continue
                ids = _RESULT_ID.findall(line)
                if not any(i.decode("utf-8", "replace") in pending for i in ids):
                    continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            seconds = epoch(str(record.get("timestamp", "")))
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            rtype = record.get("type")
            if rtype == "assistant":
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                        continue
                    name = str(block.get("name", ""))
                    if name not in _SHELL_TOOLS and name not in _EDIT_TOOLS:
                        continue
                    tool_input = block.get("input")
                    event = Event(kind=EventKind.TOOL_CALL, index=-1,
                                  tool_name=name, tool_id=str(block.get("id", "")),
                                  tool_input=tool_input if isinstance(tool_input, dict) else {},
                                  source=source, cwd=str(record.get("cwd", "") or ""))
                    if where is None:
                        where = source.describe()
                    source.gaming.extend(signals_for_event(event, where))
                    command = event.command
                    if command:
                        event.check_types = check_types(command)
                        if not event.check_types:
                            continue
                        event.tool_input = {"command": command}
                    else:
                        if not event.file_path:
                            continue
                        event.tool_input = {"file_path": event.file_path}
                    pending[event.tool_id] = event
            elif rtype == "user":
                tool_use_result = record.get("toolUseResult")
                for block in content:
                    if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                        continue
                    event = pending.pop(str(block.get("tool_use_id", "")), None)
                    if event is None:
                        continue
                    event.is_error = bool(block.get("is_error"))
                    event.output = _result_text(block, tool_use_result)
                    exit_code_of(event)
                    event.failure_hint = bool(FAILURE_IN_OUTPUT.search(event.output))
                    event.pass_hint = bool(PASS_IN_OUTPUT.search(event.output))
                    bid = (background_id(tool_use_result, event.output)
                           if event.tool_name in _SHELL_TOOLS else None)
                    event.output = ""
                    if bid is not None:
                        # Only launched: it counts once its notice is seen.
                        event.background = True
                        event.exit_code = None
                        background[event.tool_id] = event
                        if bid != "?":
                            background[bid] = event
                        continue
                    event.timestamp = str(record.get("timestamp", ""))
                    if seconds is not None:
                        event.t = seconds
                        source.events.append(event)
    # The agent finished when it wrote its last record. A last line that
    # does not parse is still being written: the agent has not finished.
    try:
        json.loads(last_line) if last_line else None
    except ValueError:
        source.finished = None
        return source
    source.finished = latest
    return source


def attach_sources(session: Session) -> Session:
    """Discover and read every delegated transcript of `session`."""
    sources = discover_sources(session.path)
    for source in sources:
        source.label = _label(source)
        read_source(source)
        session.scan.bytes += source.size
        if source.kind == "subagent":
            session.scan.subagents += 1
        else:
            session.scan.workflow_agents += 1
    session.sources = sources
    session.main_only = False
    return session
