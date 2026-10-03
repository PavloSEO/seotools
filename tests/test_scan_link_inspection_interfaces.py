"""One saved-scan link query contract across handler, CLI and local MCP."""

from __future__ import annotations

import hashlib
import json

import pytest

from seohead import cli
from seohead.servers import handlers
from seohead.servers.mcp_server import build_server
from tests.test_scan_link_context import _link_rows
from tests.test_scan_link_context import _scan as context_scan
from tests.test_scan_link_queries import ROOT, TARGET, A, _edge
from tests.test_scan_link_queries import _scan as graph_scan


def _mcp_tool():
    return build_server()._tool_manager.get_tool("seo_scan_link_inspect")


def test_path_handler_cli_and_mcp_return_the_same_observed_hops(tmp_path, capsys):
    path = graph_scan(tmp_path, {ROOT: [_edge(ROOT, A)], A: [_edge(A, TARGET)]})
    query = {"input_path": str(path), "view": "path", "seed": ROOT, "target": TARGET}
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    direct = handlers.scan_link_inspect(**query)
    assert direct["ok"] is True and direct["state"] == "found"
    assert len(direct["hops"]) == 2
    assert direct["coverage"]["global_reachability"] == "unknown"
    assert (
        cli.main(
            [
                "scan-link-inspect",
                "--scan",
                str(path),
                "--view",
                "path",
                "--seed",
                ROOT,
                "--target",
                TARGET,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == direct
    assert _mcp_tool().fn(**query) == direct
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    limited = handlers.scan_link_inspect(**query, max_edges=1)
    assert limited["ok"] is True and limited["state"] == "limit_reached"
    assert limited["coverage"]["global_reachability"] == "unknown"


def test_reverse_cursor_and_nested_cli_match_the_shared_handler(tmp_path, capsys):
    path = graph_scan(tmp_path, {ROOT: [_edge(ROOT, TARGET), _edge(ROOT, TARGET)]})
    first = handlers.scan_link_inspect(input_path=str(path), view="inlinks", target=TARGET, limit=1)
    assert first["ok"] is True and first["returned"] == 1 and first["has_more"] is True
    second_query = {
        "input_path": str(path),
        "view": "inlinks",
        "target": TARGET,
        "cursor": first["next_cursor"],
        "limit": 1,
    }
    second = handlers.scan_link_inspect(**second_query)
    assert second["returned"] == 1 and second["has_more"] is False
    assert (
        cli.main(
            [
                "scan",
                "link-inspect",
                "--scan",
                str(path),
                "--view",
                "inlinks",
                "--target",
                TARGET,
                "--cursor",
                first["next_cursor"],
                "--limit",
                "1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == second
    assert _mcp_tool().fn(**second_query) == second
    wrong = handlers.scan_link_inspect(
        input_path=str(path), view="inlinks", target=A, cursor=first["next_cursor"]
    )
    assert wrong["ok"] is False and "different scan" in wrong["error"]


def test_context_handler_cli_and_mcp_preserve_representation_and_missing_body(tmp_path, capsys):
    html = (
        "<body><main><h2>Details</h2><a href='/target'>Target</a>"
        "<a href='/another'>Another</a></main></body>"
    )
    path = context_scan(tmp_path, html)
    link = _link_rows(path)[0]
    query = {"input_path": str(path), "view": "context", "link_id": link["link_id"]}
    direct = handlers.scan_link_inspect(**query)
    assert direct["ok"] is True and direct["heading"]["text"] == "Details"
    assert direct["source_document_id"] == link["source_document_id"]
    assert direct["representation"] == "static"
    assert (
        cli.main(
            [
                "scan-link-inspect",
                "--scan",
                str(path),
                "--view",
                "context",
                "--link-id",
                str(link["link_id"]),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == direct
    assert _mcp_tool().fn(**query) == direct
    page_query = {
        "input_path": str(path),
        "view": "context",
        "document_id": link["source_document_id"],
        "limit": 1,
    }
    page = handlers.scan_link_inspect(**page_query)
    assert page["ok"] is True and page["total"] == 2
    assert page["has_more"] is True and page["next_offset"] == 1
    assert _mcp_tool().fn(**page_query) == page
    wrong_representation = handlers.scan_link_inspect(**query, representation="rendered")
    assert wrong_representation["ok"] is False
    assert "representation" in wrong_representation["error"]
    unavailable = context_scan(tmp_path / "missing", html, retain_body=False)
    missing_id = _link_rows(unavailable)[0]["link_id"]
    result = handlers.scan_link_inspect(
        input_path=str(unavailable), view="context", link_id=missing_id
    )
    assert result["ok"] is True and result["state"] == "unavailable"
    assert result["document_coverage"]["state"] == "unavailable"


def test_invalid_scan_url_and_mode_arguments_are_result_data(tmp_path, capsys):
    missing = handlers.scan_link_inspect(
        input_path=str(tmp_path / "missing.sqlite"), view="path", seed=ROOT, target=TARGET
    )
    assert missing["ok"] is False and missing["error"]
    invalid = handlers.scan_link_inspect(
        input_path=str(tmp_path / "missing.sqlite"),
        view="path",
        seed="file:///tmp/x",
        target=TARGET,
    )
    assert invalid["ok"] is False and "HTTP(S)" in invalid["error"]
    misuse = handlers.scan_link_inspect(input_path="scan.sqlite", view="context", seed=ROOT)
    assert misuse["ok"] is False and "exactly one" in misuse["error"]
    assert handlers.scan_link_inspect(input_path="scan.sqlite", view=["path"])["view"] == "invalid"
    assert (
        cli.main(
            [
                "scan-link-inspect",
                "--scan",
                str(tmp_path / "missing.sqlite"),
                "--view",
                "path",
                "--seed",
                ROOT,
                "--target",
                TARGET,
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["ok"] is False
    path = graph_scan(tmp_path / "valid", {})
    assert (
        cli.main(
            [
                "scan-link-inspect",
                "--scan",
                str(path),
                "--view",
                "path",
                "--seed",
                "file:///tmp/x",
                "--target",
                TARGET,
            ]
        )
        == 1
    )
    assert "HTTP(S)" in json.loads(capsys.readouterr().out)["error"]


def test_mcp_registration_is_read_only_and_errors_are_structured(tmp_path):
    tool = _mcp_tool()
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.openWorldHint is False
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        tool.fn(
            input_path=str(tmp_path / "missing.sqlite"),
            view="path",
            seed=ROOT,
            target=TARGET,
        )
    assert json.loads(str(exc.value))["ok"] is False


def test_public_output_byte_ceiling_is_a_named_result(monkeypatch):
    from seohead.storage import link_queries

    monkeypatch.setattr(
        link_queries,
        "shortest_observed_path",
        lambda *args, **kwargs: {
            "scan_uuid": "synthetic",
            "evidence_revision": 1,
            "coverage": {"global_reachability": "unknown"},
            "hops": [{"anchor": "x" * 5000}],
        },
    )
    result = handlers.scan_link_inspect(
        input_path="synthetic.sqlite", view="path", seed=ROOT, target=TARGET, max_bytes=4096
    )
    assert result["ok"] is False
    assert result["state"] == "limit_reached"
    assert result["reason"] == "output_byte_limit_exceeded"
    assert result["max_bytes"] == 4096
