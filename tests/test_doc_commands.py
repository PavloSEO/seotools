"""Direct tests for scripts/doc_commands.py's fence extraction (issue #129).

A single non-greedy regex over ```` ``` ```` delimiters cannot tell an opening
fence from a closing one once an info string it does not recognise (``json``,
``text``, ``xml``, ...) sits before a bash block: it reads that block's own
closer as an opener and swallows the bash block whole, up to *its* closer.
These tests pin two things: a regression fixture that exercises the bug
directly, and a floor on how many commands the real documentation set
extracts, so a future change that quietly drops examples fails loudly instead
of shipping.
"""

from __future__ import annotations

from pathlib import Path

from scripts.doc_commands import extract_commands

ROOT = Path(__file__).resolve().parent.parent

# The real corpus must extract at least this many `seohead ...` invocations.
# Fixing issue #129 raised the count from 356 to 394 by un-hiding 38 commands
# that a preceding json/text/xml fence had swallowed; a drop below this floor
# means the extractor is silently losing examples again.
MINIMUM_EXTRACTED_COMMANDS = 394


def _write_minimal_root(tmp_path: Path, readme_text: str) -> Path:
    """The files doc_files() always looks for, so extract_commands() doesn't
    fail on a missing README/AGENTS/CONTRIBUTING before it even reaches the
    fixture content under test."""
    (tmp_path / "README.md").write_text(readme_text, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("", encoding="utf-8")
    (tmp_path / "CONTRIBUTING.md").write_text("", encoding="utf-8")
    return tmp_path


def test_command_after_a_json_fence_is_extracted(tmp_path):
    """Regression fixture for issue #129. Fails against the pre-fix
    ``FENCE_RE`` (confirmed by hand: stashing the fix and re-running this
    fixture extracts zero commands), which reads the json fence's closer as
    the opener of a bash block and swallows the real bash block that follows
    it up to its own closer."""
    doc = (
        "# Example\n\n"
        "```json\n"
        '{"redirects": [{"from": "/a", "to": "/b"}]}\n'
        "```\n\n"
        "```bash\n"
        "seohead redirects-generate --input '{}' --format nginx\n"
        "```\n"
    )
    root = _write_minimal_root(tmp_path, doc)
    commands = extract_commands(root)
    assert [c.raw for c in commands] == ["seohead redirects-generate --input '{}' --format nginx"]


def test_common_non_shell_info_strings_do_not_hide_the_command_after_them(tmp_path):
    """The bug is not specific to `json`: any info string absent from an
    allow-list breaks the old regex the same way. Fixed properly (scanning
    real openers/closers) needs no such list at all."""
    for lang in ("text", "xml", "html", "yaml", "console", "diff"):
        doc = (
            f"```{lang}\n"
            "some non-command content\n"
            "```\n\n"
            "```bash\n"
            "seohead robots-check --url https://example.com\n"
            "```\n"
        )
        root = _write_minimal_root(tmp_path, doc)
        commands = extract_commands(root)
        assert [c.raw for c in commands] == ["seohead robots-check --url https://example.com"], (
            f"fence info string {lang!r} hid the command after it"
        )


def test_apostrophe_in_comment_does_not_hide_the_command_after_it(tmp_path):
    """Regression fixture for issue #388. ``_join_continuations`` used to count
    quotes on the raw accumulated buffer, comments included, so a single
    apostrophe in a ``# ...`` annotation between two commands left the quote
    count odd forever and folded every later line of the block into one
    buffer that no longer started with ``seohead``, hiding the second
    command entirely."""
    doc = (
        "```bash\n"
        "seohead parse --url https://example.com\n"
        "# this comment's apostrophe should not hide what follows\n"
        "seohead headers-check --url https://example.com\n"
        "```\n"
    )
    root = _write_minimal_root(tmp_path, doc)
    commands = extract_commands(root)
    assert [c.raw for c in commands] == [
        "seohead parse --url https://example.com",
        "seohead headers-check --url https://example.com",
    ]


def test_genuine_multiline_quoted_argument_still_folds(tmp_path):
    """Control for issue #388's fix: a real quoted JSON literal that wraps
    across lines without a trailing backslash must still be recognised as one
    logical command, not split by the comment-stripping change."""
    doc = (
        "```bash\n"
        "seohead redirects-generate --input '{\n"
        '"redirects": [{"from": "/a", "to": "/b"}]}\' --format nginx\n'
        "```\n"
    )
    root = _write_minimal_root(tmp_path, doc)
    commands = extract_commands(root)
    assert [c.raw for c in commands] == [
        "seohead redirects-generate --input "
        '\'{\n"redirects": [{"from": "/a", "to": "/b"}]}\' --format nginx'
    ]


def test_real_documentation_extracts_at_least_the_pinned_floor():
    commands = extract_commands(ROOT)
    assert len(commands) >= MINIMUM_EXTRACTED_COMMANDS, (
        f"only {len(commands)} commands extracted from the real docs; "
        f"expected at least {MINIMUM_EXTRACTED_COMMANDS} (issue #129 regression)"
    )
