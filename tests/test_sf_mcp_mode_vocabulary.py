"""sf_mcp's accepted input modes must not drift from the audit core's live modes.

``sf_mcp.VALID_MODES`` (seohead/servers/sf_mcp.py) manually repeats
``audit.CRAWL_MODES`` (seohead/sf/core/audit.py) plus the MCP-only
``"parse-exports"`` value. They agree today, but nothing ties them together:
in a temporary offline copy, adding a new live mode to ``CRAWL_MODES`` alone
made the audit core accept it while ``sf_audit_run`` went on rejecting it
before ``run_audit`` ever ran -- and the existing selected interface tests
kept passing, because none of them derive one vocabulary from the other.

This gate reads both live sets and asserts the relationship directly, then
proves with a synthetic copy of each set that the comparison actually
discriminates a genuine drift rather than happening to pass because the two
literals were kept in sync by hand.
"""

from __future__ import annotations

from seohead.servers.sf_mcp import VALID_MODES
from seohead.sf.core.audit import CRAWL_MODES

# The one MCP-only addition to the core's live-mode vocabulary: parsing
# pre-existing exports never touches the crawl core's live-mode branch.
_MCP_ONLY_MODES = {"parse-exports"}


def _agree(valid_modes: set[str], crawl_modes: set[str]) -> bool:
    return valid_modes == crawl_modes | _MCP_ONLY_MODES


def test_sf_mcp_valid_modes_equals_core_crawl_modes_plus_parse_exports():
    assert _agree(VALID_MODES, CRAWL_MODES), (
        f"sf_mcp.VALID_MODES={sorted(VALID_MODES)} no longer matches "
        f"audit.CRAWL_MODES={sorted(CRAWL_MODES)} plus {sorted(_MCP_ONLY_MODES)}"
    )


def test_gate_catches_a_core_only_mode_addition():
    """Mutation test: a new live mode added only to the core's CRAWL_MODES.

    Reproduces the gap from the issue offline, without touching the real
    modules: the MCP vocabulary a client actually sees stays the old, smaller
    set while the core would already accept the new mode.
    """
    core_modes_with_new_mode = CRAWL_MODES | {"capture"}
    assert not _agree(VALID_MODES, core_modes_with_new_mode), (
        "VALID_MODES must not silently agree with a CRAWL_MODES that grew a mode it doesn't have"
    )


def test_gate_stays_silent_when_both_sides_grow_together():
    """Negative control: adding the same mode to both sides must still agree."""
    grown_valid_modes = VALID_MODES | {"capture"}
    grown_core_modes = CRAWL_MODES | {"capture"}
    assert _agree(grown_valid_modes, grown_core_modes)
