"""Data models for agent-receipts.

Everything the auditor reasons about is one of these shapes:
a Session is an ordered list of Events (assistant text or tool calls),
Claims are extracted from text events, and Findings are the verdicts.
A Session can also carry Sources: the transcripts of the sub-agents and
workflow agents it delegated to, which hold only the tool calls that
can count as evidence (checks, git commands, file writes).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class EventKind(enum.Enum):
    TEXT = "text"
    TOOL_CALL = "tool_call"


@dataclass
class Event:
    """One thing that happened in the session, in transcript order."""

    kind: EventKind
    index: int  # position in the session event stream
    timestamp: str = ""
    is_sidechain: bool = False

    # TEXT events
    text: str = ""

    # TOOL_CALL events
    tool_name: str = ""
    tool_id: str = ""
    tool_input: dict = field(default_factory=dict)
    output: str = ""
    is_error: bool = False
    exit_code: int | None = None
    cwd: str = ""  # working directory the tool call started in

    # Ordering across transcripts. `t` is seconds since the epoch: for a
    # main-session event, the record time made non-decreasing; for an
    # event in a delegated transcript, the time its result came back.
    t: float = float("-inf")
    source: "Source | None" = None  # None means the main session
    failure_hint: bool = False  # output reported failures (kept when output is dropped)
    check_types: frozenset = frozenset()  # ClaimTypes this command can back

    @property
    def command(self) -> str:
        """Shell command for Bash/PowerShell calls, else empty."""
        if self.tool_name in ("Bash", "PowerShell"):
            return str(self.tool_input.get("command", ""))
        return ""

    @property
    def file_path(self) -> str:
        """Target path for file-mutating tools, else empty."""
        return str(self.tool_input.get("file_path", ""))

    def is_file_edit(self) -> bool:
        return self.tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit")


@dataclass
class Source:
    """A delegated transcript: a sub-agent, or one agent of a workflow run."""

    kind: str  # "subagent" or "workflow_agent"
    path: str
    agent_id: str = ""
    run_id: str = ""
    label: str = ""  # the agent's description, redacted and trimmed
    finished: float | None = None  # time of the transcript's last record
    events: list[Event] = field(default_factory=list)
    gaming: list["GamingSignal"] = field(default_factory=list)
    size: int = 0

    def describe(self) -> str:
        where = (f"workflow {self.run_id}, agent {self.agent_id}"
                 if self.kind == "workflow_agent" else f"sub-agent {self.agent_id}")
        return where + (f" ({self.label})" if self.label else "")

    def matches(self, keys: set[tuple[str, str]]) -> bool:
        return (("agent", self.agent_id) in keys
                or (bool(self.run_id) and ("run", self.run_id) in keys))


@dataclass
class Delivery:
    """A delegated result reaching the main session (agent or workflow done)."""

    index: int  # number of main events before it
    key: tuple[str, str]  # ("agent", agentId) or ("run", runId)


@dataclass
class ScanStats:
    files: int = 0
    subagents: int = 0
    workflow_agents: int = 0
    bytes: int = 0
    seconds: float = 0.0


@dataclass
class Session:
    """A parsed agent session transcript."""

    path: str
    session_id: str = ""
    cwd: str = ""
    git_branch: str = ""
    slug: str = ""
    version: str = ""
    events: list[Event] = field(default_factory=list)
    first_timestamp: str = ""
    last_timestamp: str = ""
    # Where each human prompt starts (index of the next event), and where
    # delegated results came back, for telling a relayed claim apart.
    prompt_starts: list[int] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    main_only: bool = True
    scan: ScanStats = field(default_factory=ScanStats)

    def tool_calls(self) -> list[Event]:
        return [e for e in self.events if e.kind is EventKind.TOOL_CALL]

    def text_events(self) -> list[Event]:
        return [e for e in self.events if e.kind is EventKind.TEXT]


class ClaimType(enum.Enum):
    TESTS_PASS = "tests_pass"
    BUILD_OK = "build_ok"
    LINT_OK = "lint_ok"
    TYPECHECK_OK = "typecheck_ok"
    FILE_CREATED = "file_created"
    COMMIT_MADE = "commit_made"
    PUSHED = "pushed"
    TASK_DONE = "task_done"


@dataclass
class Claim:
    """A checkable assertion the agent made in its prose."""

    type: ClaimType
    quote: str  # the sentence the claim came from, trimmed
    event_index: int  # index of the text event containing it
    detail: str = ""  # e.g. the file path for FILE_CREATED


class Verdict(enum.Enum):
    VERIFIED = "verified"  # evidence in the transcript backs the claim
    STALE = "stale"  # evidence exists but code changed after it
    UNVERIFIED = "unverified"  # no evidence either way
    CONTRADICTED = "contradicted"  # evidence shows the claim is false


@dataclass
class Finding:
    """The verdict on one claim, with a pointer to the evidence."""

    claim: Claim
    verdict: Verdict
    evidence: str = ""  # human-readable: what we checked and what we saw
    evidence_index: int | None = None  # main event index of the deciding evidence
    evidence_source: str = ""  # where the deciding evidence ran: "main" or a sub-agent
    relayed: bool = False  # the claim passes on a delegated agent's report


class GamingSeverity(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class GamingSignal:
    """A pattern that suggests the agent made checks pass by weakening them."""

    kind: str
    severity: GamingSeverity
    description: str
    event_index: int | None  # main event index, None in a delegated transcript
    source: str = "main"


@dataclass
class AuditResult:
    """Everything the audit produced for one session."""

    session: Session
    findings: list[Finding] = field(default_factory=list)
    gaming_signals: list[GamingSignal] = field(default_factory=list)
    score: int | None = None  # 0-100, None when there were no claims
    grade: str = ""

    def counts(self) -> dict[str, int]:
        c = {v.value: 0 for v in Verdict}
        for f in self.findings:
            c[f.verdict.value] += 1
        return c
