"""Offline regression coverage for MCP profiles and request-token progress."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from seohead.servers.mcp_profiles import configure_profile
from seohead.servers.mcp_progress import MIN_INTERVAL_SECONDS, ProgressReporter, wrap_long_tools


def _server():
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("profile-test")
    annotations = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @server.tool(name="seo_inspect_url", annotations=annotations, meta={"lane": "inspect"})
    def inspect_url(url: str) -> dict:
        return {"url": url}

    @server.tool(name="seo_tool_catalog", annotations=annotations)
    def tool_catalog() -> dict:
        return {}

    @server.tool(name="seo_parse", annotations=annotations)
    def parse(url: str, options: dict | None = None) -> dict:
        return {"url": url, "options": options}

    @server.tool(name="seo_headers_check", annotations=annotations)
    def headers_check(url: str) -> dict:
        return {"url": url}

    @server.tool(name="seo_robots_check", annotations=annotations)
    def robots_check(url: str) -> dict:
        return {"url": url}

    @server.tool(name="seo_redirects_check", annotations=annotations)
    def redirects_check(url: str) -> dict:
        return {"url": url}

    @server.tool(name="seo_schema_check", annotations=annotations)
    def schema_check(url: str) -> dict:
        return {"url": url}

    @server.tool(name="seo_hreflang_check", annotations=annotations)
    def hreflang_check(url: str) -> dict:
        return {"url": url}

    @server.tool(name="seo_hidden", annotations=annotations)
    def hidden() -> dict:
        return {}

    return server


def test_quick_check_profile_reduces_actual_fastmcp_catalog():
    server = _server()

    configured = configure_profile(server, "quick-check")
    advertised = {tool.name for tool in asyncio.run(server.list_tools())}

    assert advertised == set(configured["advertised"])
    assert "seo_hidden" in configured["removed"]
    assert "seo_hidden" not in advertised
    assert "seo_parse" in advertised


def test_wrapped_fastmcp_tool_preserves_schema_annotations_and_meta():
    server = _server()
    before = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "seo_parse")

    wrap_long_tools(server)

    after = next(tool for tool in asyncio.run(server.list_tools()) if tool.name == "seo_parse")
    assert after.inputSchema == before.inputSchema
    assert after.outputSchema == before.outputSchema
    assert after.annotations == before.annotations
    assert after._meta == before._meta


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class _Context:
    def __init__(self, token):
        self.request_context = SimpleNamespace(meta=SimpleNamespace(progressToken=token))
        self.notifications = []

    async def report_progress(self, **value):
        self.notifications.append(value)


def test_progress_requires_token_is_monotonic_throttled_and_stops():
    async def exercise():
        clock = _Clock()
        without_token = _Context(None)
        async with ProgressReporter(without_token, "crawl", clock=clock) as report:
            await report.known(1, 2)
        assert without_token.notifications == []

        context = _Context("client-token")
        report = ProgressReporter(context, "crawl", clock=clock)
        async with report:
            await report.known(1, 2)
            await report.known(2, 2)  # Same instant: deliberately throttled.
            clock.value += MIN_INTERVAL_SECONDS
            await report.known(2, 2)
            with pytest.raises(ValueError, match="monotonic"):
                await report.known(1, 2)
        count = len(context.notifications)
        await report.known(2, 2, "must not notify after close")
        assert len(context.notifications) == count
        assert context.notifications[0]["total"] == 2

        elapsed_context = _Context("elapsed-token")
        elapsed = ProgressReporter(elapsed_context, "crawl", clock=clock)
        async with elapsed:
            clock.value += 1
            await elapsed.elapsed()
        assert elapsed_context.notifications[-1]["total"] is None
        assert "total unknown" in elapsed_context.notifications[-1]["message"]

    asyncio.run(exercise())
