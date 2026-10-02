# Auditing sessions that delegate

Modern Claude Code sessions hand work to sub-agents (the Agent tool) and to
workflows (many agents run by a script). Each of those agents keeps its own
transcript. From v0.2, `receipts audit` reads them too, so a "tests pass"
backed by a test run inside an agent is no longer reported as UNVERIFIED.

## Where the transcripts are

```
~/.claude/projects/<project>/
  <session>.jsonl                                    the main transcript
  <session>/subagents/agent-<id>.jsonl               a sub-agent
  <session>/subagents/agent-<id>.meta.json           its description
  <session>/subagents/workflows/<run>/agent-<id>.jsonl   one agent of a workflow run
  <session>/subagents/workflows/<run>/journal.jsonl      the run's journal
```

`receipts` lists only these folders, never walks the disk, does not follow
symlinked folders, skips compressed (`.zst`) copies, and reads
`.jsonl.superseded-<n>` and `.orphaned-` copies as the agent they copy. The
journal is not needed: the agent transcripts hold the commands.

`--main-only` turns all of this off and audits the main transcript alone,
as v0.1 did.

## What counts as evidence

The same things as in the main session: a test, build, lint or type check
run (by the program a pipeline stage starts, so `grep -rn pytest src` is not
a test run), `git commit`, `git push`, and Write/Edit calls. Each evidence
line says where it ran:

```
✓ VERIFIED     "The build is clean and all 900 tests pass."
  └─ `npm run build` succeeded (exit 0) [in workflow wf_fixture-001, agent b1build (build and test)]
```

The description in brackets comes from the agent's `.meta.json`, trimmed to
60 characters, with credentials masked like every other quoted text.

## Ordering

A claim can only be backed by something that had already happened.

- Main-session events are ordered by their position in the main transcript,
  as in v0.1.
- An event in a delegated transcript counts for a claim only when its
  result came back before the claim (comparing timestamps, with time zone
  offsets applied), and the agent's transcript ended before the claim. An
  agent that was still working when the main agent spoke cannot back what
  the main agent said, even if one of its runs had already passed.
- If the main transcript carries no timestamps, nothing delegated can be
  ordered against it and only main-session evidence is used.

## Stale runs

A passing run is STALE when code was edited after it and before the claim.
Since v0.2 only edits inside the folder the check ran in count: the folder
is the call's working directory, followed through `cd`, `pushd` and
`Set-Location` in the command. A parallel agent editing another repository
no longer makes the run stale. When the folder cannot be told (a variable
in the path, say), every edit counts, as in v0.1.

## Relayed claims

A claim made in the same turn that a delegated result came back in (the
Agent tool returned, or a `<task-notification>` arrived) passes on that
agent's report. It is checked against that agent, or every agent of that
workflow run, plus the main session. A claim that says so in words ("the
agent says...", "according to the workflow...") is also treated as
relayed.

When a relayed claim has no command result behind it, it is UNVERIFIED
with its own reason:

```
? UNVERIFIED   "The agent says 900 tests pass."
  └─ relayed from an agent's report, no command result seen
```

An agent's own summary ("I ran the tests, 900 pass") is never evidence. The
command and its result are.

## Gaming signals in delegated transcripts

Every gaming signal is looked for in every transcript, and those found in
a delegated one are labelled with where they happened. Each kind of
signal is charged once per audit in the score, however often it repeats;
the terminal and Markdown reports show five of each kind, the JSON report
all of them.

`masked_exit_code` is new in v0.2: a test, build, lint or type check piped
into a pager or filter (`tail`, `head`, `grep`, `Select-Object`,
`Select-String`, `sort`, `tee` and similar) reports the filter's exit code,
so a failing run looks like exit 0. It is not raised when the command keeps
the real status (`set -o pipefail`, `PIPESTATUS`, `$LASTEXITCODE`), and
pipelines that run no check (`git log | head`) are never flagged. The
verdict on such a run is still judged from its output, so `pytest | tail`
that printed `2 failed` is CONTRADICTED, and the evidence line says the exit
code was hidden.

## Performance

Delegated transcripts are streamed line by line in binary. A line is decoded
only when it carries a `tool_use` block or answers a tool call the reader is
waiting on, and only shell, Write and Edit calls are kept. Tool output is
read once, for its exit status and failure markers, then dropped.

`benchmarks/bench_delegated.py` builds a synthetic session and times it:

```
python benchmarks/bench_delegated.py /tmp/bench --files 2000 --mb 1024 --memory
```

Measured on the maintainer's Windows 10 machine, warm file cache: 2,000
transcripts and 1.07 GB in 2.8 s with Python 3.14 (3.7 s with 3.10), peak
allocation 11 MB (15 MB). A real session of 655 transcripts and 396 MB
took 8.5 s to read with Python 3.14, 10 s for the whole command. Real
sessions cost more per byte than the synthetic one because more of their
lines are tool calls.

## Limitations

- Which run backs a claim is chosen by claim type and time, not by project.
  A test run in another repository by another agent can back or contradict
  a claim if it is the most recent eligible run. Relayed claims narrow this
  to the delivered agents; other claims see every agent that had finished.
  (Staleness is scoped to the run's folder, see above.)
- "Finished" means the agent's transcript had no later record. A
  background agent that is idle but alive looks finished.
- A test run that is meant to fail (mutation testing, a negative control)
  looks like a failure and can contradict a claim.
- Turn boundaries come from user messages that a person typed. Harness
  notes and tool results never start a turn; a prompt that starts with `<`
  is not recognised as one.
- The shell reader is a small scanner, not a shell: aliases, functions,
  `eval`, scripts that run tests inside (`bash ci.sh`) and commands built at
  run time are not seen as checks.
- Claim extraction is unchanged from v0.1: English, regex based, and it can
  read a plan bullet ("build the bundle, get every test passing") as a
  claim.
