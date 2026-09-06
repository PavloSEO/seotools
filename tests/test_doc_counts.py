"""Every number a document states about the registry must match the registry.

tests/test_docs_drift.py asserts specific literal substrings, so a count in a
file nobody thought to list drifts silently — and several did: the test-count
badge said 672 when the suite had grown past a thousand, three documents still
described 42 tools, and README's own category table summed to two fewer than
the total printed above it.

This scans every Markdown file instead of a chosen few, so a new document with
a stale number fails on the way in rather than years later.
"""

from __future__ import annotations

import asyncio
import functools
import pathlib
import re
import subprocess
import sys

import pytest

from seohead.cli import COMMANDS
from seohead.servers.handlers import HANDLERS
from seohead.sf.core.registry import CHECKS

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = sorted(
    p
    for p in list(ROOT.glob("*.md")) + list((ROOT / "docs").glob("**/*.md"))
    if p.name != "CHANGELOG.md"  # a changelog records what was true at the time
)


def _mcp_tool_names() -> set[str]:
    from seohead.servers.mcp_server import build_server

    return {tool.name for tool in asyncio.run(build_server().list_tools())}


def _live_counts() -> dict[str, int]:
    names = _mcp_tool_names()
    return {
        "commands": len(COMMANDS),
        "handlers": len(HANDLERS),
        "core tools": len(COMMANDS),
        "callable tools": len(names),
        "checks": len(CHECKS),
    }


@functools.lru_cache(maxsize=1)
def _collected_tests() -> int:
    """How many tests the suite has, as pytest counts them.

    Collected, not the number of ``def test_`` lines: a parametrised function
    is one definition and many tests, and the documented figure is the one a
    reader sees at the end of a run. Collection is a separate process so this
    cannot recurse into the session that is asking.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    if not match:
        pytest.skip("could not collect the suite to count it")
    return int(match.group(1))


# What a number in front of these words is claiming. Ordered longest first so
# "core CLI commands" is not read as "commands" with a stray prefix.
CLAIMS: tuple[tuple[str, str], ...] = (
    (r"core CLI commands and MCP tools", "core tools"),
    (r"callable tools", "callable tools"),
    (r"shared `?seo_\*`? handlers", "handlers"),
    (r"shared handlers", "handlers"),
    (r"`?seo_\*`? tools", "core tools"),
    (r"core tools", "core tools"),
    (r"audit checks", "checks"),
    (r"checks", "checks"),
    (r"handlers", "handlers"),
    (r"commands", "commands"),
)

# Numbers that are deliberately not a live count: a historical note, a worked
# example, or a figure about something outside this registry.
EXEMPT = (
    "96 checks (the count before",
    "of the 320",
    "~320",
    # CHECKLIST_AUDIT.md is a frozen snapshot of the registry as it stood when
    # issue #30 was being worked, superseded by COVERAGE_SF_ISSUES.md — its own
    # header says so. The count it names is the one that was live *then*, not
    # a claim this suite should keep matching to the current registry. This
    # string had drifted to name a count ("121") the document no longer
    # contains, which meant the exemption was quietly matching nothing and the
    # line only kept passing because the live count still happened to equal
    # the frozen one -- exactly the silent failure this constant exists to
    # prevent (coverage-evidence, #385/#386).
    "139 checks today, up from 104",
)

# Documents claim "over 1100 offline tests" rather than an exact figure: an
# exact one is drift by construction, needing an edit in six files on every
# pull request that adds a test. A floor only has to hold, and the suite only
# grows — so it is checked as a floor.
_FLOOR_RE = re.compile(
    r"(?:over |\*\*over )(\d{3,5})(?:\+)?\s+(?:\*\*)?(?:offline )?tests|(\d{3,5})\+\s+tests|tests-(\d{3,5})%2B%20offline"
)


def _claims_in(text: str) -> list[tuple[int, str, str]]:
    out: list[tuple[int, str, str]] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if any(token in line for token in EXEMPT):
            continue
        for pattern, kind in CLAIMS:
            for match in re.finditer(rf"\b(\d{{2,4}})\s+(?:\*\*)?{pattern}", line):
                out.append((line_no, kind, match.group(1)))
                break
    return out


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_documented_counts_match_the_registry(path):
    live = _live_counts()
    wrong = [
        f"{path.relative_to(ROOT)}:{line} claims {value} {kind}, live value is {live[kind]}"
        for line, kind, value in _claims_in(path.read_text(encoding="utf-8"))
        if int(value) != live[kind]
    ]
    assert not wrong, "documented counts have drifted:\n  " + "\n  ".join(wrong)


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_claimed_test_floor_still_holds(path):
    collected = _collected_tests()
    for match in _FLOOR_RE.finditer(path.read_text(encoding="utf-8")):
        floor = int(next(g for g in match.groups() if g))
        assert collected >= floor, (
            f"{path.relative_to(ROOT)} claims over {floor} tests; only {collected} are collected"
        )


def test_the_scanner_finds_claims_at_all():
    # A regex that stops matching would make every assertion above vacuous.
    found = sum(len(_claims_in(p.read_text(encoding="utf-8"))) for p in DOCS)
    assert found > 15, f"only {found} count claims found; the patterns have gone stale"


def test_readme_category_table_sums_to_its_own_heading():
    # The table lists tools per layer under a heading that states the total.
    # They drifted apart over several additions, each correct on its own.
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    heading = re.search(r"###\s+(\d+)\s+core CLI commands and MCP tools", text)
    assert heading, "README no longer states a core tool count"
    rows = re.findall(r"^\|\s*[^|]+\|\s*(\d+)\s*\|", text, flags=re.M)
    assert rows, "README category table not found"
    assert sum(int(n) for n in rows) == int(heading.group(1)) == len(COMMANDS)
