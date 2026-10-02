"""Regression tests for the hostile-transcript fixes.

Each test feeds a crafted transcript through the public entry points and
checks that the audit stays offline, masks the tested credential shapes,
prints no live terminal control codes, and finishes in linear time.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import sys
import time

from agent_receipts.claims import extract_claims
from agent_receipts.cli import main, run_audit
from agent_receipts.gaming import detect_gaming
from agent_receipts.models import ClaimType, Verdict
from agent_receipts.parser import parse_transcript
from agent_receipts.report import render_markdown, render_terminal
from agent_receipts.verify import verify_claims

from conftest import assistant_text, assistant_tool, tool_result, write_jsonl


def _with_cwd(record: dict, cwd: str, slug: str = "") -> dict:
    record = dict(record)
    record["cwd"] = cwd
    if slug:
        record["slug"] = slug
    return record


# ---------------------------------------------------------------------------
# UNC paths from the transcript must never reach the filesystem
# ---------------------------------------------------------------------------

def _is_remote(value) -> bool:
    s = os.fspath(value) if not isinstance(value, int) else ""
    if isinstance(s, bytes):
        s = s.decode("utf-8", "replace")
    s = s.replace("/", "\\")
    return s.startswith("\\\\") or s.startswith("\\??\\")


def _trap_filesystem(monkeypatch) -> list[str]:
    """Record every stat-like call on a UNC path and short-circuit it."""
    probed: list[str] = []
    real_stat, real_lstat, real_exists = os.stat, os.lstat, os.path.exists
    real_path_exists = pathlib.Path.exists
    real_path_stat = pathlib.Path.stat

    def stat(path, *args, **kwargs):
        if _is_remote(path):
            probed.append(os.fspath(path))
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    def lstat(path, *args, **kwargs):
        if _is_remote(path):
            probed.append(os.fspath(path))
            raise FileNotFoundError(path)
        return real_lstat(path, *args, **kwargs)

    def exists(path):
        if _is_remote(path):
            probed.append(os.fspath(path))
            return False
        return real_exists(path)

    def path_exists(self, *args, **kwargs):
        if _is_remote(self):
            probed.append(str(self))
            return False
        return real_path_exists(self, *args, **kwargs)

    def path_stat(self, *args, **kwargs):
        if _is_remote(self):
            probed.append(str(self))
            raise FileNotFoundError(str(self))
        return real_path_stat(self, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(os.path, "exists", exists)
    monkeypatch.setattr(pathlib.Path, "exists", path_exists)
    monkeypatch.setattr(pathlib.Path, "stat", path_stat)
    return probed


def _file_claim_findings(path):
    session = parse_transcript(path)
    claims = extract_claims(session)
    return session, claims


def test_unc_cwd_is_never_probed(tmp_path, monkeypatch):
    path = write_jsonl(tmp_path / "unc-cwd.jsonl", [
        _with_cwd(assistant_tool("t1", "Write", {
            "file_path": "cache.py", "content": "x = 1"}),
            "\\\\attacker.invalid\\share"),
        tool_result("t1", "ok"),
        _with_cwd(assistant_text("I created `cache.py`."),
                  "\\\\attacker.invalid\\share"),
    ])
    session, claims = _file_claim_findings(path)
    probed = _trap_filesystem(monkeypatch)
    findings = verify_claims(session, claims, check_disk=True)
    assert probed == []
    created = [f for f in findings if f.claim.type is ClaimType.FILE_CREATED]
    assert [f.verdict for f in created] == [Verdict.VERIFIED]
    assert "not checked" in created[0].evidence


def test_unc_write_path_is_never_probed(tmp_path, monkeypatch):
    path = write_jsonl(tmp_path / "unc-write.jsonl", [
        _with_cwd(assistant_tool("t1", "Write", {
            "file_path": "\\\\attacker.invalid\\share\\cache.py", "content": ""}),
            str(tmp_path)),
        tool_result("t1", "ok"),
        _with_cwd(assistant_text("Done. I created `cache.py` with the LRU helper."),
                  str(tmp_path)),
    ])
    session, claims = _file_claim_findings(path)
    probed = _trap_filesystem(monkeypatch)
    findings = verify_claims(session, claims, check_disk=True)
    assert probed == []
    # A backslash UNC path is a network path only on Windows. On Linux and macOS the same string is an
    # ordinary file name with backslashes in it, so the claim match and verdict legitimately differ there;
    # the property that holds everywhere is the one above: nothing on the path is ever probed.
    if sys.platform == "win32":
        created = [f for f in findings if f.claim.type is ClaimType.FILE_CREATED]
        assert [f.verdict for f in created] == [Verdict.VERIFIED]
        assert "not checked" in created[0].evidence


def test_forward_slash_unc_in_claim_is_never_probed(tmp_path, monkeypatch):
    path = write_jsonl(tmp_path / "unc-claim.jsonl", [
        _with_cwd(assistant_tool("t1", "Write", {
            "file_path": "//attacker.invalid/share/cache.py", "content": ""}),
            "/home/dev/app"),
        tool_result("t1", "ok"),
        _with_cwd(assistant_text("I created `//attacker.invalid/share/cache.py`."),
                  "/home/dev/app"),
    ])
    session, claims = _file_claim_findings(path)
    probed = _trap_filesystem(monkeypatch)
    verify_claims(session, claims, check_disk=True)
    assert probed == []


def test_unc_audit_end_to_end_stays_offline(tmp_path, monkeypatch, capsys):
    path = write_jsonl(tmp_path / "unc-e2e.jsonl", [
        _with_cwd(assistant_tool("t1", "Write", {
            "file_path": "cache.py", "content": "x = 1"}),
            "//attacker.invalid/share"),
        tool_result("t1", "ok"),
        _with_cwd(assistant_text("I created `cache.py`."),
                  "//attacker.invalid/share"),
    ])
    probed = _trap_filesystem(monkeypatch)
    assert main(["audit", path, "--json"]) == 0
    assert probed == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["contradicted"] == 0


def test_local_disk_check_still_works(tmp_path):
    (tmp_path / "cache.py").write_text("x = 1", encoding="utf-8")
    records = [
        _with_cwd(assistant_tool("t1", "Write", {
            "file_path": str(tmp_path / "cache.py"), "content": "x = 1"}),
            str(tmp_path)),
        tool_result("t1", "ok"),
        _with_cwd(assistant_text("I created `cache.py`."), str(tmp_path)),
        _with_cwd(assistant_tool("t2", "Write", {
            "file_path": str(tmp_path / "gone.py"), "content": ""}),
            str(tmp_path)),
        tool_result("t2", "ok"),
        _with_cwd(assistant_text("I created `gone.py`."), str(tmp_path)),
    ]
    session, claims = _file_claim_findings(
        write_jsonl(tmp_path / "local.jsonl", records))
    findings = verify_claims(session, claims, check_disk=True)
    by_detail = {f.claim.detail: f for f in findings
                 if f.claim.type is ClaimType.FILE_CREATED}
    assert by_detail["cache.py"].verdict is Verdict.VERIFIED
    assert "and file exists on disk [in main]" in by_detail["cache.py"].evidence
    assert by_detail["gone.py"].verdict is Verdict.CONTRADICTED


# ---------------------------------------------------------------------------
# Credentials in commands must not be copied into reports
# ---------------------------------------------------------------------------

_SECRETS = [
    "S3cretProdPw!",
    "Hunter2Prod",
    "ghp_ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
    "sk-proj-abc123DEF456ghi789",
]


def _secret_transcript(tmp_path) -> str:
    edit = {"file_path": "C:\\fake\\project\\src\\db.py",
            "old_string": "a", "new_string": "b"}
    records = [
        assistant_tool("t0", "Edit", edit),
        tool_result("t0", "ok"),
        assistant_tool("t1", "Bash", {
            "command": "OPENAI_API_KEY=sk-proj-abc123DEF456ghi789 npm test || true"}),
        tool_result("t1", "Tests: 1 failed, 3 passed"),
        assistant_text("All tests pass."),
        assistant_tool("t2", "Edit", edit),
        tool_result("t2", "ok"),
        assistant_tool("t3", "Bash", {
            "command": "PGPASSWORD=S3cretProdPw! pytest tests/test_db.py -q"}),
        tool_result("t3", "4 passed"),
        assistant_text("The fix is complete."),
        assistant_tool("t4", "Bash", {
            "command": "DATABASE_URL=postgres://admin:Hunter2Prod@db.internal/app "
                       "python -m pytest -q"}),
        tool_result("t4", "5 passed"),
        assistant_text("Tests are passing."),
        assistant_tool("t5", "Bash", {
            "command": "git push https://x-access-token:"
                       "ghp_ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ@github.com/o/r.git"}),
        tool_result("t5", "ok"),
        assistant_text("I pushed the changes."),
    ]
    return write_jsonl(tmp_path / "secret.jsonl", records)


def test_secrets_are_redacted_from_json_report(tmp_path, capsys):
    path = _secret_transcript(tmp_path)
    assert main(["audit", path, "--json", "--no-disk-check"]) == 0
    out = capsys.readouterr().out
    for secret in _SECRETS:
        assert secret not in out
    payload = json.loads(out)
    evidence = " ".join(c["evidence"] for c in payload["claims"])
    signals = " ".join(s["description"] for s in payload["gaming_signals"])
    # the commands stay recognisable
    assert "PGPASSWORD=*** pytest tests/test_db.py -q" in evidence
    assert "DATABASE_URL=postgres://admin:***@db.internal/app python -m pytest -q" \
        in evidence
    assert "git push https://x-access-token:***@github.com/o/r.git" in evidence
    assert "OPENAI_API_KEY=*** npm test || true" in evidence
    assert "OPENAI_API_KEY=*** npm test || true" in signals


def test_secrets_are_redacted_from_markdown_and_terminal(tmp_path, capsys):
    path = _secret_transcript(tmp_path)
    md = tmp_path / "report.md"
    assert main(["audit", path, "--no-disk-check", "--no-color",
                 "--md", str(md)]) == 0
    terminal = capsys.readouterr().out
    report = md.read_text(encoding="utf-8")
    for secret in _SECRETS:
        assert secret not in terminal
        assert secret not in report


def test_redaction_covers_common_secret_shapes():
    from agent_receipts.redact import redact_secrets

    cases = {
        'curl -H "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.e30.abc" https://api.x.io':
            'curl -H "Authorization: Bearer ***" https://api.x.io',
        "curl -H 'Authorization: token abcdef123456' https://api.github.com":
            "curl -H 'Authorization: token ***' https://api.github.com",
        "curl -H 'X-Api-Key: abcdef123456' https://x.io":
            "curl -H 'X-Api-Key: ***' https://x.io",
        "wget --header=Bearer\tabcdefghij.klmnopqrst.uvw https://x.io":
            "wget --header=Bearer\t*** https://x.io",
        "deploy --token=abcdef123456 --region us-east-1":
            "deploy --token=*** --region us-east-1",
        "curl 'https://x.io/api?api_key=abcdef123456&q=1'":
            "curl 'https://x.io/api?api_key=***&q=1'",
        "mysql --password=hunter2 -u root":
            "mysql --password=*** -u root",
        'export API_KEY="abc def"':
            'export API_KEY="***"',
        "echo sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAA | ./login":
            "echo sk-*** | ./login",
        "gh auth login --with-token github_pat_11AAAAAAA0aaaaaaaaaaaa_bbbbbbbbbbbbbbbbbbbb":
            "gh auth login --with-token github_pat_***",
        "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE":
            "aws configure set aws_access_key_id AKIA***",
        "git clone https://oauth2:glpat-AAAAAAAAAAAAAAAAAAAA@gitlab.com/g/p.git":
            "git clone https://oauth2:***@gitlab.com/g/p.git",
    }
    for raw, expected in cases.items():
        assert redact_secrets(raw) == expected, raw


def test_redaction_leaves_ordinary_commands_alone():
    from agent_receipts.redact import redact_secrets

    for command in [
        "python -m pytest tests/ -q || true",
        "git commit -am 'Add rate limiting' --no-verify",
        "npm run build && npm test",
        "git push origin main",
        "rm tests/test_rate_limit.py",
        "curl https://example.com:8080/path?email=a@b.com",
        "cargo test --workspace -- --nocapture",
        "docker build -t app:latest .",
        "ssh git@github.com",
        "task-runner --skip-lint",
        "git commit -m 'Support bearer tokens and Bearer auth'",
        "python gen.py --max_tokens=100 --monkey=1",
        'curl -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com',
        "GITHUB_TOKEN=$TOKEN npm test",
    ]:
        assert redact_secrets(command) == command


def test_redaction_is_linear_on_hostile_input():
    from agent_receipts.redact import redact_secrets

    for hostile in ["a." * 50_000, "a_" * 50_000, "://a:" * 20_000,
                    "token=\"" * 20_000, "Bearer " * 20_000, "x" * 100_000]:
        start = time.perf_counter()
        redact_secrets(hostile)
        assert time.perf_counter() - start < 2.0


def test_redaction_leaves_lookalike_names_and_prose_alone():
    from agent_receipts.redact import redact_secrets

    for command in [
        # a bare "key" name needs a secret-shaped value
        "sort --key=2 data.txt",
        'git commit -m "Rotate API key: see docs"',
        "git commit -m 'fix: handle missing key'",
        "http --key=/etc/ssl/private/client.key https://x.io",
        # a name that only contains a secret word is not a secret name
        "echo monkey=banana",
        "pytest --key-order=random",
        # token prefixes need 12 or more characters after them
        "git checkout feature/sk-login",
        "git checkout -b sk-fix-typo",
        "echo ghp_12345678901",
    ]:
        assert redact_secrets(command) == command, command


def test_redaction_masks_secret_names_and_secret_shaped_key_values():
    from agent_receipts.redact import redact_secrets

    cases = {
        # a bare "key" name with a secret-shaped value is still masked
        "STRIPE_KEY=rk_live_51HxAbCdEfGhIjKlMn npm start":
            "STRIPE_KEY=*** npm start",
        '{"key": "AbCdEfGhIjKlMnOpQrSt"}':
            '{"key": "***"}',
        "tool --key=Zm9vYmFyYmF6cXV4cXV1eA== run":
            "tool --key=*** run",
        # names ending in a secret word are masked whatever the value
        "tool --pwd=hunter2 run":
            "tool --pwd=*** run",
        '{"auth": "abc123"}':
            '{"auth": "***"}',
        "SSH_PRIVATE_KEY=abc ./deploy.sh":
            "SSH_PRIVATE_KEY=*** ./deploy.sh",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY aws s3 ls":
            "AWS_SECRET_ACCESS_KEY=*** aws s3 ls",
        "curl -H 'X-Auth-Token: abc123' https://x.io":
            "curl -H 'X-Auth-Token: ***' https://x.io",
        # a token prefix followed by 12 or more characters is masked
        "echo ghp_1234567890123456":
            "echo ghp_***",
        "echo ghp_123456789012":
            "echo ghp_***",
        # credential is a credential word, so its value is masked
        "git -c credential.helper=store fetch":
            "git -c credential.helper=*** fetch",
    }
    for raw, expected in cases.items():
        assert redact_secrets(raw) == expected, raw


def test_narrowed_redaction_is_linear_on_hostile_input():
    from agent_receipts.redact import redact_secrets

    for hostile in ["key=" * 25_000 + ".", "a_key: " * 15_000,
                    "key=\"" * 20_000, "key='" * 20_000,
                    "pwd=" * 25_000, "sk-" * 30_000 + "!",
                    "Authorization: " * 7_000, "-" * 100_000 + "key=x"]:
        start = time.perf_counter()
        redact_secrets(hostile)
        assert time.perf_counter() - start < 2.0, hostile[:20]


# ---------------------------------------------------------------------------
# Final rule: credential names are matched by word, a bare "key" by value
# ---------------------------------------------------------------------------

def test_final_rule_masks_credential_assignments():
    from agent_receipts.redact import redact_secrets

    cases = {
        # the required cases
        "export SECRET_KEY=django-insecure-abc": "export SECRET_KEY=***",
        "ENCRYPTION_KEY=0123456789abcdef0123": "ENCRYPTION_KEY=***",
        "--password hunter2": "--password ***",
        '"clientSecret": "s3cr3t"': '"clientSecret": "***"',
        "accessToken=abc123": "accessToken=***",
        "Authorization: Bearer x": "Authorization: Bearer ***",
        "PWD=pa55 (ODBC)": "PWD=*** (ODBC)",
        # the same rule in other shapes
        'sqlcmd "Driver={ODBC Driver 18};Server=db;UID=sa;PWD=pa55;"':
            'sqlcmd "Driver={ODBC Driver 18};Server=db;UID=sa;PWD=***;"',
        "mysql --password 'hunter 2' -u root":
            "mysql --password '***' -u root",
        "deploy --api-key=k1 --signing-key s2 --region us-east-1":
            "deploy --api-key=*** --signing-key *** --region us-east-1",
        "APIKey=abc MasterKey=def X-Access-Token=ghi":
            "APIKey=*** MasterKey=*** X-Access-Token=***",
        # camelCase splits a word that does not end the name
        """tool tokenValue=abc --data '{"apiKeyId": "k1", "authHeader": "h"}'""":
            """tool tokenValue=*** --data '{"apiKeyId": "***", "authHeader": "***"}'""",
        "NGROK_AUTHTOKEN=2abc GPG_PASSPHRASE=p ngrok http 80":
            "NGROK_AUTHTOKEN=*** GPG_PASSPHRASE=*** ngrok http 80",
        "curl -H 'x-session-key: s1' https://x.io":
            "curl -H 'x-session-key: ***' https://x.io",
        "curl -H 'authorization: basic dXNlcjpwYXNz' https://x.io":
            "curl -H 'authorization: basic ***' https://x.io",
        "curl -H 'AUTHORIZATION: Bearer abc' https://x.io":
            "curl -H 'AUTHORIZATION: Bearer ***' https://x.io",
        "DATABASE_URL=mysql://h/db?password=abc pytest":
            "DATABASE_URL=mysql://h/db?password=*** pytest",
        "$env:API_TOKEN = 'abc'; npm test":
            "$env:API_TOKEN = '***'; npm test",
        'echo "password: hunter2" > .env':
            'echo "password: ***" > .env',
        # a value is masked whatever its length
        "tool --secret=a --auth b":
            "tool --secret=*** --auth ***",
        # JSON inside a double-quoted shell string escapes its quotes
        'curl -d "{\\"user\\": \\"admin\\", \\"password\\": \\"hunter2\\"}" https://x.io':
            'curl -d "{\\"user\\": \\"admin\\", \\"password\\": \\"***\\"}" https://x.io',
        'curl -d "{\\"auth\\": {\\"token\\":\\"t0k\\"}}" https://x.io':
            'curl -d "{\\"auth\\": {\\"token\\":\\"***\\"}}" https://x.io',
        # glued all-caps names still count (PGPASSWORD, AWS_ACCESSKEY)
        "export AWS_ACCESSKEY=abc MINIO_SECRETKEY=def":
            "export AWS_ACCESSKEY=*** MINIO_SECRETKEY=***",
        # a --flag value with a token prefix keeps the prefix
        "gh auth login --with-token ghp_123456789012":
            "gh auth login --with-token ghp_***",
        "gh auth login --with-token short-tok":
            "gh auth login --with-token ***",
    }
    for raw, expected in cases.items():
        assert redact_secrets(raw) == expected, raw


def test_final_rule_leaves_lookalikes_and_non_secrets_alone():
    from agent_receipts.redact import redact_secrets

    for command in [
        # the required cases
        "sort --key=2 data.txt",
        "echo monkey=banana",
        "git checkout feature/sk-login",
        'git commit -m "auth: fix login redirect"',
        "docker run -v $PWD:/app node",
        "--max-token=4096",
        "CACHE_KEY=build-v2",
        'git commit -m "Rotate API key: see docs"',
        # none, null, true, false, 0 and plain numbers are not secrets
        "run --token=none --secret=null --auth=true --password=False --pwd=0",
        """curl -d '{"token": null, "auth": false, "max_tokens": 512}'""",
        "gen --max-token 4096 --temperature 0.7 --token-limit=-1",
        # $NAME and ${NAME} are references, not assignments
        "echo ${TOKEN:-unset} $PASSWORD:$SECRET ${AUTHORIZATION:-none}",
        "echo $Authorization:/app",
        "cd $OLDPWD && OLDPWD=/tmp ls",
        # names whose words are not credential words
        "git commit --author='Dev <dev@x.io>' -m wip",
        "python train.py --tokenizer=gpt2 --max_tokens=100",
        "sort --key 3 --key-order random",
        # a credential name followed by prose is not an assignment
        'git commit -m "token: refresh on 401"',
        # token prefixes need 12 or more characters after them
        "echo ghp_12345678901 sk-12345678901 AKIA12345678901",
    ]:
        assert redact_secrets(command) == command, command


def test_final_rule_token_prefixes_need_12_characters():
    from agent_receipts.redact import redact_secrets

    for prefix in ["sk-", "sk_live_", "rk_", "ghp_", "gho_", "ghu_", "ghs_",
                   "ghr_", "github_pat_", "glpat-", "xoxa-", "xoxb-", "xoxp-",
                   "xoxr-", "xoxs-", "AIza", "AKIA", "ASIA"]:
        assert redact_secrets(f"echo {prefix}{'A' * 12} ok") == \
            f"echo {prefix}*** ok", prefix
        assert redact_secrets(f"echo {prefix}{'A' * 40}") == \
            f"echo {prefix}***", prefix
        short = f"echo {prefix}{'A' * 11} ok"
        assert redact_secrets(short) == short, prefix


def test_final_rule_is_linear_on_hostile_input():
    from agent_receipts.redact import redact_secrets

    for hostile in ["--password " * 20_000, "--password " * 20_000 + "-",
                    "auth: " * 20_000, "auth: x " * 15_000,
                    '"clientSecret": ' * 10_000, "accessToken=" * 20_000 + "!",
                    "$PWD:" * 20_000, "${TOKEN:" * 15_000, "aB" * 50_000 + "=x",
                    "key=1" * 20_000 + ".", "CACHE_KEY=" + "a." * 50_000,
                    "k=\"" + "token='" * 15_000, "sk_live_" * 15_000 + "!",
                    "--with-token " * 10_000 + "ghp_" + "a" * 20,
                    "a:" + " " * 100_000 + "b", "Authorization: Bearer " * 5_000,
                    "PWD=" * 25_000, "--key " * 20_000 + "x" * 20,
                    "x_key=" * 20_000 + "@", '\\"token\\": ' * 10_000,
                    '\\"key\\":\\"' * 10_000, 'k=\\"' + 'x_key=\\"a' * 10_000]:
        start = time.perf_counter()
        redact_secrets(hostile)
        assert time.perf_counter() - start < 2.0, hostile[:20]


# ---------------------------------------------------------------------------
# A scheme word in a header value masks the word after it, not itself
# ---------------------------------------------------------------------------

_SCHEME_HEADER_CASES = {
    # the required cases: (command, secret that must not survive, expected)
    "curl -H 'X-Api-Key: Basic dXNlcjpwYXNz' https://x":
        ("dXNlcjpwYXNz", "curl -H 'X-Api-Key: Basic ***' https://x"),
    "curl -H 'X-Auth-Token: Bearer eyJ0eXAi' https://x":
        ("eyJ0eXAi", "curl -H 'X-Auth-Token: Bearer ***' https://x"),
    "curl -H 'X-Access-Token: token ghp123abc' https://x":
        ("ghp123abc", "curl -H 'X-Access-Token: token ***' https://x"),
    "curl -H 'Token: bearer abc123' u":
        ("abc123", "curl -H 'Token: bearer ***' u"),
    "curl -H 'Authorization: Bearer \"abc123\"' https://x":
        ("abc123", "curl -H 'Authorization: Bearer \"***\"' https://x"),
}


def test_scheme_word_after_any_credential_header_masks_the_value():
    from agent_receipts.redact import redact_secrets

    for raw, (secret, expected) in _SCHEME_HEADER_CASES.items():
        redacted = redact_secrets(raw)
        assert secret not in redacted, raw
        assert redacted == expected, raw


def test_scheme_word_masks_the_value_in_other_header_shapes():
    from agent_receipts.redact import redact_secrets

    cases = {
        # a quoted value after the scheme is masked whole
        "curl -H 'Authorization: Bearer \"abc 123\"' https://x":
            "curl -H 'Authorization: Bearer \"***\"' https://x",
        "curl -H \"Authorization: Bearer 'abc123'\" https://x":
            "curl -H \"Authorization: Bearer '***'\" https://x",
        'curl -H "X-Api-Key: Bot \\"abc\\"" https://x':
            'curl -H "X-Api-Key: Bot \\"***\\"" https://x',
        # no blank after the colon, any case, tabs, CRLF header lines
        "curl -H 'X-Api-Key:Basic dXNl' https://x":
            "curl -H 'X-Api-Key:Basic ***' https://x",
        "X-Api-Key: BASIC dXNl\nHost: x":
            "X-Api-Key: BASIC ***\nHost: x",
        "X-Api-Key: Bearer\t\tabc\r\nHost: x":
            "X-Api-Key: Bearer\t\t***\r\nHost: x",
        "Proxy-Authorization: Basic dXNl\r\n":
            "Proxy-Authorization: Basic ***\r\n",
        '"Token": bearer abc, "x": 1':
            '"Token": bearer ***, "x": 1',
        # after a scheme word a number is a token too
        "curl -H 'X-Api-Key: Bearer 12345' https://x":
            "curl -H 'X-Api-Key: Bearer ***' https://x",
        "curl -H 'Authorization: Bearer \"12345\"' https://x":
            "curl -H 'Authorization: Bearer \"***\"' https://x",
        # a bare "key" header name with a scheme word is a credential
        "curl -H 'X-Key: Bearer abc' https://x":
            "curl -H 'X-Key: Bearer ***' https://x",
        # a bare Bearer token of 16 or more characters, quoted
        'echo Bearer "abcdefghijklmnopqrstu"':
            'echo Bearer "***"',
    }
    for raw, expected in cases.items():
        assert redact_secrets(raw) == expected, raw


def test_scheme_word_leaves_references_prose_and_shell_words_alone():
    from agent_receipts.redact import redact_secrets

    for command in [
        # $VARS after the scheme are references
        "curl -H 'X-Api-Key: Bearer $KEY' https://x",
        'curl -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com',
        # a header name followed by prose is not a header
        'git commit -m "token: bearer support added"',
        # in a shell, TOKEN=bearer gives TOKEN the word bearer only
        "TOKEN=bearer ./run.sh",
        "httpie --auth-type basic https://x",
        # a scheme with no value on its line has nothing to mask
        "X-Api-Key: Basic\nnext line",
        "X-Api-Key: Basic ",
        # prose Bearer stays readable
        "git commit -m 'Support bearer tokens and Bearer auth'",
    ]:
        assert redact_secrets(command) == command, command


def test_scheme_word_masks_escaped_quotes_parameters_and_dict_values():
    from agent_receipts.redact import redact_secrets

    cases = {
        # an escaped quote after the scheme, inside a double-quoted word
        'curl -H "Authorization: Bearer \\"abc123\\"" https://x':
            'curl -H "Authorization: Bearer \\"***\\"" https://x',
        'curl -H "authorization:basic \\"dXNl\\"" -H "Accept: */*" u':
            'curl -H "authorization:basic \\"***\\"" -H "Accept: */*" u',
        "Authorization: Bearer \\'abc123\\'":
            "Authorization: Bearer \\'***\\'",
        # a name="value" parameter (Token token="...") is masked whole
        "curl -H 'Authorization: Token token=\"abc123\"' https://x":
            "curl -H 'Authorization: Token ***' https://x",
        'curl -H "Authorization: Token token=\\"abc123\\", nonce=\\"n1\\"" u':
            'curl -H "Authorization: Token ***, nonce=\\"n1\\"" u',
        "curl -H 'X-Api-Key: Bearer token=\"abc123\"' https://x":
            "curl -H 'X-Api-Key: Bearer ***' https://x",
        'curl -H "X-Api-Key: Token token=\\"abc123\\"" https://x':
            'curl -H "X-Api-Key: Token ***" https://x',
        'Authorization: token="abc123"\r\nHost: x':
            "Authorization: ***\r\nHost: x",
        "Authorization:token='abc123'":
            "Authorization:***",
        # a quoted scheme and token under a bare "key" name is a credential
        """python -c "get(u, headers={'X-Key': 'Basic dXNl'})\"""":
            """python -c "get(u, headers={'X-Key': '***'})\"""",
        'fetch(u, {headers: {"X-Key": "Bearer abc123"}})':
            'fetch(u, {headers: {"X-Key": "***"}})',
        'irm u -Headers @{"x-key" = "bot abc123"}':
            'irm u -Headers @{"x-key" = "***"}',
        'curl -d "{\\"key\\": \\"Token abc123\\"}" https://x':
            'curl -d "{\\"key\\": \\"***\\"}" https://x',
    }
    for raw, expected in cases.items():
        redacted = redact_secrets(raw)
        assert "abc123" not in redacted and "dXNl" not in redacted, raw
        assert redacted == expected, raw

    for raw, expected in {
        # a base64 "=" before a closing shell quote is not a parameter
        'curl -H "X-Api-Key: Basic dXNlcjpwYXNzZQo=" https://x.io':
            'curl -H "X-Api-Key: Basic ***" https://x.io',
        'curl -H "Authorization: Basic dXNlcjpwYXNzZQo=" -H "Accept: x" u':
            'curl -H "Authorization: Basic ***" -H "Accept: x" u',
        'curl -H "X-Api-Key: Basic dXNlcjpwYXNzZQo="$EXTRA https://x.io':
            'curl -H "X-Api-Key: Basic ***"$EXTRA https://x.io',
        'curl -H "Authorization: dXNlcjpwYXNzZQo="$EXTRA https://x.io':
            'curl -H "Authorization: ***"$EXTRA https://x.io',
        'echo "AUTH_TOKEN=abc=" >> .env':
            'echo "AUTH_TOKEN=***" >> .env',
    }.items():
        assert redact_secrets(raw) == expected, raw

    for command in [
        # a bare "key" with a quoted value that is not scheme and token
        '{"key": "Bearer tokens expire hourly"}',
        '{"key": "token"}',
        """get(u, headers={'X-Key': 'Bearer $KEY'})""",
        "sort --key 'basic' data.txt",
    ]:
        assert redact_secrets(command) == command, command


def test_scheme_word_secrets_stay_out_of_reports(tmp_path, capsys):
    edit = {"file_path": "C:\\fake\\project\\src\\api.py",
            "old_string": "a", "new_string": "b"}
    records = []
    claims = ["All tests pass.", "The fix is complete.", "Tests are passing.",
              "The test suite passes now.", "Everything is working."]
    commands = [f"{raw} && pytest -q" for raw in _SCHEME_HEADER_CASES]
    for n, (command, claim) in enumerate(zip(commands, claims)):
        records += [
            assistant_tool(f"e{n}", "Edit", edit),
            tool_result(f"e{n}", "ok"),
            assistant_tool(f"t{n}", "Bash", {"command": command}),
            tool_result(f"t{n}", "4 passed"),
            assistant_text(claim),
        ]
    path = write_jsonl(tmp_path / "scheme.jsonl", records)
    md = tmp_path / "report.md"
    assert main(["audit", path, "--json", "--no-disk-check"]) == 0
    out = capsys.readouterr().out
    assert main(["audit", path, "--no-disk-check", "--no-color",
                 "--md", str(md)]) == 0
    terminal = capsys.readouterr().out
    report = md.read_text(encoding="utf-8")
    evidence = " ".join(c["evidence"] for c in json.loads(out)["claims"])
    for secret, expected in _SCHEME_HEADER_CASES.values():
        # the command was quoted as evidence, with its value masked
        assert expected in evidence, expected
        for text in (out, terminal, report):
            assert secret not in text, secret


def test_scheme_word_redaction_is_linear_on_hostile_input():
    from agent_receipts.redact import redact_secrets

    for hostile in ["X-Api-Key: Basic " * 15_000, "Token: bearer " * 15_000,
                    "token:bot\t" * 20_000, "a_key: token " * 15_000 + "x",
                    'Authorization: Bearer "' * 10_000,
                    "Authorization: Bearer '" * 10_000,
                    "Token: Bearer " + " " * 100_000 + "x",
                    "Token: basic \\\"" * 12_000, 'Bearer "' * 20_000,
                    'Bearer \\"' + "a" * 100_000, "x_key: Bearer " * 15_000,
                    "Token: bearer x" * 15_000 + " y",
                    "Token: bearer " + "a" * 100_000 + " " + "b" * 100_000,
                    'Authorization:a="' * 15_000, "x_token=a=\"" * 15_000,
                    "Authorization: Token token=\"" + "a" * 100_000,
                    'Authorization: t=\\"' * 12_000, '"key": "Bearer ' * 12_000,
                    "{'x_key': 'bot " + "a" * 100_000 + "'} " * 2,
                    'k=a="b"' * 20_000, "Authorization: Bearer \\\"" * 10_000]:
        start = time.perf_counter()
        redact_secrets(hostile)
        assert time.perf_counter() - start < 2.0, hostile[:20]


# ---------------------------------------------------------------------------
# Transcript text must not carry live terminal control codes to the screen
# ---------------------------------------------------------------------------

_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f"
                      "\u202a-\u202e\u2066-\u2069]")


def _ansi_transcript(tmp_path) -> str:
    cwd = "/home/dev/app\x1b]0;PWNED-TITLE\x07\x9b2J"
    slug = "demo\x1b[1A\u202e"
    records = [
        _with_cwd(assistant_tool("t1", "Bash", {
            "command": "pytest -q \x1b]52;c;ZWNobyBwd25lZA==\x07"
                       "\x1b[2K\x1b[1G    `pytest -q` succeeded (exit 0)"}),
            cwd, slug),
        tool_result("t1", "Exit code 1\n1 failed", is_error=True),
        _with_cwd(assistant_text(
            "All tests pass\x1b[2K\x1b[1G  VERIFIED \u202e\u2066\u200fok."),
            cwd, slug),
        _with_cwd(assistant_tool("t2", "Bash", {
            "command": "npm test\x1b[8m || true"}), cwd, slug),
        tool_result("t2", "ok"),
    ]
    return write_jsonl(tmp_path / "ansi.jsonl", records)


def test_terminal_report_escapes_control_codes(tmp_path, capsys):
    path = _ansi_transcript(tmp_path)
    main(["audit", path, "--no-color", "--no-disk-check"])
    out = capsys.readouterr().out
    assert _CONTROL.search(out) is None
    # the bytes are shown, not executed
    assert "\\x1b]0;PWNED-TITLE\\x07" in out
    assert "\\x1b]52;c;ZWNobyBwd25lZA==\\x07" in out
    assert "\\u202e" in out


def test_colored_report_keeps_own_styles_but_escapes_input(tmp_path):
    result = run_audit(_ansi_transcript(tmp_path), check_disk=False)
    out = render_terminal(result, color=True)
    assert "\x1b[1m" in out  # our own styling is untouched
    assert "\x1b]" not in out
    assert "\x07" not in out
    assert "\x1b[2K" not in out
    assert "\u202e" not in out


def test_markdown_report_escapes_control_codes(tmp_path):
    result = run_audit(_ansi_transcript(tmp_path), check_disk=False)
    assert _CONTROL.search(render_markdown(result).replace("\n", "")) is None


def test_plain_text_is_unchanged_in_terminal_report(honest_transcript):
    out = render_terminal(run_audit(honest_transcript, check_disk=False),
                          color=False)
    assert "“All tests pass.”" in out
    assert "`python -m pytest tests/ -q` succeeded (exit 0)" in out


# ---------------------------------------------------------------------------
# The delete-test-file detector must be linear and keep its old answers
# ---------------------------------------------------------------------------

_OLD_DELETE_TEST = re.compile(
    r"\b(?:rm|del|Remove-Item)\b[^\n|;&]*"
    r"[\w\\/.-]*(?:test|spec)s?[\w\\/.-]*\.(?:py|js|ts|tsx|go|rs|rb|java|cs)",
    re.IGNORECASE,
)


def _deleted_test_signal(tmp_path, command: str) -> bool:
    path = write_jsonl(tmp_path / "cmd.jsonl", [
        assistant_tool("t1", "Bash", {"command": command}),
        tool_result("t1", "ok"),
    ])
    return any(s.kind == "deleted_test_file"
               for s in detect_gaming(parse_transcript(path)))


def test_delete_test_detection_is_linear(tmp_path):
    for hostile in ["rm/" * 600, "rm " + "test" * 5_000,
                    "rm " + "test/" * 5_000 + "x"]:
        start = time.perf_counter()
        _deleted_test_signal(tmp_path, hostile)
        assert time.perf_counter() - start < 2.0, hostile[:20]


def test_delete_test_detection_still_flags_real_deletes(tmp_path):
    assert _deleted_test_signal(tmp_path, "rm tests/test_rate_limit.py")
    assert _deleted_test_signal(tmp_path, "git rm -f src/__tests__/api.spec.ts")
    assert _deleted_test_signal(
        tmp_path, "Remove-Item -Force tests\\test_core.py")
    assert not _deleted_test_signal(tmp_path, "rm build/output.js")
    assert not _deleted_test_signal(tmp_path, "rm -rf dist; echo test.py")


def test_delete_test_detection_matches_old_pattern():
    from agent_receipts.gaming import _deletes_test_file

    rng = random.Random(1234)
    tokens = ["rm", "del", "Remove-Item", "RM", "test", "spec", "Tests", "s",
              ".", "py", "js", "tsx", "go", "/", "\\", "-", "_", " ", " ",
              "|", ";", "&", "\n", "a", "x", "la", "\u017f", "é"]
    for _ in range(20_000):
        command = "".join(rng.choice(tokens)
                          for _ in range(rng.randint(1, 14)))
        assert _deletes_test_file(command) == bool(
            _OLD_DELETE_TEST.search(command)), repr(command)
