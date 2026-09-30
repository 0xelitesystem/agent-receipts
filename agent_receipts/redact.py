"""Mask credential values in command text before it goes into a report.

Reports are made to be shared (Markdown, JSON, CI logs), and the commands
quoted as evidence often carry credentials: a password in a DATABASE_URL,
a token in a git remote, an API key in an env assignment. The command
stays recognisable; only the secret value becomes ***. Masking is
pattern based: a credential in a shape not listed below is not masked.

What is masked:

* The value of a credential name in NAME=value, NAME: value,
  "name": "value", --name=value or --name value. The name is split into
  lowercase words on non-alphanumerics and on camelCase (accessToken is
  access + token). It is a credential name when a word is token, secret,
  password, passwd, passphrase, credential, credentials, auth,
  authorization, apikey or privatekey; when "key" comes with api, access,
  private, secret, encryption, signing, client, master or session; or
  when the whole name is pwd. Any non-empty value is masked except none,
  null, true, false and plain numbers, so --max-token=4096 stays.
* The value of a name whose only credential word is a bare "key" (--key,
  CACHE_KEY), but only when it is 16 or more of [A-Za-z0-9_-+/=].
* In a header shape (NAME: value, "name": value) a scheme word (bearer,
  basic, token or bot, any case) is not the secret: it stays, and the
  value after it is masked, quoted or not, even when it is a number or
  the name is a bare "key". "X-Api-Key: Basic dXNl" becomes
  "X-Api-Key: Basic ***". In any shape, a quoted value that is a scheme
  word and one token ("X-Key": "Bearer abc") makes a bare "key" name a
  credential too.
* A value run that ends in "=" and is followed by a closed, quoted word
  with no blanks (Token token="abc", or with escaped quotes) takes the
  quoted word along, so no part of the parameter stays.
* Authorization header values, whatever their length, and a bare
  "Bearer <token>" of 16 or more characters, quoted or not.
* Tokens with a known prefix (sk-, ghp_, AKIA, ...) followed by 12 or
  more token characters. The prefix is kept.
* The password in scheme://user:password@host.

$NAME and ${NAME} are variable references, not assignments, and a value
that starts with $ is a reference too; both stay readable.

Every pattern is anchored on a literal or on the start of a word run,
the assignment scanner only moves forward, a value run is measured
once however many names point into it, and a parameter's quoted word
is read only when the value is masked and the scan moves past it, so
the cost stays linear in the length of the command.
"""

from __future__ import annotations

import re

_MASK = "***"

# scheme://user:password@host  ->  scheme://user:***@host
_URL_CREDENTIALS = re.compile(r"(://[^\s/:@]*:)[^\s/@]+@")

# An Authorization header value is always masked, whatever its length
# ("Authorization: Bearer x" -> "Authorization: Bearer ***"). A bare
# "Bearer <token>" needs 16 token characters, so prose such as "Bearer
# auth" stays readable; a quote before the token (Bearer "...") is kept.
# $VARS are references, not secrets, and so is ${AUTHORIZATION:-x}. A
# quoted header value (Authorization: Bearer "abc", or \"abc\" inside a
# double-quoted shell word) is left to the assignment scanner, which
# masks the whole quoted string. The value of a name="value" parameter
# (Authorization: Token token="abc") is masked with its quoted word.
_SCHEME = r"(?:bearer|basic|token|bot)"
# The quoted word of a name="value" parameter: closed, with no blank or
# shell operator in it, so the closing quote of a shell word after a
# base64 "=" ("X-Api-Key: dXNl=" https://x) is not mistaken for one.
_PARAM_WORD = r"(?P<esc>\\?)(?P<pq>[\"'])[^\s\"'\\;&|<>(),]+(?P=esc)(?P=pq)"
_PARAM_TAIL = re.compile(_PARAM_WORD)
_AUTH_HEADER = re.compile(
    r"(?P<header>(?<!\$)(?<!\$\{)\b(?i:authorization)\s*:\s*"
    r"(?:(?i:" + _SCHEME + r")\s+)?)"
    r"(?!\*\*\*|\$|(?i:" + _SCHEME + r")\s)"
    r"(?:[^\s\"',;\\]|\\(?![\"']))+(?:(?<==)" + _PARAM_WORD + r")?"
    r"|(?P<bearer>\bBearer\s+(?:\\?[\"'])?)(?!\*\*\*)[A-Za-z0-9\-._~+/]{16,}=*"
)
_SCHEME_WORD = re.compile(_SCHEME, re.IGNORECASE)
# A scheme word and the blanks after it, at the start of a header value.
_SCHEME_GAP = re.compile(_SCHEME + r"[ \t]+", re.IGNORECASE)
# A whole quoted value that is a scheme word and one token, not a $VAR.
_SCHEME_VALUE = re.compile(_SCHEME + r"[ \t]+[^\s$]\S*", re.IGNORECASE)

# Tokens recognisable by prefix, masked only with 12 or more token
# characters after the prefix, so a branch like feature/sk-login stays
# readable. The prefix is kept so the reader still sees what kind of
# credential it was.
_TOKEN_PREFIX = re.compile(
    r"\b(sk_live_|sk-|rk_|gh[pousr]_|github_pat_|glpat-|xox[abprs]-"
    r"|AIza|AKIA|ASIA)[A-Za-z0-9_\-]{12,}"
)

# A name and its separator: NAME=value, NAME: value, "name": "value"
# (also with the quotes escaped, as JSON inside a shell string),
# --name=value or --name value. A name straight after $ or ${ is a
# variable reference ($PWD:/app, ${TOKEN:-x}) and never matches.
_CANDIDATE = re.compile(
    r"(?<![\w.$-])(?<!\$\{)"
    r"(?:(?P<q>\\?[\"'])(?P<qname>[\w.-]+)(?P=q)[ \t]*[=:]"
    r"|(?P<name>[\w.-]+)[ \t]*(?P<sep>[=:])"
    r"|(?P<flag>--[\w.-]+)(?=[ \t]))"
    r"(?P<gap>[ \t]*)"
)
# The value after it: quoted (an unclosed quote runs to the end), or a
# run of characters that cannot end a shell word.
_QUOTED = re.compile(r"\"[^\"]*\"?|'[^']*'?")
_BARE = re.compile(r"[^\s\"'&;|,<>]+")
# After "NAME: value" the value must end the line, the quoted string or
# the shell word list; "auth: fix login redirect" is prose, not a value.
_VALUE_END = re.compile(r"[ \t]*(?:[\r\n\"'`;&|,)}\]<>]|\Z)")
# none, null, true, false, 0 and plain numbers are settings, not secrets.
_PLAIN = re.compile(r"(?i:none|null|true|false)|-?[0-9]+(?:\.[0-9]+)?")
# What a bare "key" value must look like to be masked.
_SHAPED_CHARS = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-+/="
)
_SHAPED = re.compile(r"[A-Za-z0-9_\-+/=]{16,}")

# Name words: split on non-alphanumerics and camelCase (APIKey is api + key).
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_CREDENTIAL_WORDS = frozenset({
    "token", "secret", "password", "passwd", "passphrase", "credential",
    "credentials", "auth", "authorization", "apikey", "privatekey",
})
_KEY_QUALIFIERS = frozenset({
    "api", "access", "private", "secret", "encryption", "signing",
    "client", "master", "session",
})
# An all-caps name glues its words together (PGPASSWORD, NGROK_AUTHTOKEN,
# AWS_ACCESSKEY), so a word that ends in a credential word, or in a
# qualifier plus "key", counts as that word. "auth" is left out: four
# letters are too little to go on inside another word.
_GLUED_ENDINGS = tuple(sorted(
    (_CREDENTIAL_WORDS - {"auth"}) | {q + "key" for q in _KEY_QUALIFIERS}))
_CREDENTIAL, _BARE_KEY = "credential", "bare key"


def _classify(name: str) -> str | None:
    words = [w.lower() for w in _WORD.findall(name)]
    if words == ["pwd"]:
        return _CREDENTIAL
    if any(w in _CREDENTIAL_WORDS or w.endswith(_GLUED_ENDINGS) for w in words):
        return _CREDENTIAL
    if "key" in words:
        return _CREDENTIAL if _KEY_QUALIFIERS.intersection(words) else _BARE_KEY
    return None


def _param_end(text: str, end: int) -> int:
    """End of the quoted word of a name="value" parameter, or -1.

    `end` closes a value run. When the run ends in "=" (or in "=\\" before
    an escaped quote) and _PARAM_TAIL follows, the quoted word belongs to
    the value: Token token="abc" is masked to Token ***.
    """
    at = end - 1 if text[end - 1:end] == "\\" else end
    if text[at - 1:at] != "=":
        return -1
    tail = _PARAM_TAIL.match(text, at)
    return tail.end() if tail else -1


def _redact_assignments(text: str) -> str:
    out: list[str] = []
    copied = pos = 0
    # The last bare value run measured, and where its shaped tail starts.
    # Every later name inside that run reuses it instead of rescanning.
    run_start = run_end = run_shaped = -1
    while True:
        match = _CANDIDATE.search(text, pos)
        if match is None:
            break
        # Resume right after the separator unless the value gets masked,
        # so a credential inside a longer value (a URL's ?password=) is
        # still found.
        pos = start = match.end()
        kind = _classify(match.group("qname") or match.group("name")
                         or match.group("flag"))
        if kind is None:
            continue
        # In a header ("X-Api-Key: Basic dXNl", "Token: bearer abc") the
        # scheme word is not the secret; the value after it is. Only the
        # colon shape counts: in a shell, TOKEN=bearer abc and --auth
        # bearer abc give the name the word "bearer" and nothing more.
        scheme = (match.group("flag") is None
                  and text[match.start("gap") - 1] == ":"
                  and _SCHEME_GAP.match(text, start))
        if scheme:
            # "X-Key: Bearer abc": the scheme word says it is a credential,
            # so a bare "key" name needs no secret-shaped value here.
            start, kind = scheme.end(), _CREDENTIAL
        if start >= len(text):
            continue
        first = text[start]
        if first == "\\" and text[start + 1:start + 2] in ("\"", "'"):
            # \"value\" runs to the next escaped quote of the same kind.
            quote = text[start:start + 2]
            close = text.find(quote, start + 2)
            closing = quote if close >= 0 else ""
            low, high = start + 2, close if close >= 0 else len(text)
            end = high + len(closing)
        elif first in "\"'":
            quote = first
            end = _QUOTED.match(text, start).end()
            closing = first if end - start > 1 and text[end - 1] == first else ""
            low, high = start + 1, end - len(closing)
        else:
            quote = ""
        if quote:
            # "X-Key": "Bearer abc": a scheme word and a token say it is
            # a credential, as they do outside the quotes.
            if kind == _BARE_KEY and _SCHEME_VALUE.fullmatch(text, low, high):
                kind = _CREDENTIAL
            if (high <= low or text.startswith(_MASK, low)
                    or (not scheme and _PLAIN.fullmatch(text, low, high))
                    or (kind == _BARE_KEY
                        and not _SHAPED.fullmatch(text, low, high))):
                continue
            masked = quote + _MASK + closing
        else:
            # $VAR is a reference; == and :: are not separators; a --flag
            # followed by another option has no value; "name": {...} is a
            # nested object whose own names are checked next.
            if (first in "$=:" or (match.group("flag") and first == "-")
                    or (match.group("q") and first in "{[")):
                continue
            if not run_start <= start < run_end:
                bare = _BARE.match(text, start)
                if bare is None:
                    continue
                run_start, run_end = start, bare.end()
                segment = text[run_start:run_end]
                run_shaped = run_end - (len(segment)
                                        - len(segment.rstrip(_SHAPED_CHARS)))
            end = run_end
            if (text.startswith(_MASK, start)
                    or (not scheme and _PLAIN.fullmatch(text, start, end))
                    or (kind == _BARE_KEY
                        and not (start >= run_shaped and end - start >= 16))):
                continue
            # A scheme word on its own is not a secret: TOKEN=bearer in a
            # shell, or a header scheme whose value is on the next line.
            if (_SCHEME_WORD.fullmatch(text, start, end)
                    and text[end:end + 1].isspace()):
                continue
            if ((scheme or (match.group("sep") == ":" and match.group("gap")))
                    and not _VALUE_END.match(text, end)):
                continue
            # --with-token ghp_... keeps its prefix via _TOKEN_PREFIX.
            if match.group("flag") and _TOKEN_PREFIX.fullmatch(text, start, end):
                continue
            # Token token="abc": the quoted word of the parameter goes too.
            tail = _param_end(text, end)
            if tail >= 0:
                end = tail
            masked = _MASK
        out.append(text[copied:start])
        out.append(masked)
        copied = pos = end
    out.append(text[copied:])
    return "".join(out)


def _mask_header(match: re.Match) -> str:
    return (match.group("header") or match.group("bearer")) + _MASK


def _mask_token(match: re.Match) -> str:
    return match.group(1) + _MASK


def redact_secrets(text: str) -> str:
    """Replace credential values in `text` with ***; leave the rest as is."""
    text = _URL_CREDENTIALS.sub(r"\1" + _MASK + "@", text)
    text = _AUTH_HEADER.sub(_mask_header, text)
    text = _redact_assignments(text)
    return _TOKEN_PREFIX.sub(_mask_token, text)
