from agent_receipts.models import EventKind
from agent_receipts.parser import parse_transcript


def test_parses_events_in_order(honest_transcript):
    session = parse_transcript(honest_transcript)
    kinds = [e.kind for e in session.events]
    assert kinds == [EventKind.TEXT, EventKind.TOOL_CALL,
                     EventKind.TOOL_CALL, EventKind.TEXT]
    assert session.session_id == "fixture-session"
    assert session.cwd == "C:\\fake\\project"


def test_tool_results_attached(honest_transcript):
    session = parse_transcript(honest_transcript)
    test_run = session.tool_calls()[1]
    assert test_run.tool_name == "Bash"
    assert "12 passed" in test_run.output
    assert test_run.is_error is False
    assert test_run.exit_code == 0


def test_error_results_carry_exit_code(liar_transcript):
    session = parse_transcript(liar_transcript)
    failing_run = [e for e in session.tool_calls() if e.command][0]
    assert failing_run.is_error is True
    assert failing_run.exit_code == 1


def test_garbage_lines_skipped(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('not json\n{"type":"system"}\n', encoding="utf-8")
    session = parse_transcript(path)
    assert session.events == []
