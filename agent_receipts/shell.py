"""Split a shell command into statements and pipeline stages.

The auditor needs to know what a command actually ran, not which words
appear in it: `grep -rn pytest src | head` reads files, it does not run
the tests. This module splits a Bash or PowerShell command line into
statements (separated by &&, ||, ;, & or a newline) and each statement
into pipeline stages (separated by |), outside quotes and here-doc
bodies, and normalises the head of each stage so a check runner can be
recognised by the program it starts.

It is a small, linear scanner, not a shell. It errs toward not seeing a
check (a command it cannot read counts as no evidence and no signal).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Programs whose job is to page or filter output. As the last stage of a
# pipeline they decide its exit status (without pipefail), so a failing
# test run piped into one of them reports exit 0.
FILTERS = frozenset({
    "tail", "head", "grep", "egrep", "fgrep", "rg", "less", "more",
    "sort", "uniq", "wc", "cut", "awk", "sed", "tee", "tr", "column",
    "select-object", "select", "select-string", "sls", "findstr",
    "out-string", "out-host", "where-object", "where", "measure-object",
})

# Words that only modify how the next program starts.
_PREFIX_WORDS = frozenset({
    "do", "then", "else", "elif", "time", "sudo", "env", "exec", "nohup",
    "command", "builtin", "!", "&", "{", "(", "nice", "xvfb-run",
})
# Runners that start the program named after them.
_RUNNERS = {
    "uv": "run", "poetry": "run", "pipenv": "run", "hatch": "run",
    "pdm": "run", "rye": "run",
}
_SHELL_WRAPPERS = frozenset({"bash", "sh", "zsh", "pwsh", "powershell", "cmd"})
_WRAPPER_FLAGS = frozenset({"-c", "-lc", "-ic", "-command", "/c", "/k"})

_WORD = re.compile(r"""(?:"(?:[^"\\]|\\.)*"?|'[^']*'?|[^\s"'])+""")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PY_HEAD = re.compile(r"(?:python[0-9.]*|py)", re.IGNORECASE)
_EXE_SUFFIX = re.compile(r"\.(?:exe|cmd|bat)$", re.IGNORECASE)
_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")
# Interpreter options that may sit between python (or py) and -m.
_PY_VERSION_FLAG = re.compile(r"^-(?:[0-9][0-9.]*(?:-(?:32|64))?|V:\S+)$")
_PY_SINGLE_FLAGS = re.compile(r"^-[uBOEIsSbdqRP]+$")
_PY_ARG_FLAGS = frozenset({"-X", "-W"})
# timeout's options that take a value, before its duration.
_TIMEOUT_ARG_FLAGS = frozenset({"-s", "-k", "--signal", "--kill-after"})


@dataclass
class Statement:
    """One pipeline and the operator that follows it ('' at the end)."""

    stages: list[str]
    op: str
    start: int = 0  # span of the statement in the command, operator excluded
    end: int = 0


def split_statements(command: str) -> list[Statement]:
    """Split at top-level &&, ||, ;, &, newline and |, in one pass."""
    statements: list[Statement] = []
    stages: list[str] = []
    buf: list[str] = []
    heredocs: list[tuple[str, bool]] = []
    started = False  # the current stage has a non-blank character
    stmt_start = 0
    i, n = 0, len(command)

    def end_stage() -> None:
        nonlocal started
        stages.append("".join(buf).strip())
        buf.clear()
        started = False

    def end_statement(op: str, at: int) -> None:
        nonlocal stages, stmt_start
        end_stage()
        if any(stages):
            statements.append(Statement(stages=stages, op=op,
                                        start=stmt_start, end=at))
        stages = []
        stmt_start = at + len(op)

    while i < n:
        ch = command[i]
        if ch in "\\'\"@<":
            started = True
        if ch == "\\" and i + 1 < n:
            buf.append(command[i:i + 2])
            i += 2
            continue
        if ch == "'":
            close = command.find("'", i + 1)
            close = n - 1 if close < 0 else close
            buf.append(command[i:close + 1])
            i = close + 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and command[j] != '"':
                j += 2 if command[j] == "\\" else 1
            buf.append(command[i:j + 1])
            i = j + 1
            continue
        if ch == "@" and command[i + 1:i + 2] in ("'", '"') \
                and command[i + 2:i + 3] in ("\n", "\r"):
            # PowerShell here-string: @' ... '@ on its own line.
            closer = "\n" + command[i + 1] + "@"
            close = command.find(closer, i + 2)
            close = n if close < 0 else close + len(closer)
            buf.append(command[i:close])
            i = close
            continue
        if ch == "<" and command.startswith("<<", i) and not command.startswith("<<<", i):
            match = _HEREDOC.match(command, i)
            if match:
                heredocs.append((match.group(3), match.group(1) == "-"))
                buf.append(match.group())
                i = match.end()
                continue
        if ch == "\n":
            end_statement("\n", i)
            i += 1
            # Skip here-doc bodies opened on the line that just ended.
            for word, strip_tabs in heredocs:
                while i < n:
                    eol = command.find("\n", i)
                    eol = n if eol < 0 else eol
                    line = command[i:eol].rstrip("\r")
                    i = eol + 1
                    if (line.lstrip("\t") if strip_tabs else line) == word:
                        break
            heredocs.clear()
            continue
        if ch == "#" and not started:
            # A comment at the start of a stage runs to the end of the line.
            eol = command.find("\n", i)
            i = n if eol < 0 else eol
            continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            end_statement(two, i)
            i += 2
            continue
        if ch == ";":
            end_statement(";", i)
            i += 1
            continue
        if ch == "|":
            end_stage()
            i += 2 if two == "|&" else 1
            continue
        if ch == "&" and command[i + 1:i + 2] != ">" and command[i - 1:i] not in (">", "<"):
            end_statement("&", i)
            i += 1
            continue
        buf.append(ch)
        if not ch.isspace():
            started = True
        i += 1
    end_statement("", n)
    return statements


def _unquote(word: str) -> str:
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "\"'":
        return word[1:-1]
    return word.strip("\"'")


def _basename(word: str) -> str:
    word = _unquote(word)
    cut = max(word.rfind("/"), word.rfind("\\"))
    return word[cut + 1:] if cut >= 0 else word


def _skip_timeout(words: list[str]) -> list[str]:
    """Drop `timeout`, its options and its duration: `timeout -s KILL 60 x` is x."""
    i = 1
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in _TIMEOUT_ARG_FLAGS else 1
    if i < len(words) and words[i] == "--":
        i += 1
    return words[i + 1:]


def _skip_python_options(rest: list[str]) -> list[str]:
    """Drop interpreter options before -m: `-3 -X utf8 -u -m pytest` is `-m pytest`."""
    i = 0
    while i < len(rest) and rest[i] != "-m":
        word = rest[i]
        if word in _PY_ARG_FLAGS:
            i += 2
        elif _PY_VERSION_FLAG.match(word) or _PY_SINGLE_FLAGS.match(word):
            i += 1
        else:
            return rest
    return rest[i:] if i < len(rest) else rest


def normalize_stage(stage: str, _depth: int = 0) -> str:
    """The stage with its prefixes dropped and its program reduced to a name.

    `FOO=1 sudo "C:/Python314/python.exe" -m pytest -q` becomes
    `python -m pytest -q`. A shell wrapper (`bash -c "..."`) returns the
    normalised first stage of its inner command.
    """
    words = _WORD.findall(stage.strip().lstrip("({!& \t"))
    while words:
        low = words[0].lower()
        if low in _PREFIX_WORDS or _ASSIGNMENT.match(words[0]):
            words.pop(0)
        elif low == "timeout" and len(words) > 1:
            words = _skip_timeout(words)
        elif low in _RUNNERS and len(words) > 1 and words[1] == _RUNNERS[low]:
            words = words[2:]
        else:
            break
    if not words:
        return ""
    head = _EXE_SUFFIX.sub("", _basename(words[0])).lower()
    if _PY_HEAD.fullmatch(head):
        head = "python"
    rest = words[1:]
    if head == "python":
        rest = _skip_python_options(rest)
    if head == "git":
        while len(rest) >= 2 and rest[0] in ("-C", "-c"):
            rest = rest[2:]
    if (head in _SHELL_WRAPPERS and rest and rest[0].lower() in _WRAPPER_FLAGS
            and _depth < 2):
        inner = _unquote(" ".join(rest[1:]))
        statements = split_statements(inner)
        if statements and statements[0].stages:
            return normalize_stage(statements[0].stages[0], _depth + 1)
        return ""
    return " ".join([head] + rest)


def stage_heads(command: str) -> list[str]:
    """The normalised form of every stage of every statement."""
    return [normalize_stage(stage)
            for statement in split_statements(command)
            for stage in statement.stages if stage]


def program(stage: str) -> str:
    """The program name a stage starts, lowercased."""
    norm = normalize_stage(stage)
    return norm.split(" ", 1)[0] if norm else ""


_CD_COMMANDS = frozenset({"cd", "chdir", "pushd", "set-location", "sl"})
_DRIVE_MSYS = re.compile(r"^/([A-Za-z])(?=/|$)")
_DRIVE = re.compile(r"^[A-Za-z]:/")


def norm_path(path: str, base: str | None = None) -> str | None:
    """A comparable form of a path: forward slashes, lowercase, no dot parts.

    /c/Users and C:\\Users are the same folder. A relative path is joined
    to `base`; without one, or with a variable or ~ in it, None.
    """
    path = _unquote(path.strip()).replace("\\", "/")
    if not path or "$" in path or path.startswith("~") or "%" in path:
        return None
    path = _DRIVE_MSYS.sub(lambda m: m.group(1) + ":", path)
    if not (_DRIVE.match(path) or path.startswith("/")):
        if base is None:
            return None
        path = base + "/" + path
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if len(parts) > 1:
                parts.pop()
            continue
        parts.append(part)
    joined = "/".join(parts).lower()
    return joined if _DRIVE.match(joined + "/") else "/" + joined


def run_directory(command: str, cwd: str, is_target) -> str | None:
    """The folder the first stage matching `is_target` runs in, or None.

    Follows `cd`, `pushd` and `Set-Location` from the call's working
    directory, in order. None when that cannot be told (no known start,
    a variable in the path, or no matching stage).
    """
    current = norm_path(cwd) if cwd else None
    for statement in split_statements(command):
        for stage in statement.stages:
            if not stage:
                continue
            if is_target(stage):
                return current
            words = _WORD.findall(normalize_stage(stage))
            if words and words[0] in _CD_COMMANDS:
                args = [w for w in words[1:] if not w.startswith("-")
                        and w.lower() != "/d"]
                current = norm_path(args[-1], current) if args else None
    return None
