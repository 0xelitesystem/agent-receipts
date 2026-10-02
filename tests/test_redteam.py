"""Regression tests for the v0.2 red-team findings F1 to F9.

Each block reproduces one finding with a synthetic fixture and checks
the honest verdict. Fixtures are made up; none comes from a real session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_receipts.cli import run_audit
from agent_receipts.delegated import discover_sources, read_source
from agent_receipts.evidence import check_types, masking
from agent_receipts.models import ClaimType, EventKind, Verdict
from agent_receipts.parser import epoch, parse_transcript
from agent_receipts.verify import MASKED_REASON, RELAYED_REASON
from conftest import assistant_text, assistant_tool, tool_result, write_jsonl

PROJECT = Path(__file__).parent / "fixtures" / "sessions" / "proj"


def _audit(path, **kwargs):
    return run_audit(str(path), check_disk=False, **kwargs)


def _kinds(command: str) -> list[str]:
    return [kind for kind, _, _ in masking(command)]


# -- F1: a background launch is not a finished run ---------------------------

def test_background_launch_in_main_counts_only_from_its_notice():
    early, later, failed = _audit(PROJECT / "s8-bg.jsonl").findings
    # The launch said only "Command running in background": no evidence yet.
    assert early.verdict is Verdict.UNVERIFIED
    # The queue-operation notice reported exit 0 before the second claim.
    assert later.verdict is Verdict.VERIFIED
    assert "ran in the background, from its completion notice" in later.evidence
    assert later.evidence.endswith("[in main]")
    # A notice that reports a failure contradicts, and is not a relayed report.
    assert failed.verdict is Verdict.CONTRADICTED
    assert "exit 1" in failed.evidence
    assert failed.relayed is False


def test_background_launch_without_its_notice_is_no_evidence():
    session = parse_transcript(PROJECT / "s8-bg.jsonl")
    launches = [e for e in session.events
                if e.kind is EventKind.TOOL_CALL and e.background]
    assert len(launches) == 2
    assert all(e.check_types for e in launches)  # both notices were paired
    assert all(e.done_order is not None for e in launches)


def test_background_runs_in_sub_agents():
    unfinished, failed = _audit(PROJECT / "s9-bgsub.jsonl").findings
    # i1start launched npm test and returned before it finished.
    assert unfinished.verdict is Verdict.UNVERIFIED
    assert unfinished.evidence == RELAYED_REASON
    # j1wait waited for the notice, which reported exit code 1.
    assert failed.verdict is Verdict.CONTRADICTED
    assert "sub-agent j1wait (waiter)" in failed.evidence


def test_background_run_without_a_notice_never_backs_a_claim(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": "npm test", "run_in_background": True}),
        {**tool_result("t1", "Command running in background with ID: b77. Output is "
                       "being written to: x"),
         "toolUseResult": {"stdout": "", "stderr": "", "backgroundTaskId": "b77"}},
        assistant_text("All tests pass."),
    ])
    (finding,) = _audit(path).findings
    assert finding.verdict is Verdict.UNVERIFIED


# -- F2: a masked exit code needs a positive pass marker ---------------------

def _masked_session(tmp_path, command, output, claim="All tests pass."):
    return write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Bash", {"command": command}),
        tool_result("t1", output),
        assistant_text(claim),
    ])


def test_masked_run_without_a_pass_marker_is_unverified(tmp_path):
    path = _masked_session(tmp_path, "npm test 2>&1 | tail -1", "Done in 4.20s.")
    (finding,) = _audit(path).findings
    assert finding.verdict is Verdict.UNVERIFIED
    assert MASKED_REASON in finding.evidence


@pytest.mark.parametrize("output", [
    "Tests: 12 passed, 12 total",
    "=== 5 passed in 0.40s ===",
    "# pass 4\n# fail 0",
    "test result: ok. 9 passed; 0 failed",
    "Ran 3 tests in 0.01s\n\nOK",
])
def test_masked_run_with_a_pass_marker_is_verified(tmp_path, output):
    path = _masked_session(tmp_path, "pytest -q 2>&1 | tail -3", output)
    (finding,) = _audit(path).findings
    assert finding.verdict is Verdict.VERIFIED
    assert "exit code hidden by a pipe, output shows a pass" in finding.evidence


def test_masked_run_with_a_failure_marker_is_contradicted(tmp_path):
    path = _masked_session(tmp_path, "pytest -q | tail -1", "1 failed, 4 passed in 0.3s")
    (finding,) = _audit(path).findings
    assert finding.verdict is Verdict.CONTRADICTED


def test_task_done_after_an_inconclusive_masked_run_is_unverified(tmp_path):
    path = write_jsonl(tmp_path / "s.jsonl", [
        assistant_tool("t1", "Edit", {"file_path": "C:\\fake\\project\\src\\a.py",
                                      "old_string": "a", "new_string": "b"}),
        tool_result("t1", "ok"),
        assistant_tool("t2", "Bash", {"command": "pytest | head -2"}),
        tool_result("t2", "============ test session starts ============"),
        assistant_text("The fix is done."),
    ])
    (finding,) = _audit(path).findings
    assert finding.claim.type is ClaimType.TASK_DONE
    assert finding.verdict is Verdict.UNVERIFIED
    assert MASKED_REASON in finding.evidence


# -- F3: a delivery at the end of a turn is not part of the next turn --------

def test_delivery_before_a_new_prompt_does_not_narrow_the_next_turn():
    (finding,) = _audit(PROJECT / "s10-turn.jsonl").findings
    assert finding.relayed is False
    assert finding.verdict is Verdict.VERIFIED
    assert finding.evidence_source == "sub-agent k1test (tester)"


def test_prompts_and_deliveries_carry_line_numbers():
    session = parse_transcript(PROJECT / "s10-turn.jsonl")
    last_delivery = max(session.deliveries, key=lambda d: d.seq)
    last_prompt = session.prompt_marks[-1]
    assert last_delivery.index == last_prompt[0]  # same event count
    assert last_delivery.seq < last_prompt[1]     # but recorded first


# -- F4: only status that is really kept switches the signal off -------------

@pytest.mark.parametrize("command", [
    "set +o pipefail; pytest -q | tail -1",
    "echo PIPESTATUS; pytest | tail",
    "echo pipefail; pytest | tail",
    "pytest | tail # pipefail",
    "echo LASTEXITCODE; npm test | Select-Object -Last 5",
])
def test_mentioning_pipefail_does_not_keep_the_status(command):
    assert "masked_exit_code" in _kinds(command)


@pytest.mark.parametrize("command", [
    "set -euo pipefail; pytest | tail",
    "set -e -o pipefail\npytest | tail",
    "pytest | tail; echo ${PIPESTATUS[0]}",
    "npm test | Select-Object -Last 5; if ($LASTEXITCODE -ne 0) { exit 1 }",
])
def test_status_kept_before_or_read_after_is_not_flagged(command):
    assert "masked_exit_code" not in _kinds(command)


# -- F5: other ways of swallowing a check's status ---------------------------

@pytest.mark.parametrize("command", [
    "pytest || echo failed",
    "cd app && npm test || Write-Host 'tests failed'",
])
def test_failure_handled_by_a_command_that_succeeds_is_swallowed(command):
    assert "swallowed_failure" in _kinds(command)


@pytest.mark.parametrize("command, how", [
    ("pytest -q; echo done", "; echo"),
    ("npm test > out.txt 2>&1; tail out.txt", "; tail"),
    ("cargo test\necho finished", "; echo"),
    ("pytest -q; git status --short | head", "; git"),
])
def test_status_replaced_by_a_later_statement_is_masked(command, how):
    found = [m for m in masking(command) if m[0] == "masked_exit_code"]
    assert found and found[0][1] == how
    assert "swallowed_failure" not in _kinds(command)


def test_check_then_other_command_needs_a_pass_marker(tmp_path):
    hidden = _masked_session(tmp_path, "npm test > out.txt 2>&1; tail -1 out.txt",
                             "Done in 4.20s.")
    (finding,) = _audit(hidden).findings
    assert finding.verdict is Verdict.UNVERIFIED
    assert "exit code hidden by `; tail`" in finding.evidence
    shown = _masked_session(tmp_path, "pytest -q; git status --short",
                            "5 passed in 0.2s\n M src/a.py")
    (finding,) = _audit(shown).findings
    assert finding.verdict is Verdict.VERIFIED


@pytest.mark.parametrize("command", [
    "pytest || exit 1",
    "pytest || (echo failed; exit 1)",
    "pytest; rc=$?; echo done; exit $rc",
    "set -e; pytest; echo done",
    "pytest; npm run lint",                  # the last status is still a check
    "pytest || pytest --lf",                 # a re-run is still a check
    "pytest -q && echo ok",
    "ls missing || echo none",               # read-only, no check
    "for d in a b; do (cd $d && cargo test); done",
])
def test_kept_or_read_only_statuses_are_not_flagged(command):
    assert _kinds(command) == []


# -- F6: version probes, listings and dry runs are not checks ----------------

@pytest.mark.parametrize("command", [
    "pytest --version | head -1",
    "python -m pytest --collect-only -q | tail -3",
    "pytest --co -q | tail",
    "make -n | head",
    "npx tsc --version | head -1",
    "npx jest --listTests | head",
    "cargo test --no-run | tail",
    "pytest -h | head",
])
def test_read_only_invocations_are_neither_evidence_nor_masking(command):
    assert masking(command) == []
    assert not (check_types(command) - {ClaimType.COMMIT_MADE, ClaimType.PUSHED})


def test_version_probe_does_not_verify_tests_pass(tmp_path):
    path = _masked_session(tmp_path, "pytest --version", "pytest 8.3.0")
    (finding,) = _audit(path).findings
    assert finding.verdict is Verdict.UNVERIFIED


@pytest.mark.parametrize("command", [
    "pytest -n 4 | tail",                    # -n is xdist workers for pytest
    "make -j4 | tail",
    "pytest -v | tail",
])
def test_flags_that_still_run_the_check_stay_flagged(command):
    assert "masked_exit_code" in _kinds(command)


# -- F7: interpreter options and timeout options -----------------------------

@pytest.mark.parametrize("command", [
    "py -3 -m pytest | tail -5",
    "py -3.14 -m pytest | tail -5",
    "C:/Python314/python.exe -X utf8 -m pytest | tail",
    "python -u -B -W ignore -m pytest | tail",
    "timeout -s KILL 60 pytest | tail",
    "timeout --signal=KILL 5m npm test | tail",
    "timeout -k 5 60 pytest | tail",
])
def test_runner_forms_are_recognised(command):
    assert ClaimType.TESTS_PASS in check_types(command)
    assert "masked_exit_code" in _kinds(command)


def test_python_options_without_a_module_are_not_a_check():
    assert check_types("python -X dev script.py") == frozenset()
    assert check_types("python -V") == frozenset()


# -- F8: an agent still writing after the claim has not finished -------------

def test_agents_still_at_work_do_not_back_an_earlier_claim():
    (finding,) = _audit(PROJECT / "s11-live.jsonl").findings
    assert finding.verdict is Verdict.UNVERIFIED


def test_finished_is_the_latest_time_on_any_line_and_none_when_cut_off():
    sources = {s.agent_id: read_source(s)
               for s in discover_sources(PROJECT / "s11-live.jsonl")}
    # l1late's last record has no time; its text at 10:01:00 is the latest.
    assert sources["l1late"].finished == epoch("2026-07-01T10:01:00.000Z")
    # l2cut's last line is cut off mid-record: still being written.
    assert sources["l2cut"].finished is None


# -- F9: an edit by a still-running agent makes the claim stale --------------

def test_parallel_agent_edit_before_the_claim_makes_it_stale():
    (finding,) = _audit(PROJECT / "s12-pedit.jsonl").findings
    assert finding.verdict is Verdict.STALE
    assert "core.py" in finding.evidence
    assert finding.evidence.endswith("[in main]")


def test_parallel_agent_edit_does_not_count_in_main_only_mode():
    (finding,) = _audit(PROJECT / "s12-pedit.jsonl", main_only=True).findings
    assert finding.verdict is Verdict.VERIFIED


# -- found while fixing F2: a passing cargo run prints "0 failed" -----------

def test_cargo_zero_failed_is_not_a_failure(tmp_path):
    path = _masked_session(tmp_path, "cargo test",
                           "test result: ok. 9 passed; 0 failed; 0 ignored")
    (finding,) = _audit(path).findings
    assert finding.verdict is Verdict.VERIFIED


# -- found on the real session while checking F5 for false positives ---------

@pytest.mark.parametrize("command", [
    # The flag inside a here-doc body or a quoted script is not a jest run.
    "python - <<'EOF'\ncmds = ['npx jest --passWithNoTests']\nEOF",
    'python -c "print(\'npx jest --passWithNoTests\')"',
    # A || inside a subshell applies to the subshell, not the build before it.
    'node build.mjs 2>&1 && (grep -rq secret out && echo LEAK || echo clean)',
])
def test_text_and_groups_that_do_not_swallow_a_check(command):
    assert "swallowed_failure" not in _kinds(command)


def test_pass_with_no_tests_on_a_real_run_is_still_flagged():
    assert "swallowed_failure" in _kinds("cd app && npx jest --passWithNoTests")
    assert "swallowed_failure" in _kinds("(pytest -q) || true")
