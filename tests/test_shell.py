"""v0.2: reading commands stage by stage, and the masked-exit-code signal (requirement 3)."""

from __future__ import annotations

import time

import pytest

from agent_receipts.evidence import check_types, masking
from agent_receipts.gaming import signals_for_event
from agent_receipts.models import ClaimType, Event, EventKind
from agent_receipts.shell import normalize_stage, split_statements


def _kinds(command: str) -> list[str]:
    return [kind for kind, _, _ in masking(command)]


@pytest.mark.parametrize("command, filter_name", [
    ("pytest -q | tail -5", "tail"),
    ("cd app && npm test 2>&1 | tail -20", "tail"),
    ("python -m pytest | head -n 40", "head"),
    ("npm run build | grep -i error", "grep"),
    ("npm test | Select-Object -Last 5", "select-object"),
    ('& "C:\\Python314\\python.exe" -m pytest -q | Select-Object -Last 3', "select-object"),
    ("for d in a b; do (cd $d && cargo test 2>&1 | tail -3); done", "tail"),
    ('node --test "test/**/*.test.js" 2>&1 | grep -E "pass|fail"', "grep"),
    ('bash -c "pytest -q" | head', "head"),
])
def test_check_piped_into_a_filter_is_masked(command, filter_name):
    found = [m for m in masking(command) if m[0] == "masked_exit_code"]
    assert found and found[0][1] == "| " + filter_name


@pytest.mark.parametrize("command", [
    "grep -rn pytest src | head",            # reads files, runs no tests
    "cat pytest.ini | head -5",
    "ls tests | wc -l",
    "git log --oneline | head -20",
    "ls missing || true",                    # read-only diagnostic
    "grep -q TODO notes.md || true",
    'grep "pytest|tail" f',                  # the | is inside quotes
    "set -o pipefail; pytest | tail",        # the real status is kept
    "pytest | tail; exit ${PIPESTATUS[0]}",
    "npm test | Select-Object -Last 5; exit $LASTEXITCODE",
    "pytest -q || exit",                     # exit keeps the failing status
    "cat <<EOF > run.sh\npytest | tail\nnpm test || true\nEOF\nbash -n run.sh",
])
def test_read_only_diagnostics_and_kept_status_are_not_flagged(command):
    assert masking(command) == []


@pytest.mark.parametrize("command", [
    "npm test || true",
    "OPENAI_API_KEY=x npm test || true",
    "cd app && pytest -q || :",
    "cargo test; exit 0",
    "npx jest --passWithNoTests",
])
def test_swallowed_failures_on_checks_are_flagged(command):
    assert "swallowed_failure" in _kinds(command)


def test_swallowed_snippet_quotes_the_chain():
    (kind, how, snippet), = masking("echo start; cd app && pytest -q || true")
    assert (kind, how) == ("swallowed_failure", "|| true")
    assert snippet.strip() == "cd app && pytest -q || true"


def test_signal_quotes_the_masking_pipeline_not_the_preamble():
    command = 'cd "/a/very/long/path/that/would/fill/the/quote/on/its/own" && npm test 2>&1 | tail -3'
    event = Event(kind=EventKind.TOOL_CALL, index=0, tool_name="Bash",
                  tool_input={"command": command})
    signal, = signals_for_event(event)
    assert signal.kind == "masked_exit_code"
    assert "`npm test 2>&1 | tail -3`" in signal.description


@pytest.mark.parametrize("command, expected", [
    ("C:/Python314/python.exe -m pytest -q", {ClaimType.TESTS_PASS}),
    ("PYTHONDONTWRITEBYTECODE=1 python3.11 -m pytest", {ClaimType.TESTS_PASS}),
    ("uv run pytest", {ClaimType.TESTS_PASS}),
    ('node --test "test/**/*.test.js"', {ClaimType.TESTS_PASS}),
    ("node scripts/build.mjs", {ClaimType.BUILD_OK}),
    ("git -C repo commit -m x", {ClaimType.COMMIT_MADE}),
    ("grep -rn pytest src", set()),
    ("echo npm test", set()),
    ("cat build.log | grep make", set()),
])
def test_evidence_is_judged_by_the_program_each_stage_starts(command, expected):
    assert set(check_types(command)) == expected


def test_normalize_stage_strips_prefixes_and_paths():
    assert normalize_stage('FOO=1 sudo "C:/Python314/python.exe" -m pytest') == \
        "python -m pytest"
    assert normalize_stage("timeout 60 npm test") == "npm test"


def test_statements_keep_operators_and_spans():
    command = "a && b | c || d; e"
    statements = split_statements(command)
    assert [(s.stages, s.op) for s in statements] == [
        (["a"], "&&"), (["b", "c"], "||"), (["d"], ";"), (["e"], "")]
    assert [command[s.start:s.end].strip() for s in statements] == ["a", "b | c", "d", "e"]


_HOSTILE = {
    "pipes": "|" * 50_000, "single-quotes": "'" * 50_000,
    "double-quotes": '"' * 50_000, "hashes": "#" * 50_000,
    "comments": "a #" * 20_000, "heredocs": "<<EOF\n" * 10_000,
    "pipelines": "pytest | " * 5_000, "ands": "&&" * 25_000,
    "backslashes": "\\" * 50_000, "here-strings": "@'\n" * 10_000,
    "wrappers": 'bash -c "' * 5_000,
}


@pytest.mark.parametrize("name", sorted(_HOSTILE))
def test_shell_reading_is_linear_on_hostile_input(name):
    hostile = _HOSTILE[name]
    started = time.perf_counter()
    masking(hostile)
    check_types(hostile)
    assert time.perf_counter() - started < 5


def test_masked_run_is_judged_from_its_output(tmp_path):
    from agent_receipts.cli import run_audit
    from agent_receipts.models import Verdict
    from conftest import assistant_text, assistant_tool, tool_result, write_jsonl
    red = write_jsonl(tmp_path / "red.jsonl", [
        assistant_tool("t1", "Bash", {"command": "pytest -q | tail -3"}),
        tool_result("t1", "2 failed, 3 passed"),
        assistant_text("All tests pass."),
    ])
    green = write_jsonl(tmp_path / "green.jsonl", [
        assistant_tool("t1", "Bash", {"command": "pytest -q | tail -3"}),
        tool_result("t1", "5 passed"),
        assistant_text("All tests pass."),
    ])
    red_finding, = run_audit(red, check_disk=False).findings
    green_finding, = run_audit(green, check_disk=False).findings
    assert red_finding.verdict is Verdict.CONTRADICTED
    assert green_finding.verdict is Verdict.VERIFIED
    assert "exit code hidden by a pipe" in green_finding.evidence


def test_norm_path_compares_windows_and_msys_forms():
    from agent_receipts.shell import norm_path
    assert norm_path("/c/Users/X/repo") == "c:/users/x/repo"
    assert norm_path(r"C:\Users\X\repo\..\b") == "c:/users/x/b"
    assert norm_path("src/a.py", "c:/r") == "c:/r/src/a.py"
    assert norm_path("src/a.py") is None
    assert norm_path("$HOME/x") is None


def test_run_directory_follows_cd():
    from agent_receipts.shell import run_directory
    is_test = lambda stage: ClaimType.TESTS_PASS in check_types(stage)  # noqa: E731
    assert run_directory('cd "C:/Users/X/repo" && npm test | tail', r"C:\other",
                         is_test) == "c:/users/x/repo"
    assert run_directory("npm test", r"C:\proj", is_test) == "c:/proj"
    assert run_directory("cd sub; pytest", "/work/app", is_test) == "/work/app/sub"
    assert run_directory("cd $D && pytest", "/work/app", is_test) is None
    assert run_directory(r'Set-Location -Path "C:\r"; npm test', "", is_test) == "c:/r"
    assert run_directory("ls", "/work/app", is_test) is None


def test_edits_in_another_repository_do_not_make_a_run_stale(tmp_path):
    from agent_receipts.cli import run_audit
    from agent_receipts.models import Verdict
    from conftest import assistant_text, assistant_tool, tool_result, write_jsonl

    def session(name, edited):
        return write_jsonl(tmp_path / name, [
            assistant_tool("t1", "Bash", {"command": 'cd "C:/work/api" && pytest -q'}),
            tool_result("t1", "5 passed"),
            assistant_tool("t2", "Edit", {"file_path": edited, "old_string": "a",
                                          "new_string": "b"}),
            tool_result("t2", "ok"),
            assistant_text("All tests pass."),
        ])
    other, = run_audit(session("other.jsonl", r"C:\work\web\src\app.js"),
                       check_disk=False).findings
    same, = run_audit(session("same.jsonl", r"C:\work\api\src\app.py"),
                      check_disk=False).findings
    assert other.verdict is Verdict.VERIFIED
    assert same.verdict is Verdict.STALE
