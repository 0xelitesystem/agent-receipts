from agent_receipts.gaming import detect_gaming
from agent_receipts.parser import parse_transcript


def _kinds(path):
    return {s.kind for s in detect_gaming(parse_transcript(path))}


def test_detects_all_gaming_patterns(gamer_transcript):
    kinds = _kinds(gamer_transcript)
    assert "weakened_assertions" in kinds
    assert "added_skip" in kinds
    assert "swallowed_failure" in kinds
    assert "no_verify_commit" in kinds


def test_honest_session_has_no_signals(honest_transcript):
    assert _kinds(honest_transcript) == set()


def test_non_test_file_edits_are_ignored(stale_transcript):
    # src/index.ts edit must not be treated as test tampering
    assert _kinds(stale_transcript) == set()
