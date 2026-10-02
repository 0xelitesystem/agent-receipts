# Changelog

## 0.2.0

Sessions that delegate are now audited as a whole.

### Added

- `receipts audit` finds the session's sub-agent transcripts
  (`<session>/subagents/agent-*.jsonl`) and workflow agent transcripts
  (`<session>/subagents/workflows/wf_*/agent-*.jsonl`) next to the main
  transcript. A test, build, lint, type check, commit or file write in any
  of them counts as evidence, and every evidence line names where it ran:
  `[in main]`, `[in sub-agent <id> (<description>)]` or
  `[in workflow <run id>, agent <id> (<description>)]`.
- Honest ordering across files: delegated evidence counts only when its
  result came back before the claim and its agent had finished before the
  claim. A claim made in the turn that a delegated result came back in is
  checked against that agent (or every agent of that workflow) and the
  main session only.
- A claim that only passes on an agent's report ("the agent says 900 tests
  pass") with no command result behind it is UNVERIFIED with the reason
  "relayed from an agent's report, no command result seen". JSON findings
  carry `relayed` and `evidence_source`.
- `--main-only` keeps the v0.1 behaviour (main transcript only).
- New gaming signal `masked_exit_code` (MEDIUM): a test, build, lint or
  type check piped into a pager or filter (`| tail`, `| head`, `| grep`,
  `| Select-Object` and similar), which reports the filter's exit code
  instead of the check's, or followed by other commands, which report
  their own. Not raised when the command keeps the real status
  (`set -o pipefail` before it, `PIPESTATUS`, `$?` or `$LASTEXITCODE`
  read after it).
- Gaming signals are found in delegated transcripts too, labelled with
  where they happened.
- Test runners `node --test`, `deno test`, `bun test`, `mocha`, `tox`,
  `nox`, and `node <path>/build.mjs` as a build, are recognised.
- JSON report: `version`, `scope` (transcripts and bytes read, seconds),
  and `source` on each gaming signal.
- `benchmarks/bench_delegated.py`: builds a synthetic session of any size
  and times the audit.

### Changed

- Commands are read stage by stage (split at `&&`, `||`, `;`, `|` and
  newlines, outside quotes and here-doc bodies) and a stage counts only by
  the program it starts. `grep -rn pytest src` is no longer test evidence.
- `swallowed_failure` (`|| true`, `; exit 0`) is raised only when the
  command runs a test, build, lint or type check. Read-only diagnostics
  such as `ls dir || true` are no longer flagged.
- Each kind of gaming signal is charged once per audit in the score; every
  occurrence is still listed (terminal and Markdown show five per kind,
  JSON shows all).
- A passing run is STALE only for edits inside the folder it ran in (the
  call's working directory, followed through `cd`, `pushd` and
  `Set-Location`); edits to other repositories no longer count. When the
  folder cannot be told, every edit counts, as before.
- Evidence lines end with `[in <where>]`.

### Fixed before release (red-team review of the v0.2 branch)

- A shell command sent to the background counted as a passing run: its
  first result ("Command running in background with ID: ...") read as
  exit 0. It now counts only once its `<task-notification>` arrives, with
  the exit code and time from that notice, in the main session and in
  delegated transcripts alike. A notice that reports a failure
  contradicts the claim; a launch that never completes is no evidence.
  Background shell notices no longer mark a claim as relayed.
- A run whose exit code a pager or filter hid was VERIFIED whenever its
  filtered output showed no failure. It now needs a positive pass marker
  (`N passed`, `# fail 0`, `test result: ok` and similar); without one the
  claim is UNVERIFIED: "exit code hidden by a pipe and output does not show
  the result".
- A delegated result recorded as the last line of a turn counted as part
  of the next turn, which marked the next turn's claims as relayed and
  narrowed them to that agent. Prompts and deliveries are now ordered by
  their line in the main transcript.
- Any mention of `pipefail`, `PIPESTATUS` or `LASTEXITCODE` switched the
  masked-exit signal off, including `set +o pipefail` and an echo. Only
  pipefail turned on before the pipeline, or the status read after it,
  counts now.
- A check followed by `|| echo ...` or by `;` and other commands lost its
  status unnoticed. `|| <command that succeeds>` is now
  `swallowed_failure`; a check followed by non-check commands is
  `masked_exit_code` ("check followed by `git`"), with the same
  pass-marker rule for the verdict.
- Version probes, listings and dry runs (`pytest --version`,
  `--collect-only`, `make -n`, `tsc --version`) counted as test or build
  evidence and as masking. They are now neither.
- `py -3 -m pytest`, `python.exe -X utf8 -m pytest` and
  `timeout -s KILL 60 pytest` were not recognised as test runs.
- An agent whose last record had no timestamp, or was cut off, could count
  as finished before its later work. Its finish time is now the latest
  timestamp on any line, and a cut-off last line means not finished.
- An edit by an agent still running at claim time was ignored for
  staleness. Edits now count by their own time, so a parallel agent's edit
  before the claim makes an earlier pass STALE.
- Found while fixing the above: cargo's passing summary
  (`test result: ok. 9 passed; 0 failed`) read as a failure because a bare
  `FAILED` matched without regard to case; it now matches in capitals
  only. `--passWithNoTests` inside a here-doc or a quoted script, and a
  `||` inside a `( ... )` group, are no longer flagged.

### Performance

- Delegated transcripts are streamed line by line in binary and only lines
  that can matter are decoded; tool output is dropped once read. On the
  maintainer's Windows machine (warm file cache) a synthetic session of
  2,001 transcripts and 1.07 GB audits in 3.3 s with 12 MB of peak
  allocation (Python 3.14; 6.1 s with 3.10), and a real session of 657
  transcripts and 418 MB in 10.9 s.

## 0.1.0

First release: claim extraction, verification against the main transcript
and the filesystem, gaming signals, Receipts Score, terminal, Markdown and
JSON reports.
