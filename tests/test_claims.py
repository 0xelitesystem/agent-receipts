from agent_receipts.claims import extract_claims
from agent_receipts.models import ClaimType, Event, EventKind, Session


def _session_with_text(text: str) -> Session:
    return Session(path="x", events=[
        Event(kind=EventKind.TEXT, index=0, text=text),
    ])


def _types(text: str) -> list[ClaimType]:
    return [c.type for c in extract_claims(_session_with_text(text))]


def test_extracts_tests_pass():
    assert ClaimType.TESTS_PASS in _types("All tests pass. Ready for review.")
    assert ClaimType.TESTS_PASS in _types("The test suite passes cleanly.")


def test_negation_is_not_a_claim():
    assert _types("The tests don't pass yet.") == []
    assert _types("3 tests failed after my change.") == []


def test_future_and_conditional_are_not_claims():
    assert _types("Once the tests pass, we can merge.") == []
    assert _types("The tests should pass after this fix.") == []


def test_extracts_file_created_with_path():
    claims = extract_claims(_session_with_text("Created `src/utils/cache.py` for the new layer."))
    assert claims[0].type is ClaimType.FILE_CREATED
    assert claims[0].detail == "src/utils/cache.py"


def test_extracts_commit_and_push():
    assert ClaimType.COMMIT_MADE in _types("I committed the changes.")
    assert ClaimType.PUSHED in _types("Pushed everything to the remote.")


def test_extracts_task_done():
    assert ClaimType.TASK_DONE in _types("The fix is complete.")
    assert ClaimType.TASK_DONE in _types("Fixed the bug in the date parser.")


def test_dedupes_repeats_within_event():
    text = "All tests pass. Yes, all tests pass."
    assert _types(text).count(ClaimType.TESTS_PASS) == 1
