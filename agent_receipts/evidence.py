"""Which commands count as evidence, and how a run's result is read.

A command counts as evidence for a claim type when one of its pipeline
stages starts a matching program: `cd app && npm test` runs the tests,
`grep -rn pytest src` does not. Patterns are matched against the stage
after shell.normalize_stage has dropped env assignments, wrappers and
the directory part of the program path.
"""

from __future__ import annotations

import re

from .models import ClaimType
from .shell import FILTERS, normalize_stage, program, split_statements

COMMAND_EVIDENCE: dict[ClaimType, re.Pattern] = {
    ClaimType.TESTS_PASS: re.compile(
        r"pytest\b|python\s+-m\s+(?:pytest|unittest)\b"
        r"|npm\s+(?:run\s+)?test\b|npm\s+t\b|(?:npx|yarn|pnpm|bun)\s+(?:run\s+)?"
        r"(?:test|jest|vitest|mocha|playwright\s+test)\b"
        r"|node\s+(?:\S+\s+)*?--test\b|deno\s+test\b|bun\s+test\b"
        r"|jest\b|vitest\b|mocha\b|go\s+test\b|cargo\s+(?:test|nextest)\b"
        r"|rspec\b|phpunit\b|dotnet\s+test\b|mvn\s+test\b|tox\b|nox\b"
        r"|gradlew?\s+test\b|mix\s+test\b|swift\s+test\b|ctest\b",
        re.IGNORECASE,
    ),
    ClaimType.BUILD_OK: re.compile(
        r"npm\s+run\s+build\b|(?:npx|yarn|pnpm|bun)\s+(?:run\s+)?build\b"
        r"|node\s+(?:\S*[\\/])?build\.[cm]?[jt]s\b"
        r"|cargo\s+build\b|go\s+build\b|make\b|dotnet\s+build\b"
        r"|mvn\s+(?:package|compile|install)\b|gradlew?\s+(?:build|assemble)\b"
        r"|vite\s+build\b|webpack\b|tsc\b.*(?:-b|--build)"
        r"|docker\s+build\b|python\s+-m\s+build\b",
        re.IGNORECASE,
    ),
    ClaimType.LINT_OK: re.compile(
        r"eslint\b|ruff\s+(?:check|format)\b|flake8\b|pylint\b"
        r"|golangci-lint\b|cargo\s+clippy\b|npm\s+run\s+lint\b"
        r"|(?:npx|yarn|pnpm)\s+(?:run\s+)?(?:lint|eslint)\b|biome\s+(?:check|lint)\b"
        r"|python\s+-m\s+(?:ruff|flake8|pylint)\b",
        re.IGNORECASE,
    ),
    ClaimType.TYPECHECK_OK: re.compile(
        r"mypy\b|pyright\b|tsc\b|npm\s+run\s+type-?check\b"
        r"|(?:npx|yarn|pnpm)\s+(?:run\s+)?(?:type-?check|tsc)\b|ty\s+check\b"
        r"|python\s+-m\s+mypy\b",
        re.IGNORECASE,
    ),
    ClaimType.COMMIT_MADE: re.compile(r"git\s+commit\b", re.IGNORECASE),
    ClaimType.PUSHED: re.compile(r"git\s+push\b", re.IGNORECASE),
}

# Checks whose result says something about the code: tests, builds, lint,
# type checks. Masking their exit code is a gaming signal.
CHECK_TYPES = frozenset({
    ClaimType.TESTS_PASS, ClaimType.BUILD_OK,
    ClaimType.LINT_OK, ClaimType.TYPECHECK_OK,
})

# "N failed" with N > 0, or framework-specific hard failure markers.
FAILURE_IN_OUTPUT = re.compile(
    r"\b([1-9]\d*)\s+fail(?:ed|ures?)\b"
    r"|\bFAILED\b|\bTests?\s+failed\b|\bBUILD\s+FAILED\b"
    r"|test result: FAILED"
    r"|^\S*\s*fail\s+[1-9]\d*\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_SWALLOW_TAIL = re.compile(
    r"(?:true|:|\$true|exit\s+0|cmd\s+/c\s+exit\s+0)", re.IGNORECASE)
_EXIT_ZERO = re.compile(r"exit\s+0", re.IGNORECASE)
_PASS_WITH_NO_TESTS = re.compile(r"--passWithNoTests\b")
# A command that keeps or inspects the real status of a pipeline.
_STATUS_KEPT = re.compile(r"pipefail|PIPESTATUS|LASTEXITCODE", re.IGNORECASE)


def stage_types(stage: str) -> frozenset[ClaimType]:
    norm = normalize_stage(stage)
    if not norm:
        return frozenset()
    return frozenset(t for t, pattern in COMMAND_EVIDENCE.items()
                     if pattern.match(norm))


def check_types(command: str) -> frozenset[ClaimType]:
    """Claim types this command can back, judged stage by stage."""
    if not command:
        return frozenset()
    found: set[ClaimType] = set()
    for statement in split_statements(command):
        for stage in statement.stages:
            if stage:
                found |= stage_types(stage)
    return frozenset(found)


def _has_check(stages: list[str]) -> bool:
    return any(stage_types(s) & CHECK_TYPES for s in stages if s)


def masking(command: str) -> list[tuple[str, str, str]]:
    """Ways this command hides the exit status of a test, build or check.

    Returns (kind, how, snippet) triples, where snippet is the part of the
    command that does it. kind is "masked_exit_code" for a pipeline
    whose last stage is a pager or filter, "swallowed_failure" for
    `|| true`, `; exit 0` and --passWithNoTests. Read-only diagnostics
    (`grep x f | head`, `ls d || true`) are not reported.
    """
    found: list[tuple[str, str, str]] = []
    if _PASS_WITH_NO_TESTS.search(command):
        found.append(("swallowed_failure", "--passWithNoTests", command))
    statements = split_statements(command)
    status_kept = bool(_STATUS_KEPT.search(command))
    # Stages of the current &&/|| chain, so `cd x && pytest || true` counts.
    chain: list[str] = []
    chain_start = 0
    seen_check = False
    for pos, statement in enumerate(statements):
        stages = [s for s in statement.stages if s]
        if not stages:
            continue
        is_swallow = (len(stages) == 1 and _SWALLOW_TAIL.fullmatch(
            normalize_stage(stages[0]) or ""))
        prev_op = statements[pos - 1].op if pos else ""
        if is_swallow and prev_op == "||" and _has_check(chain):
            found.append(("swallowed_failure", "|| " + stages[0].strip(),
                          command[chain_start:statement.end]))
        elif (is_swallow and prev_op in (";", "\n") and seen_check
              and _EXIT_ZERO.fullmatch(stages[0].strip())
              and pos == len(statements) - 1):
            found.append(("swallowed_failure", "; exit 0", command))
        if len(stages) > 1 and not status_kept:
            last = program(stages[-1])
            if last in FILTERS and _has_check(stages[:-1]):
                found.append(("masked_exit_code", "| " + last,
                              command[statement.start:statement.end]))
        if _has_check(stages):
            seen_check = True
        if not chain:
            chain_start = statement.start
        chain = (chain + stages) if statement.op in ("&&", "||") else []
    return found
