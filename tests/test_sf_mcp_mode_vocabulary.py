"""sf_mcp's accepted input modes must not drift from the audit core's live modes.

Previously ``sf_mcp.VALID_MODES`` (seohead/servers/sf_mcp.py) manually repeated
``audit.CRAWL_MODES`` (seohead/sf/core/audit.py) plus the MCP-only
``"parse-exports"`` value as a second, hand-written literal. They agreed at
the time, but nothing tied them together: in a temporary offline copy, adding
a new live mode to ``CRAWL_MODES`` alone made the audit core accept it while
``sf_audit_run`` went on rejecting it before ``run_audit`` ever ran -- and the
existing selected interface tests kept passing, because none of them derived
one vocabulary from the other.

The fix declares the vocabulary once, as ``audit.INPUT_MODES``, and has
``sf_mcp`` import that value rather than repeat it. This test proves the
import is real (identity, not a coincidentally-equal copy) and reproduces the
original drift scenario against a reloaded copy of both modules to show a
core-only mode addition now reaches the MCP boundary without a second edit.
"""

from __future__ import annotations

import importlib

from seohead.servers import sf_mcp
from seohead.sf.core import audit


def test_sf_mcp_valid_modes_is_the_same_object_as_audit_input_modes():
    """Not just equal -- literally the same set, so it cannot be edited out of sync."""
    assert sf_mcp.VALID_MODES is audit.INPUT_MODES


def test_input_modes_equals_crawl_modes_plus_parse_exports():
    assert audit.CRAWL_MODES | {"parse-exports"} == audit.INPUT_MODES


def test_unknown_mode_still_raises_value_error():
    import pytest

    with pytest.raises(ValueError, match="mode must be one of"):
        sf_mcp._do_run(mode="not-a-real-mode", source="whatever")


def test_parse_exports_routing_untouched(monkeypatch, tmp_path):
    """Negative control: parse-exports must still take the exports_dir branch, not source/output_dir."""
    calls: list[dict] = []

    def fake_run_audit(**kwargs):
        calls.append(kwargs)
        return type("R", (), {"summary": {}})()

    monkeypatch.setattr(sf_mcp, "run_audit", fake_run_audit)
    monkeypatch.setattr(sf_mcp, "write_json", lambda result, path: path)
    monkeypatch.setattr(sf_mcp, "write_markdown", lambda result, path: path)

    sf_mcp._do_run(mode="parse-exports", source=str(tmp_path), out=str(tmp_path / "out"))

    assert len(calls) == 1
    assert calls[0]["exports_dir"] == str(tmp_path)
    assert "source" not in calls[0]
    assert "output_dir" not in calls[0]


def test_gate_catches_a_core_only_mode_addition(monkeypatch):
    """Mutation test: a new live mode added only to the core's vocabulary.

    Reproduces the original gap offline, without touching either real file:
    inject an extra mode into the already-imported ``audit.INPUT_MODES``
    (mirroring "someone edited audit.py's CRAWL_MODES and a new mode landed
    in INPUT_MODES with it"), then reload ``sf_mcp`` so its
    ``from ... import INPUT_MODES`` statement runs again against the mutated
    value. Because sf_mcp now imports the vocabulary instead of repeating a
    literal, the reload picks the new mode up with no second edit -- proving
    the import, not a hand-kept copy, is what MCP validates against.
    """
    monkeypatch.setattr(audit, "INPUT_MODES", audit.CRAWL_MODES | {"capture"})

    reloaded_sf_mcp = importlib.reload(sf_mcp)
    try:
        assert "capture" in reloaded_sf_mcp.VALID_MODES
    finally:
        # Undo the patch now (idempotent -- pytest's own teardown will also
        # call this) and reload sf_mcp once more so it re-binds VALID_MODES
        # to the real, unpatched audit.INPUT_MODES before other tests run.
        monkeypatch.undo()
        importlib.reload(sf_mcp)
