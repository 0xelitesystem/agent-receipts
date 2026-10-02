"""Parse Claude Code session transcripts (JSONL) into a Session.

Format notes (observed against Claude Code 2.x transcripts):
- Each line is a JSON object with a top-level "type".
- "assistant" records carry message.content: a list of blocks, where
  type=="text" is prose and type=="tool_use" is a tool invocation
  (id / name / input).
- "user" records carry tool results: message.content blocks with
  type=="tool_result" reference the tool_use id and include is_error.
  A sibling top-level "toolUseResult" holds richer data (stdout/stderr
  dict on success, or an "Error: Exit code N" string on failure).
- Other record types (system, attachment, file-history-snapshot, ...)
  are not needed for auditing and are skipped.
- Delegation: an Agent/Task tool result carries toolUseResult.agentId, a
  Workflow tool result carries toolUseResult.runId (wf_...). Either may
  be "async_launched", in which case the result arrives later as a user
  message that starts with <task-notification> and names the launching
  <tool-use-id>. Those points are recorded as Deliveries.
- Background shell commands: a Bash call with run_in_background (or one
  moved to the background after its timeout) first returns only "Command
  running in background with ID: ..." and toolUseResult.backgroundTaskId.
  That is not a result. The command counts as evidence only once its
  <task-notification> arrives (as a user message, an attachment or a
  queue-operation record), with the exit code and time from that notice.
"""

from __future__ import annotations

import calendar
import json
import os
import re
from pathlib import Path

from .evidence import FAILURE_IN_OUTPUT, PASS_IN_OUTPUT, check_types
from .models import Delivery, Event, EventKind, Session

_EXIT_CODE_RE = re.compile(r"[Ee]xit code:? (\d+)")
_TS_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_DELEGATING_TOOLS = ("Agent", "Task", "Workflow")
_RUN_ID_RE = re.compile(r"\b(wf_[0-9A-Za-z][0-9A-Za-z-]{2,63})")
_AGENT_ID_RE = re.compile(r"agentId:\s*([0-9A-Za-z_-]{4,64})")
_TAG_RE = {tag: re.compile(rf"<{tag}>([^<]{{1,200}})</{tag}>")
           for tag in ("task-id", "tool-use-id", "status")}
_DONE_STATUSES = ("completed", "failed", "killed", "error", "stopped", "cancelled")
_SHELL_TOOLS = ("Bash", "PowerShell")
_BACKGROUND_TEXT = re.compile(
    r"^\s*Command running in background|moved to the background", re.IGNORECASE)
_BACKGROUND_ID = re.compile(r"\bID:?\s*([0-9A-Za-z_-]{3,64})")
_NOTICE_EXIT = re.compile(r"exit code (\d+)", re.IGNORECASE)


def background_id(tool_use_result, text: str) -> str | None:
    """The task id when a shell result only says the command went to the background."""
    if isinstance(tool_use_result, dict):
        bid = tool_use_result.get("backgroundTaskId")
        if isinstance(bid, str) and bid:
            return bid
    if _BACKGROUND_TEXT.search(text[:300]):
        match = _BACKGROUND_ID.search(text[:300])
        return match.group(1) if match else "?"
    return None


def notice_text(record: dict) -> str | None:
    """The <task-notification> a record carries, in any shape it is written in."""
    rtype = record.get("type")
    if rtype == "user":
        message = record.get("message")
        return _notification_text(message) if isinstance(message, dict) else None
    if rtype == "attachment":
        attachment = record.get("attachment")
        text = attachment.get("prompt") if isinstance(attachment, dict) else None
    elif rtype == "queue-operation":
        text = record.get("content")
    else:
        return None
    if isinstance(text, str) and text.lstrip().startswith("<task-notification>"):
        return text
    return None


def background_done(text: str) -> tuple[list[str], int] | None:
    """(ids, exit code) from a background command's completion notice.

    ids are the notice's tool-use id and task id. None when the notice
    does not report a finished run (killed, stopped, still running).
    """
    head = text[:4000]
    found = {}
    for tag, rx in _TAG_RE.items():
        match = rx.search(head)
        found[tag] = match.group(1).strip() if match else ""
    status = found["status"].lower()
    if status not in ("completed", "failed"):
        return None
    match = _NOTICE_EXIT.search(head)
    code = int(match.group(1)) if match else (0 if status == "completed" else 1)
    return [i for i in (found["tool-use-id"], found["task-id"]) if i], code


def complete_background(event: Event, code: int, when: float, timestamp: str) -> None:
    """Turn a background launch into a finished run, from its completion notice."""
    event.exit_code = code
    event.is_error = code != 0
    event.failure_hint = False
    event.pass_hint = False  # the output is in a file the transcript does not hold
    event.t = when
    event.timestamp = timestamp


def epoch(timestamp: str) -> float | None:
    """ISO 8601 timestamp to seconds since the epoch (UTC), or None."""
    match = _TS_RE.match(timestamp.strip()) if timestamp else None
    if not match:
        return None
    y, mo, d, h, mi, sec, frac, zone = match.groups()
    try:
        value = float(calendar.timegm(
            (int(y), int(mo), int(d), int(h), int(mi), int(sec))))
    except (ValueError, OverflowError):
        return None
    if frac:
        value += int(frac) / 10 ** len(frac)
    if zone and zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        digits = zone[1:].replace(":", "")
        value -= sign * (int(digits[:2]) * 3600 + int(digits[2:]) * 60)
    return value


def exit_code_of(event: Event) -> None:
    """Set exit_code from the output text, as Claude Code writes it."""
    match = _EXIT_CODE_RE.search(event.output[:500])
    if match:
        event.exit_code = int(match.group(1))
    elif not event.is_error:
        event.exit_code = 0


def _blocks(message: dict) -> list[dict]:
    content = message.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _result_text(block: dict, tool_use_result) -> str:
    """Flatten a tool_result's content (string or block list) to text."""
    parts: list[str] = []
    content = block.get("content")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
    if isinstance(tool_use_result, dict):
        for key in ("stdout", "stderr"):
            val = tool_use_result.get(key)
            if isinstance(val, str) and val and val not in parts:
                parts.append(val)
    elif isinstance(tool_use_result, str) and tool_use_result not in parts:
        parts.append(tool_use_result)
    return "\n".join(p for p in parts if p)


def parse_transcript(path: str | Path) -> Session:
    path = Path(path)
    session = Session(path=str(path))
    pending: dict[str, Event] = {}  # tool_use id -> Event awaiting its result
    delegated: dict[str, tuple[str, str]] = {}  # launching tool id / task id -> key
    background: dict[str, tuple[Event, frozenset]] = {}  # tool id / task id -> launch
    shell_ids: set[str] = set()  # every background shell id: its notice is no delivery
    index = 0
    line_no = 0
    clock = float("-inf")

    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            line_no += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            rtype = record.get("type")
            timestamp = str(record.get("timestamp", ""))
            if timestamp:
                session.first_timestamp = session.first_timestamp or timestamp
                session.last_timestamp = timestamp
                seconds = epoch(timestamp)
                if seconds is not None and seconds > clock:
                    clock = seconds

            if background and rtype in ("user", "attachment", "queue-operation"):
                notice = notice_text(record)
                done = background_done(notice) if notice else None
                if done is not None:
                    ids, code = done
                    launch = next((background[i] for i in ids if i in background), None)
                    if launch is not None:
                        event, types = launch
                        for key in [k for k, v in background.items() if v is launch]:
                            del background[key]
                        complete_background(event, code, clock, timestamp)
                        event.done_order = index - 0.5
                        event.check_types = types

            if rtype == "assistant":
                if not session.session_id:
                    session.session_id = str(record.get("sessionId", ""))
                    session.cwd = str(record.get("cwd", ""))
                    session.git_branch = str(record.get("gitBranch", "") or "")
                    session.slug = str(record.get("slug", "") or "")
                    session.version = str(record.get("version", ""))
                sidechain = bool(record.get("isSidechain"))
                for block in _blocks(record.get("message", {})):
                    btype = block.get("type")
                    if btype == "text":
                        text = str(block.get("text", "")).strip()
                        if not text:
                            continue
                        session.events.append(Event(
                            kind=EventKind.TEXT,
                            index=index,
                            timestamp=timestamp,
                            is_sidechain=sidechain,
                            text=text,
                            t=clock,
                        ))
                        index += 1
                    elif btype == "tool_use":
                        tool_input = block.get("input")
                        event = Event(
                            kind=EventKind.TOOL_CALL,
                            index=index,
                            timestamp=timestamp,
                            is_sidechain=sidechain,
                            tool_name=str(block.get("name", "")),
                            tool_id=str(block.get("id", "")),
                            tool_input=tool_input if isinstance(tool_input, dict) else {},
                            t=clock,
                            cwd=str(record.get("cwd", "") or ""),
                        )
                        event.check_types = check_types(event.command)
                        session.events.append(event)
                        pending[event.tool_id] = event
                        index += 1

            elif rtype == "user":
                message = record.get("message", {})
                if not isinstance(message, dict):
                    continue
                tool_use_result = record.get("toolUseResult")
                notice = _notification_text(message)
                if notice is not None:
                    if not _notice_ids(notice) & shell_ids:
                        _note_notification(session, notice, delegated, index, line_no)
                    continue
                if _is_human_prompt(record, message):
                    session.prompt_starts.append(index)
                    session.prompt_marks.append((index, line_no))
                    continue
                for block in _blocks(message):
                    if block.get("type") != "tool_result":
                        continue
                    event = pending.pop(str(block.get("tool_use_id", "")), None)
                    if event is None:
                        continue
                    event.is_error = bool(block.get("is_error"))
                    event.output = _result_text(block, tool_use_result)
                    exit_code_of(event)
                    event.failure_hint = bool(FAILURE_IN_OUTPUT.search(event.output))
                    event.pass_hint = bool(PASS_IN_OUTPUT.search(event.output))
                    if event.tool_name in _SHELL_TOOLS:
                        bid = background_id(tool_use_result, event.output)
                        if bid is not None:
                            # Not a result yet: no evidence until its notice.
                            event.background = True
                            event.exit_code = None
                            launch = (event, event.check_types)
                            event.check_types = frozenset()
                            background[event.tool_id] = launch
                            shell_ids.add(event.tool_id)
                            if bid != "?":
                                background[bid] = launch
                                shell_ids.add(bid)
                    if event.tool_name in _DELEGATING_TOOLS:
                        _note_launch(session, event, tool_use_result, delegated,
                                     index, line_no)

    return session


def _notification_text(message: dict) -> str | None:
    """The text of a <task-notification> user message, else None."""
    content = message.get("content")
    if isinstance(content, list):
        texts = [b.get("text") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        content = texts[0] if len(texts) == 1 else None
    if isinstance(content, str) and content.lstrip().startswith("<task-notification>"):
        return content
    return None


def _is_human_prompt(record: dict, message: dict) -> bool:
    """A user turn typed by a person (not a tool result or a harness note)."""
    if record.get("isMeta") or record.get("isCompactSummary") or record.get("isSidechain"):
        return False
    content = message.get("content")
    if isinstance(content, str):
        return bool(content.strip()) and not content.lstrip().startswith("<")
    if isinstance(content, list):
        blocks = [b for b in content if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return False
        return any(b.get("type") == "text"
                   and not str(b.get("text", "")).lstrip().startswith("<")
                   for b in blocks)
    return False


def _note_launch(session: Session, event: Event, tool_use_result,
                 delegated: dict[str, tuple[str, str]], index: int,
                 line_no: int = 0) -> None:
    """Map a delegating tool call to its agent or run; record a delivery if done."""
    info = tool_use_result if isinstance(tool_use_result, dict) else {}
    key: tuple[str, str] | None = None
    if event.tool_name == "Workflow":
        run_id = info.get("runId")
        if not isinstance(run_id, str) or not run_id:
            match = _RUN_ID_RE.search(event.output[:4000])
            run_id = match.group(1) if match else ""
        if run_id:
            key = ("run", run_id)
    else:
        agent_id = info.get("agentId")
        if not isinstance(agent_id, str) or not agent_id:
            match = _AGENT_ID_RE.search(event.output[-2000:])
            agent_id = match.group(1) if match else ""
        if agent_id:
            key = ("agent", agent_id)
    if key is None:
        return
    delegated[event.tool_id] = key
    task_id = info.get("taskId")
    if isinstance(task_id, str) and task_id:
        delegated[task_id] = key
    if key[0] == "agent":
        delegated.setdefault(key[1], key)
    launched = (str(info.get("status", "")).startswith("async")
                or info.get("isAsync") is True
                or "launched in background" in event.output[:200])
    if not launched and not event.is_error:
        session.deliveries.append(Delivery(index=index, key=key, seq=line_no))


def _notice_ids(text: str) -> set[str]:
    head = text[:4000]
    found = set()
    for tag in ("task-id", "tool-use-id"):
        match = _TAG_RE[tag].search(head)
        if match:
            found.add(match.group(1).strip())
    return found


def _note_notification(session: Session, text: str,
                       delegated: dict[str, tuple[str, str]], index: int,
                       line_no: int = 0) -> None:
    head = text[:4000]
    found = {}
    for tag, rx in _TAG_RE.items():
        match = rx.search(head)
        found[tag] = match.group(1).strip() if match else ""
    if found["status"].lower() not in _DONE_STATUSES:
        return
    key = delegated.get(found["tool-use-id"]) or delegated.get(found["task-id"])
    if key is None and found["task-id"]:
        key = ("agent", found["task-id"])
    if key is not None:
        session.deliveries.append(Delivery(index=index, key=key, seq=line_no))


def projects_dir() -> Path:
    return Path(os.path.expanduser("~")) / ".claude" / "projects"


def discover_transcripts(project_filter: str | None = None) -> list[Path]:
    """All transcript files under ~/.claude/projects, newest first."""
    root = projects_dir()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for project in sorted(root.iterdir()):
        if not project.is_dir():
            continue
        if project_filter and project_filter.lower() not in project.name.lower():
            continue
        found.extend(project.glob("*.jsonl"))
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def resolve_target(target: str, project_filter: str | None = None) -> Path:
    """Turn a CLI target (path, session id, or 'latest') into a file path."""
    direct = Path(target)
    if direct.is_file():
        return direct
    transcripts = discover_transcripts(project_filter)
    if target == "latest":
        if not transcripts:
            raise FileNotFoundError("no transcripts found under ~/.claude/projects")
        return transcripts[0]
    for candidate in transcripts:
        if candidate.stem.startswith(target):
            return candidate
    raise FileNotFoundError(f"no transcript matching {target!r}")
