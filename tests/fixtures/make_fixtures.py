"""Write the synthetic delegated-session fixtures under tests/fixtures/sessions.

Every record here is made up. The layout copies what Claude Code writes:

    sessions/<project>/<session>.jsonl                          main
    sessions/<project>/<session>/subagents/agent-<id>.jsonl     sub-agent
    sessions/<project>/<session>/subagents/agent-<id>.meta.json
    sessions/<project>/<session>/subagents/workflows/<run>/agent-<id>.jsonl
    sessions/<project>/<session>/subagents/workflows/<run>/journal.jsonl

Run `python tests/fixtures/make_fixtures.py` after changing a scenario;
tests/test_delegated.py checks the committed files match this script.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).parent / "sessions" / "proj"
CWD = "/work/app"


def ts(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"2026-07-01T10:{m:02d}:{s:02d}.000Z"


def _base(kind: str, at: int, session: str, sidechain: bool, **extra) -> dict:
    rec = {"type": kind, "timestamp": ts(at), "sessionId": session,
           "isSidechain": sidechain, "cwd": CWD}
    rec.update(extra)
    return rec


class Transcript:
    def __init__(self, session: str, sidechain: bool, agent_id: str = ""):
        self.session, self.sidechain, self.agent_id = session, sidechain, agent_id
        self.records: list[dict] = []

    def _add(self, rec: dict) -> None:
        if self.agent_id:
            rec["agentId"] = self.agent_id
        self.records.append(rec)

    def prompt(self, at: int, text: str) -> None:
        self._add(_base("user", at, self.session, self.sidechain,
                        message={"role": "user", "content": text}))

    def say(self, at: int, text: str) -> None:
        self._add(_base("assistant", at, self.session, self.sidechain,
                        message={"role": "assistant",
                                 "content": [{"type": "text", "text": text}]}))

    def tool(self, at: int, tid: str, name: str, tool_input: dict) -> None:
        self._add(_base("assistant", at, self.session, self.sidechain,
                        message={"role": "assistant", "content": [
                            {"type": "tool_use", "id": tid, "name": name,
                             "input": tool_input}]}))

    def result(self, at: int, tid: str, content: str, is_error: bool = False,
               tool_use_result=None) -> None:
        rec = _base("user", at, self.session, self.sidechain,
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": tid,
                         "content": content, "is_error": is_error}]})
        if tool_use_result is not None:
            rec["toolUseResult"] = tool_use_result
        self._add(rec)

    def run(self, at: int, tid: str, command: str, output: str,
            exit_code: int = 0, took: int = 5) -> None:
        self.tool(at, tid, "Bash", {"command": command})
        if exit_code:
            self.result(at + took, tid, f"Exit code {exit_code}\n{output}", is_error=True)
        else:
            self.result(at + took, tid, output)

    def edit(self, at: int, tid: str, path: str) -> None:
        self.tool(at, tid, "Edit", {"file_path": path, "old_string": "a",
                                    "new_string": "b"})
        self.result(at + 1, tid, "ok")

    def notify(self, at: int, task_id: str, tool_use_id: str, result: str) -> None:
        text = (f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<status>completed</status>\n<summary>done</summary>\n"
                f"<result>{result}</result>\n</task-notification>")
        self._add(_base("user", at, self.session, self.sidechain,
                        message={"role": "user", "content": text}))

    def background(self, at: int, tid: str, command: str, task_id: str) -> None:
        """A shell command sent to the background: the result only says it started."""
        self.tool(at, tid, "Bash", {"command": command, "run_in_background": True})
        self.result(at + 1, tid,
                    f"Command running in background with ID: {task_id}. "
                    f"Output is being written to: /tmp/tasks/{task_id}.output",
                    tool_use_result={"stdout": "", "stderr": "", "interrupted": False,
                                     "backgroundTaskId": task_id})

    def bg_notice(self, at: int, task_id: str, tool_use_id: str, code: int,
                  shape: str = "user") -> None:
        """The completion notice of a background command, in one of its shapes."""
        ending = (f"completed (exit code {code})" if code == 0
                  else f"failed with exit code {code}")
        text = (f"<task-notification>\n<task-id>{task_id}</task-id>\n"
                f"<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<status>{'completed' if code == 0 else 'failed'}</status>\n"
                f"<summary>Background command completed: {ending}</summary>\n"
                f"</task-notification>")
        if shape == "queue":
            self._add({"type": "queue-operation", "operation": "enqueue",
                       "timestamp": ts(at), "sessionId": self.session, "content": text})
        elif shape == "attachment":
            self._add(_base("attachment", at, self.session, self.sidechain,
                            attachment={"type": "queued_command", "prompt": text,
                                        "commandMode": "task-notification"}))
        else:
            self._add(_base("user", at, self.session, self.sidechain,
                            message={"role": "user", "content": text}))

    def lines(self) -> str:
        return "\n".join(json.dumps(r, sort_keys=True) for r in self.records) + "\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def save(session: str, main: Transcript, subs=(), runs=(), tails=None) -> None:
    """subs: [(agent_id, description, Transcript)]; runs: [(run_id, [(id, desc, T)])].

    tails: {agent_id: raw text appended to that sub-agent's transcript}.
    """
    write(ROOT / f"{session}.jsonl", main.lines())
    base = ROOT / session / "subagents"
    for agent_id, desc, transcript in subs:
        write(base / f"agent-{agent_id}.jsonl",
              transcript.lines() + (tails or {}).get(agent_id, ""))
        write(base / f"agent-{agent_id}.meta.json",
              json.dumps({"agentType": "general", "description": desc}) + "\n")
    for run_id, agents in runs:
        journal = [{"type": "launched"}]
        for agent_id, desc, transcript in agents:
            write(base / "workflows" / run_id / f"agent-{agent_id}.jsonl",
                  transcript.lines())
            write(base / "workflows" / run_id / f"agent-{agent_id}.meta.json",
                  json.dumps({"agentType": "workflow-subagent",
                              "description": desc}) + "\n")
            journal += [{"type": "started", "agentId": agent_id, "label": desc},
                        {"type": "result", "agentId": agent_id, "result": "ok"}]
        write(base / "workflows" / run_id / "journal.jsonl",
              "\n".join(json.dumps(j) for j in journal) + "\n")


def agent_sync() -> None:
    """A synchronous sub-agent ran the tests; main relays the result."""
    sid = "s1-sync"
    main = Transcript(sid, False)
    main.prompt(0, "Fix the parser and make sure the tests pass.")
    main.tool(1, "tA1", "Agent", {"description": "fix parser", "prompt": "Fix it."})
    main.result(30, "tA1", "Fixed the parser. 12 tests pass.\nagentId: a1sync",
                tool_use_result={"status": "completed", "agentId": "a1sync"})
    main.say(31, "All 12 tests pass.")
    sub = Transcript(sid, True, "a1sync")
    sub.prompt(1, "Fix it.")
    sub.edit(2, "s1", f"{CWD}/src/parser.py")
    sub.run(10, "s2", "cd /work/app && python -m pytest -q", "12 passed in 0.40s", took=10)
    sub.say(25, "Fixed the parser. 12 tests pass.")
    save(sid, main, subs=[("a1sync", "fix parser", sub)])


def workflow_async() -> None:
    """A background workflow built and tested; main relays after the notification."""
    sid = "s2-wf"
    main = Transcript(sid, False)
    main.prompt(0, "Build and test the release.")
    main.tool(1, "tW1", "Workflow", {"description": "build and test"})
    main.result(2, "tW1", "Workflow launched in background. Task ID: wtask1\n"
                "Transcript dir: x/subagents/workflows/wf_fx-001",
                tool_use_result={"status": "async_launched", "taskId": "wtask1",
                                 "runId": "wf_fx-001"})
    main.notify(50, "wtask1", "tW1", "built, 900 tests pass")
    main.say(51, "The build is clean and all 900 tests pass.")
    agent = Transcript(sid, True, "b1build")
    agent.prompt(3, "Build and test.")
    agent.run(5, "w1", "npm run build", "built in 3s")
    agent.run(15, "w2", "cd /work/app && npm test 2>&1 | tail -3", "Tests: 900 passed, 900 total")
    agent.say(30, "built, 900 tests pass")
    save(sid, main, runs=[("wf_fx-001", [("b1build", "build and test", agent)])])


def ordering() -> None:
    """Delegated evidence only counts once it finished before the claim."""
    sid = "s3-order"
    main = Transcript(sid, False)
    main.prompt(0, "Run the tests in the background.")
    main.tool(1, "tA1", "Agent", {"description": "late tests", "prompt": "Test."})
    main.result(2, "tA1", "Async agent launched successfully.\nagentId: c1late",
                tool_use_result={"status": "async_launched", "isAsync": True,
                                 "agentId": "c1late"})
    main.say(5, "All tests pass.")
    main.notify(40, "c1late", "tA1", "tests pass")
    main.say(41, "Tests pass now.")
    late = Transcript(sid, True, "c1late")
    late.prompt(2, "Test.")
    late.run(8, "l1", "pytest -q", "7 passed", took=2)
    late.say(30, "tests pass")
    busy = Transcript(sid, True, "d1busy")
    busy.prompt(0, "Test and keep going.")
    busy.run(1, "b1", "pytest -q", "7 passed", took=1)
    busy.say(39, "still working")
    save(sid, main, subs=[("c1late", "late tests", late), ("d1busy", "busy", busy)])


def relay_only() -> None:
    """An agent reported passing tests but never ran a command."""
    sid = "s4-relay"
    main = Transcript(sid, False)
    main.prompt(0, "Check the tests.")
    main.tool(1, "tA1", "Agent", {"description": "talker", "prompt": "Check."})
    main.result(20, "tA1", "Done. 900 tests pass.\nagentId: e1talk",
                tool_use_result={"status": "completed", "agentId": "e1talk"})
    main.say(21, "The agent says 900 tests pass.")
    sub = Transcript(sid, True, "e1talk")
    sub.prompt(1, "Check.")
    sub.tool(2, "r1", "Read", {"file_path": f"{CWD}/README.md"})
    sub.result(3, "r1", "# app")
    sub.say(10, "Done. 900 tests pass.")
    save(sid, main, subs=[("e1talk", "talker", sub)])


def contradicted() -> None:
    """A workflow agent's test run failed; main says the tests pass."""
    sid = "s5-red"
    main = Transcript(sid, False)
    main.prompt(0, "Test it.")
    main.tool(1, "tW1", "Workflow", {"description": "test"})
    main.result(2, "tW1", "Workflow launched in background. Task ID: wtask5",
                tool_use_result={"status": "async_launched", "taskId": "wtask5",
                                 "runId": "wf_fx-005"})
    main.notify(40, "wtask5", "tW1", "tests pass")
    main.say(41, "All tests pass.")
    agent = Transcript(sid, True, "f1fail")
    agent.prompt(3, "Test.")
    agent.run(5, "w1", "pytest -q", "2 failed, 10 passed", exit_code=1)
    agent.say(20, "tests pass")
    save(sid, main, runs=[("wf_fx-005", [("f1fail", "tester", agent)])])


def stale() -> None:
    """A sub-agent tested, then edited code, and main claims the tests pass."""
    sid = "s6-stale"
    main = Transcript(sid, False)
    main.prompt(0, "Fix and test.")
    main.tool(1, "tA1", "Agent", {"description": "fixer", "prompt": "Fix."})
    main.result(40, "tA1", "Done.\nagentId: g1stale",
                tool_use_result={"status": "completed", "agentId": "g1stale"})
    main.say(41, "Tests pass.")
    sub = Transcript(sid, True, "g1stale")
    sub.prompt(1, "Fix.")
    sub.run(2, "s1", "npm test", "Tests: 5 passed, 5 total")
    sub.edit(10, "s2", f"{CWD}/src/index.js")
    sub.say(30, "Done.")
    save(sid, main, subs=[("g1stale", "fixer", sub)])


def secrets() -> None:
    """Credentials in a sub-agent's commands and description stay out of reports."""
    sid = "s7-secret"
    main = Transcript(sid, False)
    main.prompt(0, "Test with the real token.")
    main.tool(1, "tA1", "Agent", {"description": "secret", "prompt": "Test."})
    main.result(30, "tA1", "Done.\nagentId: h1secret",
                tool_use_result={"status": "completed", "agentId": "h1secret"})
    main.say(31, "All tests pass.")
    sub = Transcript(sid, True, "h1secret")
    sub.prompt(1, "Test.")
    sub.run(2, "s1", "GITHUB_TOKEN=ghp_FIXTUREFIXTUREFIXTUREFIXTURE0123 npm test || true",
            "Tests: 3 passed")
    sub.say(20, "Done.")
    save(sid, main, subs=[("h1secret", "uses api_key=sk-proj-FIXTURE0123456789abcdef", sub)])


def background_main() -> None:
    """Main sends the tests to the background; only the completion notice counts."""
    sid = "s8-bg"
    main = Transcript(sid, False)
    main.prompt(0, "Run the tests in the background.")
    main.background(1, "tB1", "npm test", "bgt8")
    main.say(3, "All tests pass.")  # nothing has finished yet
    main.bg_notice(20, "bgt8", "tB1", 0, shape="queue")
    main.bg_notice(21, "bgt8", "tB1", 0, shape="attachment")
    main.say(22, "The tests pass now.")
    main.background(30, "tB2", "pytest -q", "bgt9")
    main.bg_notice(40, "bgt9", "tB2", 1, shape="user")
    main.say(41, "Tests pass.")
    save(sid, main)


def background_sub() -> None:
    """Sub-agents send the tests to the background: one never finishes, one fails."""
    sid = "s9-bgsub"
    main = Transcript(sid, False)
    main.prompt(0, "Test it.")
    main.tool(1, "tA1", "Agent", {"description": "starter", "prompt": "Test."})
    main.result(20, "tA1", "Started the tests.\nagentId: i1start",
                tool_use_result={"status": "completed", "agentId": "i1start"})
    main.say(21, "All tests pass.")
    main.prompt(30, "Test it again.")
    main.tool(31, "tA2", "Agent", {"description": "waiter", "prompt": "Test."})
    main.result(60, "tA2", "Done.\nagentId: j1wait",
                tool_use_result={"status": "completed", "agentId": "j1wait"})
    main.say(61, "All tests pass.")
    start = Transcript(sid, True, "i1start")
    start.prompt(1, "Test.")
    start.background(2, "s1", "npm test", "bgi1")
    start.say(5, "Started the tests.")
    wait = Transcript(sid, True, "j1wait")
    wait.prompt(31, "Test.")
    wait.background(32, "w1", "pytest -q", "bgj1")
    wait.bg_notice(50, "bgj1", "w1", 1)
    wait.say(55, "Done.")
    save(sid, main, subs=[("i1start", "starter", start), ("j1wait", "waiter", wait)])


def turn_boundary() -> None:
    """A delivery that is the last record of a turn belongs to that turn only."""
    sid = "s10-turn"
    main = Transcript(sid, False)
    main.prompt(0, "Test, then report.")
    main.tool(1, "tA1", "Agent", {"description": "tester", "prompt": "Test."})
    main.result(20, "tA1", "5 passed.\nagentId: k1test",
                tool_use_result={"status": "completed", "agentId": "k1test"})
    main.tool(21, "tA2", "Agent", {"description": "talker", "prompt": "Talk."})
    main.result(40, "tA2", "Nothing to run.\nagentId: k2talk",
                tool_use_result={"status": "completed", "agentId": "k2talk"})
    main.prompt(50, "And the tests?")
    main.say(51, "All tests pass.")
    test = Transcript(sid, True, "k1test")
    test.prompt(1, "Test.")
    test.run(2, "s1", "pytest -q", "5 passed")
    test.say(15, "5 passed.")
    talk = Transcript(sid, True, "k2talk")
    talk.prompt(21, "Talk.")
    talk.say(30, "Nothing to run.")
    save(sid, main, subs=[("k1test", "tester", test), ("k2talk", "talker", talk)])


def still_writing() -> None:
    """Agents still at work after the claim: last record has no time, or is cut off."""
    sid = "s11-live"
    main = Transcript(sid, False)
    main.prompt(0, "Test.")
    main.say(21, "All tests pass.")
    notime = Transcript(sid, True, "l1late")
    notime.prompt(1, "Test.")
    notime.run(2, "s1", "pytest -q", "3 passed", took=1)
    notime.say(60, "still going")
    cut = Transcript(sid, True, "l2cut")
    cut.prompt(1, "Test.")
    cut.run(2, "s1", "pytest -q", "3 passed", took=1)
    save(sid, main, subs=[("l1late", "no time", notime), ("l2cut", "cut off", cut)],
         tails={"l1late": json.dumps({"type": "summary", "summary": "x"}) + "\n",
                "l2cut": '{"type": "assistant", "timestamp": "' + ts(70) + '", "mess'})


def parallel_edit() -> None:
    """A background agent edits the code after main's passing run, before the claim."""
    sid = "s12-pedit"
    main = Transcript(sid, False)
    main.prompt(0, "Test while the other agent works.")
    main.run(1, "m1", "pytest -q", "5 passed", took=2)
    main.say(30, "All tests pass.")
    other = Transcript(sid, True, "m2edit")
    other.prompt(5, "Refactor.")
    other.edit(9, "s1", f"{CWD}/src/core.py")
    other.say(60, "still refactoring")
    save(sid, main, subs=[("m2edit", "refactor", other)])


SCENARIOS = [agent_sync, workflow_async, ordering, relay_only, contradicted, stale, secrets,
             background_main, background_sub, turn_boundary, still_writing, parallel_edit]


def main() -> None:
    for scenario in SCENARIOS:
        scenario()
    print(f"wrote fixtures under {ROOT}")


if __name__ == "__main__":
    main()
