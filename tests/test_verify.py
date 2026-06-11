from agent_receipts.claims import extract_claims
from agent_receipts.models import ClaimType, Verdict
from agent_receipts.parser import parse_transcript
from agent_receipts.verify import verify_claims


def _audit(path):
    session = parse_transcript(path)
    return verify_claims(session, extract_claims(session), check_disk=False)


def _verdict_for(findings, claim_type):
    return [f.verdict for f in findings if f.claim.type is claim_type]


def test_honest_session_verifies(honest_transcript):
    findings = _audit(honest_transcript)
    assert Verdict.VERIFIED in _verdict_for(findings, ClaimType.TESTS_PASS)
    assert all(f.verdict is not Verdict.CONTRADICTED for f in findings)


def test_claim_without_run_is_unverified(liar_transcript):
    findings = _audit(liar_transcript)
    verdicts = _verdict_for(findings, ClaimType.TESTS_PASS)
    assert verdicts[0] is Verdict.UNVERIFIED  # claimed before any test ran


def test_claim_after_failing_run_is_contradicted(liar_transcript):
    findings = _audit(liar_transcript)
    verdicts = _verdict_for(findings, ClaimType.TESTS_PASS)
    assert verdicts[1] is Verdict.CONTRADICTED


def test_edit_after_passing_run_is_stale(stale_transcript):
    findings = _audit(stale_transcript)
    assert _verdict_for(findings, ClaimType.TESTS_PASS) == [Verdict.STALE]


def test_commit_claim_verified_by_git_command(gamer_transcript):
    findings = _audit(gamer_transcript)
    assert _verdict_for(findings, ClaimType.COMMIT_MADE) == [Verdict.VERIFIED]


def test_evidence_points_at_event(honest_transcript):
    findings = _audit(honest_transcript)
    verified = [f for f in findings if f.verdict is Verdict.VERIFIED]
    assert all(f.evidence_index is not None for f in verified)
    assert all(f.evidence for f in findings)
