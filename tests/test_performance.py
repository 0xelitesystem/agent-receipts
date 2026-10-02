"""v0.2: delegated transcripts are streamed, never loaded whole (requirement 4).

The 2,000-file, 1 GB timing lives in benchmarks/bench_delegated.py; these
tests check the properties that make it fast on any machine.
"""

from __future__ import annotations

import builtins
import json
import time
import tracemalloc

from agent_receipts import delegated
from agent_receipts.cli import run_audit
from agent_receipts.models import Verdict

FILLER = "ordinary tool output the auditor never keeps\n" * 400  # ~18 KB


def _agent_file(path, steps: int, start: int = 0) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for n in range(steps):
            at = f"2026-07-01T10:{(start + n) // 60 % 60:02d}:{(start + n) % 60:02d}.000Z"
            name, tool_input, out = (
                ("Bash", {"command": "pytest -q | tail -3"}, "9 passed")
                if n == steps - 1 else
                ("Read", {"file_path": f"/w/f{n}.py"}, FILLER))
            fh.write(json.dumps({"type": "assistant", "timestamp": at, "message": {
                "content": [{"type": "tool_use", "id": f"t{n}", "name": name,
                             "input": tool_input}]}}) + "\n")
            fh.write(json.dumps({"type": "user", "timestamp": at, "message": {
                "content": [{"type": "tool_result", "tool_use_id": f"t{n}",
                             "content": out}]}}) + "\n")


def _session(tmp_path, files: int, steps: int):
    main_file = tmp_path / "proj" / "s.jsonl"
    sub = tmp_path / "proj" / "s" / "subagents"
    sub.mkdir(parents=True)
    for n in range(files):
        _agent_file(sub / f"agent-a{n:04d}.jsonl", steps)
    main_file.write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-07-01T11:00:00.000Z",
        "message": {"content": [{"type": "text", "text": "All tests pass."}]},
    }) + "\n", encoding="utf-8")
    return main_file


class _NoWholeReads:
    """A file that can be iterated line by line but refuses whole reads."""

    def __init__(self, fh):
        self._fh = fh

    def __iter__(self):
        return iter(self._fh)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()

    def read(self, *args):
        raise AssertionError("delegated transcript loaded whole")

    readlines = read


def test_delegated_transcripts_are_iterated_never_read_whole(tmp_path, monkeypatch):
    main_file = _session(tmp_path, files=3, steps=20)

    def guarded_open(path, mode="r", *args, **kwargs):
        fh = builtins.open(path, mode, *args, **kwargs)
        return _NoWholeReads(fh) if str(path).endswith(".jsonl") else fh

    monkeypatch.setattr(delegated, "open", guarded_open, raising=False)
    result = run_audit(str(main_file), check_disk=False)
    assert result.findings[0].verdict is Verdict.VERIFIED


def test_memory_stays_flat_on_a_large_delegated_transcript(tmp_path):
    main_file = _session(tmp_path, files=1, steps=1500)  # one file of ~28 MB
    size = next((tmp_path / "proj" / "s" / "subagents").glob("*.jsonl")).stat().st_size
    assert size > 20 * 1_048_576
    tracemalloc.start()
    try:
        run_audit(str(main_file), check_disk=False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < size / 4, f"peak {peak} bytes for a {size}-byte transcript"


def test_many_delegated_files_audit_quickly(tmp_path):
    main_file = _session(tmp_path, files=300, steps=12)  # ~65 MB in 300 files
    started = time.perf_counter()
    result = run_audit(str(main_file), check_disk=False)
    elapsed = time.perf_counter() - started
    assert result.session.scan.subagents == 300
    # 1 GB in 60 s is about 17 MB/s; allow a slow CI runner 4x that margin.
    assert elapsed < 65 / 17 * 4, f"{elapsed:.1f} s"
