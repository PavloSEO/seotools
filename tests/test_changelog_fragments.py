"""The changelog is assembled from one file per change, so branches cannot collide on it.

Every branch used to add its entry at the top of ``## Unreleased``, which made that one line
the file's contention point and produced a hand-resolution per pull request whose answer was
always "keep our block, then theirs" (issue #638). Entries now live one per file under
``changelog.d/``, named for the issue they close, and ``scripts/build_changelog.py`` folds them
in at release time. Two branches therefore write two different files and never meet.

The risk in that move is not the assembly, it is the gates: several read ``CHANGELOG.md``, and
a gate that silently stops checking anything is a worse outcome than the conflicts it replaced.
Each of those answers is pinned here, beside the ordering, idempotency and naming rules, and a
real two-branch git merge that proves the conflict is gone rather than assuming it.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

import tests.test_doc_counts as doc_counts
import tests.test_docs_drift as docs_drift
from scripts.build_changelog import (
    END_MARKER,
    FRAGMENT_NAME_RE,
    START_MARKER,
    assemble,
    fragment_paths,
    render,
)
from scripts.doc_commands import doc_files

ROOT = pathlib.Path(__file__).resolve().parents[1]
FRAGMENT_DIR = ROOT / "changelog.d"
CHANGELOG = ROOT / "CHANGELOG.md"


def _write(directory: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def _git(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "gate",
            "GIT_AUTHOR_EMAIL": "gate@example.com",
            "GIT_COMMITTER_NAME": "gate",
            "GIT_COMMITTER_EMAIL": "gate@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
    )


# --- the fragments this repository actually carries ------------------------------------------


def test_every_fragment_is_named_for_something_unique_to_one_branch():
    """An issue number is the key that cannot collide: two branches are two issues. A change
    closing nothing says so in its name rather than picking a bare word that another branch
    might pick too."""
    bad = [p.name for p in FRAGMENT_DIR.glob("*.md") if not FRAGMENT_NAME_RE.match(p.name)]
    assert not bad, (
        f"fragment names must be <issue>.md, <issue>-<slug>.md or no-issue-<slug>.md: {bad}"
    )


def test_every_fragment_is_a_changelog_entry_and_nothing_else():
    """A fragment is spliced in verbatim, so anything that is not a top-level bullet -- a
    heading, a title line, front matter -- would land inside ``## Unreleased`` and break the
    section it was folded into."""
    bad = []
    for path in fragment_paths(FRAGMENT_DIR):
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines or not lines[0].startswith("- "):
            bad.append(f"{path.name}: does not start with a top-level '- ' bullet")
        if any(line.startswith("#") for line in lines):
            bad.append(f"{path.name}: carries a heading")
    assert not bad, bad


def test_the_changelog_carries_the_assembly_markers_in_order():
    text = CHANGELOG.read_text(encoding="utf-8")
    assert text.index(START_MARKER) < text.index(END_MARKER)
    assert text.index("## Unreleased") < text.index(START_MARKER)


def test_the_real_fragments_assemble_into_the_real_changelog_unchanged():
    """The end-to-end path, on this repository's own files: every fragment's prose reaches
    ``## Unreleased`` byte for byte. A fragment carries multi-paragraph prose bullets, not a
    one-line summary, and reformatting one on the way in would silently rewrite the entry."""
    paths = fragment_paths(FRAGMENT_DIR)
    assert paths, "changelog.d/ is empty; this pipeline has nothing to prove"
    assembled = assemble(CHANGELOG.read_text(encoding="utf-8"), paths)
    section = assembled.split(START_MARKER, 1)[1].split(END_MARKER, 1)[0]
    for path in paths:
        assert path.read_text(encoding="utf-8").strip("\n") in section, path.name


def test_assembly_leaves_everything_outside_the_markers_byte_for_byte():
    """Released sections, and the hand-written entries that were already under
    ``## Unreleased`` when this pipeline arrived, are history: assembly must not touch them."""
    current = CHANGELOG.read_text(encoding="utf-8")
    assembled = assemble(current, fragment_paths(FRAGMENT_DIR))
    assert assembled.split(START_MARKER, 1)[0] == current.split(START_MARKER, 1)[0]
    assert assembled.split(END_MARKER, 1)[1] == current.split(END_MARKER, 1)[1]
    assert "## 3.0.0 — first public snapshot" in assembled


# --- ordering and idempotency -----------------------------------------------------------------


def test_order_is_the_issue_number_not_whatever_the_filesystem_returns(tmp_path):
    """``Path.glob`` yields directory order, which differs between filesystems and between two
    checkouts of the same commit. The order is computed from the names instead: issues newest
    first, then the issue-less ones alphabetically."""
    for name in ("no-issue-zebra.md", "9.md", "638.md", "no-issue-alpha.md", "1200.md", "638-b.md"):
        _write(tmp_path, name, f"- {name}\n")
    assert [p.name for p in fragment_paths(tmp_path)] == [
        "1200.md",
        "638-b.md",
        "638.md",
        "9.md",
        "no-issue-alpha.md",
        "no-issue-zebra.md",
    ]


def test_assembling_twice_produces_the_same_file(tmp_path):
    """Assembly replaces the whole marked region rather than inserting into it, so a second run
    -- by a nervous maintainer, or by a release script run again after a failure -- cannot
    duplicate an entry."""
    _write(tmp_path, "638.md", "- An entry.\n  Its second line.\n")
    _write(tmp_path, "700.md", "- Another entry.\n")
    base = f"# Changelog\n\n## Unreleased\n\n{START_MARKER}\n{END_MARKER}\n\n## 1.0.0\n\n- Old.\n"

    once = assemble(base, fragment_paths(tmp_path))
    twice = assemble(once, fragment_paths(tmp_path))
    assert once == twice
    assert once.count("- An entry.") == 1
    assert once.count("- Another entry.") == 1


def test_pruned_fragments_leave_the_assembled_changelog_alone(tmp_path):
    """``--prune`` deletes the fragments after folding them in. Re-running assembly on the
    pruned tree must not then empty the section it just wrote -- it does, and that is correct:
    the release step commits the assembled file and the empty directory together."""
    _write(tmp_path, "638.md", "- An entry.\n")
    base = f"## Unreleased\n\n{START_MARKER}\n{END_MARKER}\n\n## 1.0.0\n"
    filled = assemble(base, fragment_paths(tmp_path))
    assert "- An entry." in filled

    (tmp_path / "638.md").unlink()
    assert assemble(filled, fragment_paths(tmp_path)) == base


def test_a_changelog_without_markers_refuses_rather_than_guessing(tmp_path):
    _write(tmp_path, "638.md", "- An entry.\n")
    with pytest.raises(ValueError, match="must carry"):
        assemble("## Unreleased\n\n- Something.\n", fragment_paths(tmp_path))


def test_render_separates_entries_with_one_blank_line(tmp_path):
    _write(tmp_path, "700.md", "\n- First.\n  Wrapped.\n\n")
    _write(tmp_path, "638.md", "- Second.\n")
    assert render(fragment_paths(tmp_path)) == "- First.\n  Wrapped.\n\n- Second."


# --- the reason to go slowly: every doc gate that reads the changelog --------------------------


def test_the_english_and_command_gates_read_the_fragments():
    """``tests/test_docs_drift.py`` listed ``CHANGELOG.md`` in ``PUBLIC_MARKDOWN``, which is
    what subjects it to the Cyrillic gate, the "references only existing commands" gate and the
    stale-tool-count gate. Moving entries out of that file must not move them out of those."""
    public = set(docs_drift.PUBLIC_MARKDOWN)
    assert set(fragment_paths(FRAGMENT_DIR)) <= public
    assert CHANGELOG in public


def test_the_live_count_gates_skip_a_fragment_for_the_changelog_s_own_reason():
    """The two count-drift gates in ``tests/test_docs_drift.py`` skip the changelog because an
    entry states what was true when it was written. A fragment is that entry before it is
    folded in, so it gets the same exemption -- and nothing else does."""
    assert docs_drift._records_a_moment_in_time(CHANGELOG)
    assert docs_drift._records_a_moment_in_time(FRAGMENT_DIR / "638.md")
    assert not docs_drift._records_a_moment_in_time(ROOT / "README.md")
    assert not docs_drift._records_a_moment_in_time(ROOT / "docs" / "TOOLS.md")


def test_the_live_count_scanner_does_not_reach_the_fragments():
    """``tests/test_doc_counts.py`` excludes ``CHANGELOG.md`` by name and its file list stops at
    the top level and ``docs/``, so a fragment is outside it for the same reason. Pinned so a
    later widening of that glob cannot start failing every historical entry."""
    assert CHANGELOG not in doc_counts.DOCS
    assert not [p for p in doc_counts.DOCS if p.parent.name == "changelog.d"]


def test_the_documented_command_gates_do_not_execute_fragment_fences():
    """``scripts/doc_commands.py`` never listed ``CHANGELOG.md``: a changelog quotes commands as
    they were, and CI executing a historical invocation would fail on a flag that has since been
    renamed. Fragments inherit that, deliberately -- so ``tests/test_doc_commands.py`` and
    ``tests/test_docs_commands_execute.py`` are unaffected by this move."""
    listed = set(doc_files(ROOT))
    assert CHANGELOG not in listed
    assert not [p for p in listed if p.parent.name == "changelog.d"]


def test_the_examples_gate_never_read_the_changelog():
    """The last member of the doc-gate family, checked rather than assumed: it regenerates
    ``examples/`` and compares, and names no changelog at all."""
    source = (ROOT / "tests" / "test_examples_gate.py").read_text(encoding="utf-8")
    assert "CHANGELOG" not in source
    assert "changelog.d" not in source


# --- the thing the issue was actually about ---------------------------------------------------


def test_two_concurrent_branches_adding_an_entry_merge_without_a_conflict(tmp_path):
    """The proof, not a claim: two branches off one base, each adding its own entry, merged.

    The first commit reproduces the old shape -- both branches editing the top of
    ``## Unreleased`` -- and asserts the merge fails, so a green result here cannot come from a
    merge that would have succeeded anyway. The second does it the new way and asserts it
    succeeds with both entries intact, including the identical blank line inside each that
    ``merge=union`` silently deduplicated.
    """
    if _git(tmp_path, "--version").returncode != 0:
        pytest.skip("git is not available")

    def repo(name: str, base_files: dict[str, str], a: dict[str, str], b: dict[str, str]) -> int:
        root = tmp_path / name
        root.mkdir()
        assert _git(root, "init", "-q", "-b", "main").returncode == 0
        for rel, body in base_files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        _git(root, "checkout", "-qb", "branch-a")
        for rel, body in a.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "a")
        _git(root, "checkout", "-q", "main")
        _git(root, "checkout", "-qb", "branch-b")
        for rel, body in b.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body, encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "b")
        _git(root, "checkout", "-q", "branch-a")
        return _git(root, "merge", "--no-edit", "branch-b").returncode

    entry_a = "- Entry from branch A.\n\n  A second paragraph.\n"
    entry_b = "- Entry from branch B.\n\n  A second paragraph.\n"
    old_base = "# Changelog\n\n## Unreleased\n\n- An older entry.\n"

    assert (
        repo(
            "old-shape",
            {"CHANGELOG.md": old_base},
            {"CHANGELOG.md": old_base.replace("\n- An older", f"\n{entry_a}\n- An older")},
            {"CHANGELOG.md": old_base.replace("\n- An older", f"\n{entry_b}\n- An older")},
        )
        != 0
    ), "the old shape must still conflict, or this test proves nothing"

    new_base = {
        "CHANGELOG.md": f"# Changelog\n\n## Unreleased\n\n{START_MARKER}\n{END_MARKER}\n",
        "changelog.d/1.md": "- An older entry.\n",
    }
    root = tmp_path / "fragments"
    assert (
        repo(
            "fragments", new_base, {"changelog.d/700.md": entry_a}, {"changelog.d/701.md": entry_b}
        )
        == 0
    ), "two branches adding one fragment each must merge cleanly"

    merged = fragment_paths(root / "changelog.d")
    assert [p.name for p in merged] == ["701.md", "700.md", "1.md"]
    body = render(merged)
    assert entry_a.strip("\n") in body
    assert entry_b.strip("\n") in body
    assert body.count("  A second paragraph.") == 2
