"""MCP server integration test over real stdio.

Skipped automatically when the optional ``mcp`` SDK isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from seohead.servers.mcp_server import build_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


def test_all_mcp_tools_have_structured_schemas_and_safety_annotations():
    """Keep MCP clients informed about output shape, side effects, and network use."""
    tools = {tool.name: tool for tool in build_server()._tool_manager.list_tools()}

    assert len(tools) == 68
    assert all(tool.fn_metadata.output_schema for tool in tools.values())
    assert all(tool.annotations is not None for tool in tools.values())

    optimizer = tools["seo_images_optimize"].annotations
    assert optimizer.destructiveHint is True
    assert optimizer.readOnlyHint is False
    assert optimizer.openWorldHint is False

    # The only tool that writes to somebody else's service (#97). A caller decides
    # whether to ask a person first from these hints, so "submits URLs to four search
    # engines" must never read as a harmless read-only call. Idempotent because
    # resubmitting a URL is defined by the protocol to be safe; not destructive
    # because nothing is removed or overwritten.
    submit = tools["seo_indexnow_submit"].annotations
    assert submit.readOnlyHint is False
    assert submit.openWorldHint is True
    assert submit.idempotentHint is True
    assert submit.destructiveHint is False

    live_fetch = tools["seo_parse"].annotations
    assert live_fetch.readOnlyHint is True
    assert live_fetch.openWorldHint is True

    paid = tools["seo_google_keywords"].annotations
    assert paid.readOnlyHint is False
    assert paid.openWorldHint is True

    local_report = tools["seo_spend_report"].annotations
    assert local_report.readOnlyHint is True
    assert local_report.openWorldHint is False

    crawl = tools["sf_audit_run"].annotations
    assert crawl.readOnlyHint is False
    assert crawl.destructiveHint is False
    assert crawl.openWorldHint is True

    tasks = tools["sf_audit_tasks"].annotations
    assert tasks.readOnlyHint is False
    assert tasks.destructiveHint is False
    assert tasks.openWorldHint is False


def test_report_build_accepts_an_audit_document_or_json_path(monkeypatch):
    """The MCP schema must expose both input forms the shared handler supports (#247)."""
    received = []

    def fake_report_build(**kwargs):
        received.append(kwargs["audit"])
        return {"ok": True}

    monkeypatch.setattr("seohead.servers.handlers.report_build", fake_report_build)
    tool = next(
        tool
        for tool in build_server()._tool_manager.list_tools()
        if tool.name == "seo_report_build"
    )

    assert asyncio.run(tool.run({"audit": {"synthetic": True}})) == {"ok": True}
    assert asyncio.run(tool.run({"audit": "synthetic-audit.json"})) == {"ok": True}
    assert received == [{"synthetic": True}, "synthetic-audit.json"]


async def _call_over_stdio(tool: str, arguments: dict) -> object:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "seohead.servers.mcp_server"], cwd=ROOT
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return await session.call_tool(tool, arguments)


def test_a_handler_reported_failure_sets_iserror():
    """A tool whose handler returns ``ok: false`` (issue #155) must be distinguishable from
    success without the client having to inspect the payload — the same distinction the CLI
    makes with a non-zero exit (docs/USAGE.md)."""
    result = asyncio.run(_call_over_stdio("seo_ai_bots_check", {"url": "not a url"}))
    assert result.isError is True
    assert "could not be resolved" in result.content[0].text


def test_a_clean_result_does_not_set_iserror():
    result = asyncio.run(_call_over_stdio("seo_spend_report", {}))
    assert result.isError is False


def _payload(r):
    sc = getattr(r, "structuredContent", None)
    if isinstance(sc, dict):
        return sc["result"] if set(sc.keys()) == {"result"} else sc
    return json.loads(r.content[0].text)


async def _drive(exports_dir: str, out_dir: str) -> dict:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "seohead.servers.mcp_server"], cwd=ROOT
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = {t.name for t in (await session.list_tools()).tools}

        le = _payload(await session.call_tool("sf_list_exports", {"exports_dir": exports_dir}))
        run = _payload(
            await session.call_tool(
                "sf_audit_run",
                {"mode": "parse-exports", "input": exports_dir, "out": out_dir},
            )
        )
        summ = _payload(
            await session.call_tool("sf_audit_summary", {"json_path": run["json_path"]})
        )
        issues = _payload(
            await session.call_tool(
                "sf_audit_issues",
                {"json_path": run["json_path"], "check": "BROKEN_INTERNAL_LINK", "limit": 5},
            )
        )
        backlog = _payload(
            await session.call_tool(
                "sf_audit_tasks",
                {"json_path": run["json_path"], "out": os.path.join(out_dir, "tasks")},
            )
        )
        return {
            "tools": tools,
            "list_exports": le,
            "run": run,
            "summary": summ,
            "issues": issues,
            "tasks": backlog,
        }


def test_mcp_all_tools(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    for name in os.listdir(FIXTURES):
        shutil.copy(os.path.join(FIXTURES, name), exports / name)
    out = tmp_path / "out"

    res = asyncio.run(_drive(str(exports), str(out)))

    assert {
        "sf_audit_run",
        "sf_audit_summary",
        "sf_audit_issues",
        "sf_list_exports",
        "sf_audit_tasks",
    } <= res["tools"]
    # One connector exposes both crawl-audit and live-analysis tools.
    assert {"seo_parse", "seo_robots_check", "seo_headers_check"} <= res["tools"]
    assert "internal_all" in res["list_exports"]["found"]
    assert os.path.isfile(res["run"]["json_path"])
    assert os.path.isfile(res["run"]["md_path"])
    assert res["summary"]["by_severity"]["critical"] >= 1
    assert res["issues"][0]["check"] == "BROKEN_INTERNAL_LINK"
    assert len(res["issues"][0]["locations"]) == 2
    assert res["tasks"]["summary"]["tasks_total"] >= 1
    assert os.path.isfile(res["tasks"]["tasks_md"])


def test_crawl_site_omitted_overrides_forward_none(monkeypatch):
    """Issue #327: a config-only MCP call must not shadow config values with the wrapper's
    own concrete defaults -- omitted overrides have to reach the handler as ``None``, the
    same shape the CLI forwards for an unset flag, so ``crawl.settings.load`` can fall
    through to config/environment/defaults instead of being overridden silently."""
    received = []

    def fake_crawl_site(**kwargs):
        received.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("seohead.servers.handlers.crawl_site", fake_crawl_site)
    tool = next(
        tool for tool in build_server()._tool_manager.list_tools() if tool.name == "seo_crawl_site"
    )

    asyncio.run(tool.run({"url": "https://example.test/", "config": "crawl.json"}))

    forwarded = received[0]
    for key in ("max_urls", "max_depth", "min_delay", "robots", "concurrency"):
        assert forwarded[key] is None, f"{key} must default to None, not a concrete override"


def test_crawl_site_explicit_override_changes_only_that_setting(monkeypatch):
    """Positive control for #327: an explicit override still reaches the handler, and
    only that one setting -- the neighbouring overrides stay None."""
    received = []

    def fake_crawl_site(**kwargs):
        received.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("seohead.servers.handlers.crawl_site", fake_crawl_site)
    tool = next(
        tool for tool in build_server()._tool_manager.list_tools() if tool.name == "seo_crawl_site"
    )

    asyncio.run(tool.run({"url": "https://example.test/", "max_urls": 2}))

    forwarded = received[0]
    assert forwarded["max_urls"] == 2
    for key in ("max_depth", "min_delay", "robots", "concurrency"):
        assert forwarded[key] is None
