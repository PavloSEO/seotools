"""Two independent MCP-boundary gaps where a failed/unscored result read as clean.

#439: seo_parse's isError must reflect total per-URL failure, not just a (nonexistent)
top-level ``ok`` key on the ParseManyResult it forwards.
#438: sf_audit_summary must surface health_score_reason when health_score is null, so a
caller reading only the summary cannot mistake "could not be scored" for "nothing found".
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from seohead.servers.mcp_server import _all_parse_results_failed, build_server
from seohead.servers.sf_mcp import build_server as build_sf_server


def _tool(server, name):
    return next(t for t in server._tool_manager.list_tools() if t.name == name)


def test_seo_parse_all_urls_failed_sets_iserror(monkeypatch):
    def fake_parse(**kwargs):
        return {
            "count": 1,
            "results": [{"ok": False, "url": "http://127.0.0.1:9/nope", "error": "blocked"}],
        }

    monkeypatch.setattr("seohead.servers.handlers.parse", fake_parse)
    tool = _tool(build_server(), "seo_parse")

    with pytest.raises(ToolError):
        asyncio.run(tool.run({"url": "http://127.0.0.1:9/nope"}))


def test_seo_parse_all_urls_failed_multi_url_sets_iserror(monkeypatch):
    def fake_parse(**kwargs):
        return {
            "count": 2,
            "results": [
                {"ok": False, "url": "http://a/", "error": "blocked"},
                {"ok": False, "url": "http://b/", "error": "blocked"},
            ],
        }

    monkeypatch.setattr("seohead.servers.handlers.parse", fake_parse)
    tool = _tool(build_server(), "seo_parse")

    with pytest.raises(ToolError):
        asyncio.run(tool.run({"urls": ["http://a/", "http://b/"]}))


def test_seo_parse_success_does_not_set_iserror(monkeypatch):
    """Negative control: a URL that fetches successfully must still return normally."""

    def fake_parse(**kwargs):
        return {"count": 1, "results": [{"ok": True, "url": "http://good/", "meta": {}}]}

    monkeypatch.setattr("seohead.servers.handlers.parse", fake_parse)
    tool = _tool(build_server(), "seo_parse")

    result = asyncio.run(tool.run({"url": "http://good/"}))
    assert result["results"][0]["ok"] is True


def test_seo_parse_partial_failure_stays_a_normal_result(monkeypatch):
    """A mix of ok/failed URLs is debatable per the issue, and must remain a normal result
    with the per-item errors still visible, not turned into an error."""

    def fake_parse(**kwargs):
        return {
            "count": 2,
            "results": [
                {"ok": True, "url": "http://good/", "meta": {}},
                {"ok": False, "url": "http://bad/", "error": "blocked"},
            ],
        }

    monkeypatch.setattr("seohead.servers.handlers.parse", fake_parse)
    tool = _tool(build_server(), "seo_parse")

    result = asyncio.run(tool.run({"urls": ["http://good/", "http://bad/"]}))
    assert result["count"] == 2
    assert result["results"][1]["ok"] is False


def test_all_parse_results_failed_helper_unit():
    assert _all_parse_results_failed({"count": 1, "results": [{"ok": False}]}) is True
    assert _all_parse_results_failed({"count": 0, "results": []}) is False
    assert _all_parse_results_failed({"ok": False}) is False
    assert _all_parse_results_failed("not-a-dict") is False


def test_sf_audit_summary_surfaces_reason_when_score_is_null(tmp_path):
    data = {
        "run": {"project": "demo"},
        "summary": {
            "health_score": None,
            "health_score_reason": "requires_rendering: JS rendering needed to trust this crawl",
            "by_severity": {"critical": 3, "warning": 1, "notice": 0},
            "by_check": {"BROKEN_LINK": 3},
            "sitemap": {"urls_in_sitemap": 100},
        },
        "issues": [],
    }
    p = tmp_path / "audit.json"
    p.write_text(json.dumps(data))

    tool = _tool(build_sf_server(), "sf_audit_summary")
    result = asyncio.run(tool.run({"json_path": str(p)}))

    assert result["health_score"] is None
    assert result["health_score_reason"] == data["summary"]["health_score_reason"]


def test_sf_audit_summary_normal_score_unchanged(tmp_path):
    """Negative control: a normal numeric score with no reason key must not gain one."""
    data = {
        "run": {"project": "demo"},
        "summary": {
            "health_score": 87,
            "by_severity": {"critical": 0, "warning": 1, "notice": 0},
            "by_check": {},
            "sitemap": {},
        },
        "issues": [],
    }
    p = tmp_path / "audit.json"
    p.write_text(json.dumps(data))

    tool = _tool(build_sf_server(), "sf_audit_summary")
    result = asyncio.run(tool.run({"json_path": str(p)}))

    assert result["health_score"] == 87
    assert "health_score_reason" not in result
    assert "health_score_basis" not in result
    assert "health_score_scope" not in result
