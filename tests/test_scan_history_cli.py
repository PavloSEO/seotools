"""Shared CLI/MCP wiring for explicit saved-scan history actions."""

from __future__ import annotations

import io
import json

from seohead import cli
from seohead.servers import handlers


def test_nested_scan_list_routes_flags_without_reading_stdin(monkeypatch, capsys):
    received = []
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO('{"must":"not-read"}'))
    monkeypatch.setitem(
        handlers.HANDLERS,
        "scan_list",
        lambda **kwargs: received.append(kwargs) or {"items": []},
    )

    assert cli.main(["scan", "list", "--directory", "scans", "--limit", "7"]) == 0

    assert received == [{"directory": "scans", "limit": 7}]
    assert json.loads(capsys.readouterr().out) == {"items": []}


def test_flat_scan_body_diff_and_prune_apply_forward_explicit_arguments(monkeypatch, capsys):
    calls = []
    monkeypatch.setitem(
        handlers.HANDLERS,
        "scan_body_diff",
        lambda **kwargs: calls.append(("diff", kwargs)) or {"status": "changed"},
    )
    monkeypatch.setitem(
        handlers.HANDLERS,
        "scan_prune",
        lambda **kwargs: calls.append(("prune", kwargs)) or {"applied": True},
    )

    assert (
        cli.main(
            [
                "scan-body-diff",
                "--left",
                "left.sqlite",
                "--right",
                "right.sqlite",
                "--url",
                "https://example.test/",
                "--text",
                "--max-lines",
                "12",
            ]
        )
        == 0
    )
    assert (
        cli.main(["scan", "prune", "--directory", "scans", "--plan", "plan.json", "--apply"]) == 0
    )

    assert calls == [
        (
            "diff",
            {
                "left": "left.sqlite",
                "right": "right.sqlite",
                "url": "https://example.test/",
                "text": True,
                "max_lines": 12,
            },
        ),
        ("prune", {"directory": "scans", "plan": "plan.json", "apply": True}),
    ]
    output = capsys.readouterr().out
    assert output.count('"status": "changed"') == 1
    assert output.count('"applied": true') == 1


def test_history_body_diff_opens_two_validated_readers_and_closes_them(monkeypatch):
    from seohead.servers import history_handlers

    closed = []

    class _Con:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    readers = iter((_Con("left"), _Con("right")))
    monkeypatch.setattr(history_handlers, "open_scan", lambda *_args, **_kwargs: next(readers))
    monkeypatch.setattr(
        history_handlers, "body_diff", lambda *args, **kwargs: {"args": args, **kwargs}
    )

    result = history_handlers.scan_body_diff("left.sqlite", "right.sqlite", "https://example.test/")

    assert result["args"][0].name == "left"
    assert result["args"][1].name == "right"
    assert closed == ["right", "left"]


def test_mcp_history_annotations_match_real_file_side_effects():
    from seohead.servers.mcp_server import build_server

    tools = {tool.name: tool for tool in build_server()._tool_manager.list_tools()}
    for name in ("seo_scan_list", "seo_scan_inspect", "seo_scan_body_diff"):
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    assert tools["seo_scan_snapshot"].annotations.readOnlyHint is False
    assert tools["seo_scan_snapshot"].annotations.destructiveHint is False
    for name in ("seo_scan_pin", "seo_scan_prune"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is True
