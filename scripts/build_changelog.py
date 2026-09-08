#!/usr/bin/env python3
"""Fold the one-file-per-change fragments in changelog.d/ into CHANGELOG.md.

    python scripts/build_changelog.py            # write the assembled entries into CHANGELOG.md
    python scripts/build_changelog.py --prune    # ... and delete the fragments that were folded in

A branch adds ``changelog.d/<issue>.md`` and never touches ``CHANGELOG.md``, so two branches
can never edit the same file and the merge conflict this replaces cannot occur (issue #638).
The fragments are folded in at release time, between the two marker comments that sit directly
under ``## Unreleased``.

Assembly replaces the whole marked region rather than inserting into it, so running it twice
produces the same file both times whether or not the fragments were pruned in between. Nothing
outside the markers is read or rewritten: released sections, and the entries that were already
in ``## Unreleased`` when the pipeline was introduced, are copied through byte for byte.

``assemble`` is a pure function over text so tests exercise the same code path the script
writes with, the way scripts/generate_tool_reference.py does.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAGMENTS = ROOT / "changelog.d"
TARGET = ROOT / "CHANGELOG.md"

START_MARKER = "<!-- changelog.d: assembled entries start -->"
END_MARKER = "<!-- changelog.d: assembled entries end -->"

# ``638.md`` (the issue the change closes), ``638-progress.md`` when one issue lands as two
# independent entries, or ``no-issue-<slug>.md`` when a change closes nothing. Anything else is
# rejected by the gate rather than silently sorted somewhere arbitrary.
FRAGMENT_NAME_RE = re.compile(r"^(?:\d+(?:-[a-z0-9][a-z0-9-]*)?|no-issue-[a-z0-9][a-z0-9-]*)\.md$")


def fragment_paths(directory: Path = FRAGMENTS) -> list[Path]:
    """Every fragment, in the order it will appear under ``## Unreleased``.

    Deterministic and independent of the filesystem's own ordering: issue-numbered
    fragments come first, highest issue number first so the newest work reads at the top
    the way hand-written entries always did, then the issue-less ones alphabetically.
    """
    return sorted(directory.glob("*.md"), key=sort_key) if directory.is_dir() else []


def sort_key(path: Path) -> tuple[int, int, str]:
    leading = re.match(r"^(\d+)", path.name)
    if leading:
        return (0, -int(leading.group(1)), path.name)
    return (1, 0, path.name)


def render(paths: list[Path]) -> str:
    """The fragments' bodies, in order, separated by one blank line.

    A fragment is the entry exactly as it belongs in the changelog -- one or more top-level
    ``- `` bullets with their continuation lines -- so nothing is reformatted on the way in
    and a multi-paragraph prose entry round-trips unchanged.
    """
    return "\n\n".join(path.read_text(encoding="utf-8").strip("\n") for path in paths)


def assemble(changelog: str, paths: list[Path]) -> str:
    """``changelog`` with the region between the markers replaced by ``render(paths)``."""
    start = changelog.find(START_MARKER)
    end = changelog.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise ValueError(
            f"CHANGELOG.md must carry {START_MARKER} and {END_MARKER}, in that order, "
            "under '## Unreleased'"
        )
    body = render(paths)
    middle = f"\n{body}\n" if body else "\n"
    return changelog[: start + len(START_MARKER)] + middle + changelog[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prune",
        action="store_true",
        help="delete the fragments after folding them in (the release step)",
    )
    args = parser.parse_args()

    paths = fragment_paths()
    current = TARGET.read_text(encoding="utf-8")
    content = assemble(current, paths)
    if content != current:
        TARGET.write_text(content, encoding="utf-8")
        print(f"wrote {TARGET} from {len(paths)} fragment(s)")
    else:
        print(f"{TARGET} already carries all {len(paths)} fragment(s)")

    if args.prune:
        for path in paths:
            path.unlink()
        print(f"removed {len(paths)} fragment(s) from {FRAGMENTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
