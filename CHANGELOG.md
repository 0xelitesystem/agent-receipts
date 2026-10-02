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
  instead of the check's. Not raised when the command keeps the real
  status (`pipefail`, `PIPESTATUS`, `$LASTEXITCODE`).
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

### Performance

- Delegated transcripts are streamed line by line in binary and only lines
  that can matter are decoded; tool output is dropped once read. On the
  maintainer's Windows machine (warm file cache) a synthetic session of
  2,000 transcripts and 1.07 GB audits in 2.8 s with 11 MB of peak
  allocation (Python 3.14), and a real session of 655 transcripts and
  396 MB in 10 s.

## 0.1.0

First release: claim extraction, verification against the main transcript
and the filesystem, gaming signals, Receipts Score, terminal, Markdown and
JSON reports.
