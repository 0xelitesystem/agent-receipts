"""Time an audit of a large synthetic session with many delegated transcripts.

    python benchmarks/bench_delegated.py OUT_DIR [--files 2000] [--mb 1024]

Writes OUT_DIR/projects/bench/<session>.jsonl plus OUT_DIR/projects/bench/
<session>/subagents/... (half sub-agents, half workflow agents spread over
40 runs), then audits it with run_audit and prints the wall time and the bytes
read; with --memory it audits again under tracemalloc and prints the
peak memory the audit allocated. Every record is synthetic. Delete
OUT_DIR afterwards.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_receipts.cli import run_audit  # noqa: E402

SESSION = "00000000-0000-4000-8000-00000000bench"
FILLER = ("line of ordinary tool output that the auditor never needs to keep\n" * 64)


def ts(second: int) -> str:
    day, rest = divmod(second, 86400)
    h, rest = divmod(rest, 3600)
    m, s = divmod(rest, 60)
    return f"2026-07-{1 + day:02d}T{h:02d}:{m:02d}:{s:02d}.000Z"


def record(kind: str, second: int, content, extra: dict | None = None) -> str:
    rec = {"type": kind, "timestamp": ts(second), "sessionId": SESSION,
           "isSidechain": True, "cwd": "/work/app",
           "message": {"role": kind, "content": content}}
    rec.update(extra or {})
    return json.dumps(rec, separators=(",", ":"))


def agent_lines(start: int, target_bytes: int, n: int):
    """Yield JSONL lines for one agent until about target_bytes."""
    written, second, step = 0, start, 0
    while written < target_bytes:
        tid = f"toolu_{n}_{step}"
        if step % 10 == 9:
            use = {"type": "tool_use", "id": tid, "name": "Bash",
                   "input": {"command": "cd /work/app && npm test 2>&1 | tail -5"}}
            out = "Tests: 42 passed, 42 total"
        elif step % 10 == 4:
            use = {"type": "tool_use", "id": tid, "name": "Edit",
                   "input": {"file_path": f"/work/app/src/mod{step}.js",
                             "old_string": "a", "new_string": "b"}}
            out = "ok"
        else:
            use = {"type": "tool_use", "id": tid, "name": "Read",
                   "input": {"file_path": f"/work/app/src/file{step}.js"}}
            out = FILLER * 6
        for line in (
            record("assistant", second, [{"type": "text", "text": "Checking."}, use]),
            record("user", second + 1, [{"type": "tool_result", "tool_use_id": tid,
                                         "content": out}]),
            record("attachment", second + 1, [], {"attachment": {"type": "note"}}),
        ):
            written += len(line) + 1
            yield line
        second += 2
        step += 1


def build(out: Path, files: int, megabytes: int) -> Path:
    project = out / "projects" / "bench"
    sub = project / SESSION / "subagents"
    (sub / "workflows").mkdir(parents=True, exist_ok=True)
    per_file = megabytes * 1_048_576 // files
    for n in range(files):
        if n % 2:
            folder = sub / "workflows" / f"wf_bench-{n % 40:03d}"
            folder.mkdir(exist_ok=True)
        else:
            folder = sub
        with open(folder / f"agent-a{n:015x}.jsonl", "w", encoding="utf-8") as fh:
            for line in agent_lines(n * 10, per_file, n):
                fh.write(line + "\n")
    main = project / f"{SESSION}.jsonl"
    with open(main, "w", encoding="utf-8") as fh:
        fh.write(record("assistant", 10 ** 6, [
            {"type": "text", "text": "All 42 tests pass."}]) + "\n")
    return main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--mb", type=int, default=1024)
    parser.add_argument("--memory", action="store_true",
                        help="audit a second time under tracemalloc for peak memory")
    args = parser.parse_args()
    started = time.perf_counter()
    main_path = build(Path(args.out), args.files, args.mb)
    print(f"built {args.files} files in {time.perf_counter() - started:.1f} s")

    started = time.perf_counter()
    result = run_audit(str(main_path), check_disk=False)
    elapsed = time.perf_counter() - started
    scan = result.session.scan
    print(f"audited {scan.files} transcripts, {scan.bytes / 1_048_576:.0f} MB "
          f"in {elapsed:.1f} s; score {result.score}")
    if args.memory:
        tracemalloc.start()
        run_audit(str(main_path), check_disk=False)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(f"peak memory allocated by the audit: {peak / 1_048_576:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
