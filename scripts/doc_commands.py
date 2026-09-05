"""Extract every ``seohead ...`` invocation shown in the documentation.

Both ``tests/test_docs_commands_execute.py`` (which runs each one against fixtures
in CI) and anyone auditing the docs by hand import this module, so there is exactly
one place that knows what counts as "a command shown in the documentation" and how
to turn the Markdown text of one back into an argv list.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

ECHO_PIPE_RE = re.compile(r"^echo\s+'(?P<payload>.*)'\s*\|\s*seohead\s+(?P<rest>.+)$")
# A fence opener: leading indent, 3+ backticks (or tildes), an optional info string
# (``bash``, ``json``, ``text``, ... or none). A single non-greedy regex across the
# whole file cannot tell an opener from the next block's opener once an info string
# it doesn't know about sits in between, so fences are scanned line by line instead —
# every info string closes correctly, none needs to be named here (issue #129).
_FENCE_OPEN_RE = re.compile(r"^([ \t]*)(`{3,}|~{3,})[ \t]*(\S*)[ \t]*$")


def doc_files(root: Path) -> list[Path]:
    """Every Markdown file whose fenced examples are part of the public contract."""
    return sorted(
        [
            root / "README.md",
            root / "AGENTS.md",
            root / "CONTRIBUTING.md",
            # Every level, not just the top: docs/scenarios/ describes chains, and a chain
            # whose commands are not executed by CI is a chain that quietly stops working.
            *(root / "docs").glob("**/*.md"),
            *(root / ".claude" / "skills").glob("*/**/*.md"),
            *(root / "seohead" / "skills").glob("*/SKILL.md"),
            *(root / "examples").glob("**/README.md"),
        ]
    )


@dataclass(frozen=True)
class DocCommand:
    source: Path
    raw: str  # the command text as shown in the docs, comments/continuations resolved
    stdin: str | None  # payload to pipe in, for the `echo '...' | seohead ...` form


def _strip_comment_for_balance(line: str) -> str:
    """The comment-free text of one line, used only to decide whether the
    accumulated buffer's quotes still balance.

    A ``#`` cannot open a shell string, so a whole-line comment contributes
    nothing to the count, and a trailing ``# ...`` annotation is stripped the
    same way ``_strip_comment`` strips it from the final command text. A line
    whose quote is still open — folded in from an earlier continuation, so
    shlex can't parse it on its own — is passed through unchanged: its quote
    characters are exactly what is supposed to keep the buffer open.
    """
    if line.strip().startswith("#"):
        return ""
    try:
        return shlex.join(shlex.split(line, comments=True))
    except ValueError:
        return line


def _join_continuations(block: str) -> list[str]:
    """Merge a fenced block's lines into logical commands.

    Two Markdown conventions both split one command across lines: an explicit
    trailing ``\\`` (shell continuation), and a quoted JSON literal that simply
    wraps without one (readable in the doc, still one shell token once quoted).
    Both are done accumulating once the buffer has no trailing backslash and its
    quotes balance — where "its quotes" means the buffer with comments already
    stripped out, so an apostrophe in a ``# ...`` annotation can never hold a
    block open (issue #388).
    """
    logical: list[str] = []
    buf = ""
    quote_check = ""
    for line in block.splitlines():
        buf = f"{buf}\n{line}" if buf else line
        piece = _strip_comment_for_balance(line)
        quote_check = f"{quote_check}\n{piece}" if quote_check else piece
        if buf.rstrip().endswith("\\"):
            buf = buf.rstrip()[:-1]
            continue
        if quote_check.count("'") % 2 or quote_check.count('"') % 2:
            continue  # quotes still open; fold in the next line
        logical.append(buf)
        buf = ""
        quote_check = ""
    if buf:
        logical.append(buf)
    return logical


def _iter_fenced_blocks(text: str) -> list[str]:
    """Every fenced code block's content, matched by its own opener and closer.

    A closing fence is the same character as its opener, at least as many of
    them, and nothing else on the line (CommonMark's rule) — regardless of what
    info string, if any, followed the opener. That means a ``json`` or ``text``
    block closes on its own closer rather than leaking into the next block, so
    the bash example after it is never swallowed (issue #129).
    """
    lines = text.splitlines()
    blocks: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        opener = _FENCE_OPEN_RE.match(lines[i])
        if not opener:
            i += 1
            continue
        fence_char, fence_len = opener.group(2)[0], len(opener.group(2))
        closer_re = re.compile(rf"^[ \t]*{re.escape(fence_char)}{{{fence_len},}}[ \t]*$")
        i += 1
        start = i
        while i < n and not closer_re.match(lines[i]):
            i += 1
        blocks.append("\n".join(lines[start:i]))
        i += 1  # past the closer (or past EOF for an unterminated fence)
    return blocks


def _strip_comment(line: str) -> str:
    """Drop a trailing ``# ...`` annotation, respecting quotes via shlex."""
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError:
        return line.split(" #", 1)[0].rstrip()
    return shlex.join(tokens)


def extract_commands(root: Path) -> list[DocCommand]:
    """Every documented ``seohead`` invocation, one entry per logical command line."""
    commands: list[DocCommand] = []
    for path in doc_files(root):
        text = path.read_text(encoding="utf-8")
        for block in _iter_fenced_blocks(text):
            for line in _join_continuations(block):
                candidate = line.strip()
                if candidate.startswith("$ "):
                    candidate = candidate[2:].strip()
                echo_match = ECHO_PIPE_RE.match(candidate)
                if echo_match:
                    rest = _strip_comment("seohead " + echo_match.group("rest"))
                    commands.append(
                        DocCommand(source=path, raw=rest, stdin=echo_match.group("payload"))
                    )
                    continue
                if not candidate.startswith("seohead "):
                    continue
                stripped = _strip_comment(candidate)
                if stripped:
                    commands.append(DocCommand(source=path, raw=stripped, stdin=None))
    return commands


def to_argv(raw: str) -> list[str]:
    """The command text (``seohead ...``) as an argv list, ``seohead`` itself dropped.

    A trailing ``> file`` shown for readability (redirecting stdout in a shell) is not
    a CLI argument; it is dropped rather than passed through as a bogus positional.
    """
    tokens = shlex.split(raw)[1:]
    if ">" in tokens:
        tokens = tokens[: tokens.index(">")]
    return tokens
