import json

from agent_receipts.cli import main, run_audit


def test_audit_json_output(honest_transcript, capsys):
    code = main(["audit", honest_transcript, "--json", "--no-disk-check"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["score"] is not None
    assert payload["counts"]["verified"] >= 1


def test_fail_under_gates_low_scores(liar_transcript, capsys):
    assert main(["audit", liar_transcript, "--no-disk-check",
                 "--fail-under", "80", "--no-color"]) == 1


def test_fail_under_passes_high_scores(honest_transcript, capsys):
    assert main(["audit", honest_transcript, "--no-disk-check",
                 "--fail-under", "80", "--no-color"]) == 0


def test_missing_target_exits_2(capsys):
    assert main(["audit", "does-not-exist-xyz"]) == 2


def test_scores_rank_sessions_sensibly(honest_transcript, liar_transcript,
                                       gamer_transcript):
    honest = run_audit(honest_transcript, check_disk=False)
    liar = run_audit(liar_transcript, check_disk=False)
    gamer = run_audit(gamer_transcript, check_disk=False)
    assert honest.score > liar.score
    assert honest.score > gamer.score
    assert honest.grade == "A"


def test_markdown_report(honest_transcript, tmp_path, capsys):
    out = tmp_path / "report.md"
    assert main(["audit", honest_transcript, "--no-disk-check",
                 "--md", str(out), "--no-color"]) == 0
    content = out.read_text(encoding="utf-8")
    assert "agent-receipts audit" in content
    assert "VERIFIED" in content
