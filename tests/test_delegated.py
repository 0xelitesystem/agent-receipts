"""v0.2: evidence from sub-agent and workflow transcripts (requirements 1, 2, 5)."""

from __future__ import annotations

import filecmp
import json
import socket
import sys
from pathlib import Path

import pytest

from agent_receipts.cli import main, run_audit
from agent_receipts.delegated import discover_sources, session_dir
from agent_receipts.models import ClaimType, Verdict
from agent_receipts.parser import epoch
from agent_receipts.verify import RELAYED_REASON

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT = FIXTURES / "sessions" / "fixture-project"

AGENT_SYNC = PROJECT / "11111111-0000-4000-8000-000000000001.jsonl"
WORKFLOW = PROJECT / "22222222-0000-4000-8000-000000000002.jsonl"
ORDERING = PROJECT / "33333333-0000-4000-8000-000000000003.jsonl"
RELAY_ONLY = PROJECT / "44444444-0000-4000-8000-000000000004.jsonl"
CONTRADICTED = PROJECT / "55555555-0000-4000-8000-000000000005.jsonl"
STALE = PROJECT / "66666666-0000-4000-8000-000000000006.jsonl"
SECRETS = PROJECT / "77777777-0000-4000-8000-000000000007.jsonl"


def _audit(path, **kwargs):
    return run_audit(str(path), check_disk=False, **kwargs)


def _only(result, claim_type=ClaimType.TESTS_PASS):
    found = [f for f in result.findings if f.claim.type is claim_type]
    assert len(found) == 1, found
    return found[0]


# -- requirement 1: discovery, evidence from anywhere, honest ordering -------

def test_sub_agent_test_run_backs_the_claim():
    finding = _only(_audit(AGENT_SYNC))
    assert finding.verdict is Verdict.VERIFIED
    assert finding.evidence_source == "sub-agent a1sync (fix parser)"
    assert "[in sub-agent a1sync (fix parser)]" in finding.evidence
    assert finding.evidence_index is None  # not a main-session event


def test_main_only_keeps_v01_behaviour():
    result = _audit(AGENT_SYNC, main_only=True)
    assert result.session.sources == []
    assert result.session.main_only is True
    assert _only(result).verdict is Verdict.UNVERIFIED


def test_main_only_flag_on_the_cli(capsys):
    assert main(["audit", str(AGENT_SYNC), "--main-only", "--json",
                 "--no-disk-check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"]["main_only"] is True
    assert payload["counts"]["unverified"] == 1


def test_workflow_agent_runs_back_relayed_claims():
    result = _audit(WORKFLOW)
    where = "workflow wf_fixture-001, agent b1build (build and test)"
    for claim_type in (ClaimType.TESTS_PASS, ClaimType.BUILD_OK):
        finding = _only(result, claim_type)
        assert finding.verdict is Verdict.VERIFIED
        assert finding.evidence_source == where
        assert finding.relayed is True
    assert result.session.scan.workflow_agents == 1


def test_claim_before_the_delegated_run_finished_is_unverified():
    first, second = _audit(ORDERING).findings
    # c1late ran its tests after the first claim; d1busy ran them before it
    # but was still working, so neither may back "All tests pass."
    assert first.verdict is Verdict.UNVERIFIED
    assert first.evidence_source == ""
    # after c1late's notification, its run backs the second claim; d1busy
    # was not delivered in that turn, so it is not consulted
    assert second.verdict is Verdict.VERIFIED
    assert second.evidence_source.startswith("sub-agent c1late")


def test_failed_delegated_run_contradicts():
    finding = _only(_audit(CONTRADICTED))
    assert finding.verdict is Verdict.CONTRADICTED
    assert "exit 1" in finding.evidence
    assert "workflow wf_fixture-005, agent f1fail" in finding.evidence


def test_delegated_edit_after_the_run_makes_the_claim_stale():
    finding = _only(_audit(STALE))
    assert finding.verdict is Verdict.STALE
    assert "index.js" in finding.evidence


def test_timestamps_compare_across_offsets():
    assert epoch("2026-07-01T10:00:00Z") == epoch("2026-07-01T12:00:00+02:00")
    assert epoch("2026-07-01T10:00:00.500Z") - epoch("2026-07-01T10:00:00Z") == 0.5
    assert epoch("not a time") is None
    assert epoch("") is None


def test_main_without_timestamps_never_uses_delegated_evidence(tmp_path):
    project = tmp_path / "proj"
    session = project / "s1"
    sub = session / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x1.jsonl").write_text(json.dumps({
        "type": "assistant", "timestamp": "2026-07-01T10:00:00Z",
        "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                 "input": {"command": "pytest"}}]}}) + "\n" + json.dumps({
        "type": "user", "timestamp": "2026-07-01T10:00:01Z",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                 "content": "3 passed"}]}}) + "\n", encoding="utf-8")
    main_file = project / "s1.jsonl"
    main_file.write_text(json.dumps({
        "type": "assistant", "message": {"content": [
            {"type": "text", "text": "All tests pass."}]}}) + "\n", encoding="utf-8")
    finding = _only(_audit(main_file))
    assert finding.verdict is Verdict.UNVERIFIED


# -- requirement 2: relayed reports ------------------------------------------

def test_relayed_report_without_a_command_is_unverified_with_its_own_reason():
    finding = _only(_audit(RELAY_ONLY))
    assert finding.verdict is Verdict.UNVERIFIED
    assert finding.evidence == RELAYED_REASON
    assert finding.relayed is True


def test_relay_reason_also_applies_in_main_only_mode():
    finding = _only(_audit(AGENT_SYNC, main_only=True))
    assert finding.evidence == RELAYED_REASON


def test_relay_wording_alone_marks_a_claim_relayed(tmp_path):
    from conftest import assistant_text, write_jsonl
    path = write_jsonl(tmp_path / "s.jsonl", [
        assistant_text("According to the workflow, all tests pass."),
        assistant_text("All tests pass."),
    ])
    relayed, plain = _audit(path).findings
    assert relayed.evidence == RELAYED_REASON
    assert plain.evidence != RELAYED_REASON


# -- discovery details -------------------------------------------------------

def test_discovery_layout_and_variants(tmp_path):
    main_file = tmp_path / "proj" / "abc.jsonl"
    main_file.parent.mkdir(parents=True)
    main_file.write_text("", encoding="utf-8")
    sub = session_dir(main_file) / "subagents"
    run = sub / "workflows" / "wf_run-1"
    run.mkdir(parents=True)
    for name in ("agent-a1.jsonl", "agent-a2.jsonl.superseded-1",
                 ".orphaned-agent-a3.jsonl", "agent-a4.jsonl.zst",
                 "agent-a1.meta.json", "notes.jsonl"):
        (sub / name).write_text("", encoding="utf-8")
    for name in ("agent-b1.jsonl", "journal.jsonl"):
        (run / name).write_text("", encoding="utf-8")
    (sub / "workflows" / "not-a-run").mkdir()
    (sub / "workflows" / "not-a-run" / "agent-z9.jsonl").write_text("", encoding="utf-8")
    found = {(s.kind, s.agent_id, s.run_id) for s in discover_sources(main_file)}
    assert found == {
        ("subagent", "a1", ""), ("subagent", "a2", ""), ("subagent", "a3", ""),
        ("workflow_agent", "b1", "wf_run-1"),
    }


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_discovery_does_not_follow_symlinked_folders(tmp_path):
    main_file = tmp_path / "proj" / "abc.jsonl"
    main_file.parent.mkdir(parents=True)
    main_file.write_text("", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "agent-e1.jsonl").write_text("", encoding="utf-8")
    workflows = session_dir(main_file) / "subagents" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "wf_link").symlink_to(elsewhere, target_is_directory=True)
    assert discover_sources(main_file) == []


def test_fixtures_match_their_generator(tmp_path, monkeypatch):
    sys.path.insert(0, str(FIXTURES))
    try:
        import make_fixtures
    finally:
        sys.path.remove(str(FIXTURES))
    monkeypatch.setattr(make_fixtures, "ROOT", tmp_path)
    make_fixtures.main()
    generated = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    committed = sorted(p.relative_to(PROJECT) for p in PROJECT.rglob("*") if p.is_file())
    assert generated == committed
    for rel in generated:
        assert filecmp.cmp(tmp_path / rel, PROJECT / rel, shallow=False), rel


# -- requirement 5: privacy --------------------------------------------------

_FIXTURE_SECRETS = ("ghp_FIXTUREFIXTUREFIXTUREFIXTURE0123", "sk-proj-FIXTURE0123456789abcdef")


def test_secrets_in_delegated_transcripts_are_redacted_everywhere(tmp_path, capsys):
    md = tmp_path / "r.md"
    assert main(["audit", str(SECRETS), "--no-disk-check", "--no-color",
                 "--md", str(md)]) == 0
    terminal = capsys.readouterr().out
    assert main(["audit", str(SECRETS), "--no-disk-check", "--json"]) == 0
    payload = capsys.readouterr().out
    report = md.read_text(encoding="utf-8")
    for text in (terminal, payload, report):
        for secret in _FIXTURE_SECRETS:
            assert secret not in text
    data = json.loads(payload)
    assert "GITHUB_TOKEN=*** npm test || true" in data["claims"][0]["evidence"]
    assert data["gaming_signals"][0]["source"] == "sub-agent h1secret (uses api_key=***)"


def test_audit_with_delegated_transcripts_stays_offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    for path in PROJECT.glob("*.jsonl"):
        run_audit(str(path), check_disk=True)


def test_json_report_names_sources(capsys):
    assert main(["audit", str(WORKFLOW), "--json", "--no-disk-check"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["version"] == "0.2.0"
    assert data["scope"]["workflow_agent_transcripts"] == 1
    assert {c["evidence_source"] for c in data["claims"]} == {
        "workflow wf_fixture-001, agent b1build (build and test)"}
    assert data["gaming_signals"][0]["kind"] == "masked_exit_code"
    assert data["gaming_signals"][0]["event"] is None
