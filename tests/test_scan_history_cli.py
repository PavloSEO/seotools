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


def test_flat_and_nested_scan_status_share_the_same_input_path(monkeypatch, capsys):
    received = []
    monkeypatch.setitem(
        handlers.HANDLERS,
        "scan_status",
        lambda **kwargs: received.append(kwargs) or {"ok": True, "frontier": {}},
    )

    assert cli.main(["scan-status", "--input", "saved.sqlite"]) == 0
    assert cli.main(["scan", "status", "--input", "saved.sqlite"]) == 0

    assert received == [{"input_path": "saved.sqlite"}, {"input_path": "saved.sqlite"}]
    assert capsys.readouterr().out.count('"ok": true') == 2


def test_scan_artifacts_use_common_scan_flag_and_legacy_path_warns_once(monkeypatch, capsys):
    received = []
    monkeypatch.setitem(
        handlers.HANDLERS,
        "scan_status",
        lambda **kwargs: received.append(kwargs) or {"ok": True},
    )

    assert cli.main(["scan-status", "--scan", "saved.sqlite"]) == 0
    assert cli.main(["scan-status", "--input", "old.sqlite"]) == 0

    assert received == [{"input_path": "saved.sqlite"}, {"input_path": "old.sqlite"}]
    assert capsys.readouterr().err.count("--input FILE is deprecated") == 1


def test_scan_json_input_and_deprecated_json_alias_are_unambiguous(monkeypatch, capsys):
    received = []
    monkeypatch.setitem(
        handlers.HANDLERS,
        "scan_status",
        lambda **kwargs: received.append(kwargs) or {"ok": True},
    )

    assert cli.main(["scan-status", "--input", '{"input_path":"json.sqlite"}']) == 0
    assert cli.main(["scan-status", "--json-input", '{"input_path":"old-json.sqlite"}']) == 0

    assert received == [{"input_path": "json.sqlite"}, {"input_path": "old-json.sqlite"}]
    assert capsys.readouterr().err.count("--json-input is deprecated") == 1


def test_braced_legacy_scan_paths_rewrite_before_argument_parsing(monkeypatch, capsys):
    received = []
    monkeypatch.setitem(
        handlers.HANDLERS,
        "scan_status",
        lambda **kwargs: received.append(kwargs) or {"ok": True},
    )
    legacy_path = "{archive}.sqlite"

    for argv in (
        ["scan-status", "--input", legacy_path],
        ["scan-status", f"--input={legacy_path}"],
        ["scan", "status", "--input", legacy_path],
        ["scan", "status", f"--input={legacy_path}"],
    ):
        assert cli.main(argv) == 0

    assert received == [{"input_path": legacy_path}] * 4
    assert capsys.readouterr().err.count("--input FILE is deprecated") == 4


def test_windows_stream_configuration_is_optional_and_utf8(monkeypatch):
    calls = []

    class Stream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.sys, "stdout", Stream())
    monkeypatch.setattr(cli.sys, "stderr", Stream())

    cli._configure_windows_streams()

    assert calls == [{"encoding": "utf-8"}, {"encoding": "utf-8"}]


def test_project_new_preserves_a_spaced_non_ascii_directory_in_cli_json(tmp_path, capsys):
    directory = (
        tmp_path
        / "\u043f\u0440\u043e\u0435\u043a\u0442 \u0441 \u043f\u0440\u043e\u0431\u0435\u043b\u043e\u043c"
    )

    assert (
        cli.main(
            ["project-new", "--directory", str(directory), "--target", "https://example.test/"]
        )
        == 0
    )

    result = json.loads(capsys.readouterr().out)
    assert result["path"] == str(directory)
    assert directory.is_dir()


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
    for name in ("seo_scan_list", "seo_scan_inspect", "seo_scan_status", "seo_scan_body_diff"):
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    assert tools["seo_scan_snapshot"].annotations.readOnlyHint is False
    assert tools["seo_scan_snapshot"].annotations.destructiveHint is False
    for name in ("seo_scan_pin", "seo_scan_prune"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is True
