"""The documented offline scan command and MCP share one handler."""

import asyncio
import json

from seohead import cli
from seohead.servers import handlers


def test_nested_reanalysis_cli_uses_file_input(monkeypatch, capsys):
    calls = []
    monkeypatch.setitem(
        handlers.HANDLERS,
        "scan_reanalyze",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )
    assert cli.main(["scan", "reanalyze", "--input", "old.sqlite", "--out", "new.sqlite"]) == 0
    assert calls == [{"input_path": "old.sqlite", "out": "new.sqlite"}]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_flat_reanalysis_cli_retains_json_input(monkeypatch, capsys):
    calls = []
    monkeypatch.setitem(
        handlers.HANDLERS,
        "scan_reanalyze",
        lambda **kwargs: calls.append(kwargs) or {"ok": True},
    )
    assert (
        cli.main(["scan-reanalyze", "--input", '{"input_path":"old.sqlite","out":"new.sqlite"}'])
        == 0
    )
    assert calls == [{"input_path": "old.sqlite", "out": "new.sqlite"}]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_reanalysis_mcp_annotations_and_handler(monkeypatch):
    from seohead.servers.mcp_server import build_server

    calls = []
    monkeypatch.setattr(handlers, "scan_reanalyze", lambda **kw: calls.append(kw) or {"ok": True})
    server = build_server()
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "seo_scan_reanalyze")
    assert tool.annotations.openWorldHint is False
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.readOnlyHint is False
    asyncio.run(
        server.call_tool("seo_scan_reanalyze", {"input_path": "old.sqlite", "out": "new.sqlite"})
    )
    assert calls == [{"input_path": "old.sqlite", "out": "new.sqlite", "producer_build": None}]
