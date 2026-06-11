"""receipts — audit what your coding agent claimed against what it did.

Usage:
  receipts audit <transcript.jsonl | session-id-prefix | latest> [options]
  receipts list [--project NAME] [--limit N]

Options:
  --project NAME    only consider transcripts whose project folder matches
  --json            emit machine-readable JSON instead of the terminal report
  --md FILE         also write a Markdown report to FILE
  --no-disk-check   skip filesystem checks (transcript evidence only)
  --fail-under N    exit 1 if the Receipts Score is below N (CI gate)
  --no-color        disable ANSI colors
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .claims import extract_claims
from .gaming import detect_gaming
from .models import AuditResult
from .parser import discover_transcripts, parse_transcript, resolve_target
from .report import render_json, render_markdown, render_terminal
from .score import score_audit
from .verify import verify_claims


def run_audit(target: str, project: str | None = None, check_disk: bool = True) -> AuditResult:
    """Library entry point: audit a transcript and return the result."""
    path = resolve_target(target, project)
    session = parse_transcript(path)
    claims = extract_claims(session)
    result = AuditResult(
        session=session,
        findings=verify_claims(session, claims, check_disk=check_disk),
        gaming_signals=detect_gaming(session),
    )
    return score_audit(result)


def _cmd_audit(args: argparse.Namespace) -> int:
    try:
        result = run_audit(args.target, args.project, check_disk=not args.no_disk_check)
    except FileNotFoundError as exc:
        print(f"receipts: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(render_json(result))
    else:
        color = False if args.no_color else None
        print(render_terminal(result, color=color))

    if args.md:
        Path(args.md).write_text(render_markdown(result), encoding="utf-8")
        if not args.json:
            print(f"  markdown report written to {args.md}\n")

    if args.fail_under is not None:
        if result.score is not None and result.score < args.fail_under:
            return 1
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    transcripts = discover_transcripts(args.project)[: args.limit]
    if not transcripts:
        print("no transcripts found under ~/.claude/projects", file=sys.stderr)
        return 2
    for path in transcripts:
        mtime = datetime.fromtimestamp(path.stat().st_mtime)
        size_kb = path.stat().st_size // 1024
        print(f"{path.stem[:8]}  {mtime:%Y-%m-%d %H:%M}  {size_kb:>6} KB  "
              f"{path.parent.name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="receipts",
        description="Audit what your coding agent claimed against what it actually did.",
    )
    parser.add_argument("--version", action="version", version=f"receipts {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="audit one session transcript")
    audit.add_argument("target", help="transcript path, session-id prefix, or 'latest'")
    audit.add_argument("--project", help="filter session discovery by project name")
    audit.add_argument("--json", action="store_true", help="JSON output")
    audit.add_argument("--md", metavar="FILE", help="write Markdown report to FILE")
    audit.add_argument("--no-disk-check", action="store_true",
                       help="skip filesystem verification")
    audit.add_argument("--fail-under", type=int, metavar="N",
                       help="exit 1 if score < N (for CI)")
    audit.add_argument("--no-color", action="store_true", help="plain output")
    audit.set_defaults(func=_cmd_audit)

    lst = sub.add_parser("list", help="list recent session transcripts")
    lst.add_argument("--project", help="filter by project folder name")
    lst.add_argument("--limit", type=int, default=15)
    lst.set_defaults(func=_cmd_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often default to cp1252, which can't print ✓/✗/⚠.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
