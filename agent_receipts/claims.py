"""Extract checkable claims from the agent's prose.

A claim is a sentence in an assistant text block that asserts something
about the state of the world: tests pass, the build is clean, a file was
created, work was committed. Hedged, negated, future, or conditional
sentences ("once tests pass...", "tests should pass") are not claims.
"""

from __future__ import annotations

import re

from .models import Claim, ClaimType, Event, EventKind, Session

# Sentences containing these are aspirational or negative, not assertions.
_DISQUALIFIERS = re.compile(
    r"\b(?:will|would|should|could|may|might|once|if|unless|until|when|"
    r"whenever|after|let'?s|going to|need(?:s)? to|have to|to make|"
    r"don'?t|doesn'?t|didn'?t|aren'?t|isn'?t|wasn'?t|not|no longer|"
    r"fail(?:s|ed|ing)?|broken|can'?t|cannot|try|attempt)\b",
    re.IGNORECASE,
)

# Claim patterns, tried in order. FILE_CREATED captures the path.
_PATTERNS: list[tuple[ClaimType, re.Pattern]] = [
    (ClaimType.TESTS_PASS, re.compile(
        r"\b(?:all\s+)?(?:\d+\s+)?tests?\s+(?:are\s+|now\s+|still\s+)?"
        r"(?:pass(?:es|ing|ed)?|green|succeed(?:s|ed)?)\b"
        r"|\btest\s+suite\s+(?:passes|is\s+green|succeeds)\b"
        r"|\b(?:\d+)\s+passed\b.*\b0\s+failed\b",
        re.IGNORECASE,
    )),
    (ClaimType.BUILD_OK, re.compile(
        r"\bbuild\s+(?:is\s+|now\s+)?(?:pass(?:es|ing|ed)?|succeed(?:s|ed)?|"
        r"clean|green|works|working|successful)\b"
        r"|\b(?:compiles?|compiled)\s+(?:cleanly|successfully|without\s+errors?)\b",
        re.IGNORECASE,
    )),
    (ClaimType.LINT_OK, re.compile(
        r"\blint(?:er|ing)?\s+(?:is\s+|now\s+)?(?:pass(?:es|ing|ed)?|clean|green)\b"
        r"|\bno\s+lint(?:er|ing)?\s+(?:errors?|warnings?|issues?)\b",
        re.IGNORECASE,
    )),
    (ClaimType.TYPECHECK_OK, re.compile(
        r"\btype[\s-]?check(?:s|ing|er)?\s+(?:is\s+|now\s+)?(?:pass(?:es|ing|ed)?|clean|green)\b"
        r"|\bno\s+type\s+errors?\b"
        r"|\b(?:mypy|pyright|tsc)\s+(?:is\s+|now\s+)?(?:passes|clean|green|happy)\b",
        re.IGNORECASE,
    )),
    (ClaimType.FILE_CREATED, re.compile(
        r"\bcreated\s+(?:the\s+(?:file\s+)?|a\s+(?:new\s+)?(?:file\s+)?|file\s+)?"
        r"`([^`\s]+\.[A-Za-z0-9]{1,10})`",
        re.IGNORECASE,
    )),
    (ClaimType.COMMIT_MADE, re.compile(
        r"\bcommit(?:ted)?\s+(?:the\s+|all\s+|these\s+|those\s+)?(?:changes?|files?|work|everything|it)\b"
        r"|\bchanges?\s+(?:are\s+|have\s+been\s+|were\s+)?committed\b"
        r"|\bcreated\s+(?:a\s+|the\s+)?commit\b",
        re.IGNORECASE,
    )),
    (ClaimType.PUSHED, re.compile(
        r"\bpushed\s+(?:the\s+|to\s+|everything|changes?|it|commits?|branch)\b"
        r"|\bchanges?\s+(?:are\s+|have\s+been\s+|were\s+)?pushed\b",
        re.IGNORECASE,
    )),
    (ClaimType.TASK_DONE, re.compile(
        r"\b(?:task|fix|feature|implementation|migration|refactor)\s+"
        r"(?:is\s+)?(?:complete|done|finished|working)\b"
        r"|\b(?:everything|all)\s+(?:is\s+|now\s+)?(?:working|done|complete|in\s+place)\b"
        r"|\bbug\s+(?:is\s+)?fixed\b|\bfixed\s+the\s+(?:bug|issue|problem|error)\b",
        re.IGNORECASE,
    )),
]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
# Markdown noise we strip before matching: bold/italics, checkmarks, bullets.
_MD_NOISE = re.compile(r"[*_#>]|^\s*[-•✅✓✔☑]+\s*", re.MULTILINE)


def _sentences(text: str) -> list[str]:
    cleaned = _MD_NOISE.sub(" ", text)
    return [s.strip() for s in _SENTENCE_SPLIT.split(cleaned) if s.strip()]


def extract_claims(session: Session) -> list[Claim]:
    claims: list[Claim] = []
    for event in session.events:
        if event.kind is not EventKind.TEXT:
            continue
        for sentence in _sentences(event.text):
            if len(sentence) > 400:
                continue  # code blocks / pasted output, not prose
            if _DISQUALIFIERS.search(sentence):
                continue
            for claim_type, pattern in _PATTERNS:
                match = pattern.search(sentence)
                if not match:
                    continue
                detail = ""
                if claim_type is ClaimType.FILE_CREATED:
                    detail = match.group(1) or ""
                claims.append(Claim(
                    type=claim_type,
                    quote=sentence[:200],
                    event_index=event.index,
                    detail=detail,
                ))
                # no break: "tests pass and I committed" is two claims
    return _dedupe(claims)


def _dedupe(claims: list[Claim]) -> list[Claim]:
    """Drop repeats of the same claim type within the same text event."""
    seen: set[tuple] = set()
    unique: list[Claim] = []
    for claim in claims:
        key = (claim.type, claim.event_index, claim.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(claim)
    return unique
