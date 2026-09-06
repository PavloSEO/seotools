"""The existing CLI and MCP expose the same explicit scan output parameters."""

import io

import pytest

from seohead import cli
from seohead.servers import handlers


def test_cli_maps_scan_file_and_producer_build(monkeypatch, tmp_path, capsys):
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    monkeypatch.setitem(handlers.HANDLERS, "crawl_site", capture)
    path = str(tmp_path / "scan.sqlite")
    assert (
        cli.main(
            [
                "crawl-site",
                "--url",
                "https://example.test/",
                "--scan-out",
                path,
                "--producer-build",
                "a" * 40,
            ]
        )
        == 0
    )
    assert captured["scan_out"] == path
    assert captured["producer_build"] == "a" * 40
    assert captured["url"] == "https://example.test/"
    assert not (tmp_path / "scan.sqlite").exists()  # The replaced handler owns effects.


def test_mcp_maps_the_same_scan_arguments(monkeypatch, tmp_path):
    from seohead.servers.mcp_server import build_server

    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(handlers, "crawl_site", capture)
    path = str(tmp_path / "scan.sqlite")
    tool = build_server()._tool_manager.get_tool("seo_crawl_site")
    assert tool.fn(url="https://example.test/", scan_out=path, producer_build="a" * 40)["ok"]
    assert captured["scan_out"] == path
    assert captured["producer_build"] == "a" * 40


def test_authenticated_native_mode_omits_bodies_and_records_redacted_config(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from seohead.crawl.settings import load
    from seohead.crawl.sqlite_adapter import crawl_to_scan
    from seohead.storage import open_scan
    from tests.test_scan_native import _metadata

    monkeypatch.setenv("TEST_TOKEN", "synthetic-test-only")
    settings = load(
        overrides={
            "http.credentials_acknowledged": True,
            "http.credential_headers": [
                {"host": "example.test", "headers": {"Authorization": "env:TEST_TOKEN"}}
            ],
            "robots.policy": "ignore",
            "limits.max_urls": 1,
            "speed.min_delay_seconds": 0,
        }
    )
    path = tmp_path / "scan.sqlite"
    body = b"<html><title>Authenticated fixture</title></html>"
    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(path),
        settings=settings,
        producer_version="test",
        producer_revision="a" * 40,
        runtime_versions=_metadata()["runtime_versions"],
        fetcher=lambda _url: SimpleNamespace(
            status_code=200, content=body, text=body.decode(), headers={"content-type": "text/html"}
        ),
        sleeper=lambda _: None,
    )
    assert run.pages == 1
    with open_scan(path, require_audit=False) as con:
        assert tuple(con.execute("SELECT body_state,body_reason FROM responses").fetchone()) == (
            "omitted",
            "credentialed",
        )
        config = con.execute("SELECT config_json FROM scan").fetchone()[0]
        assert "REDACTED" in config
        assert "TEST_TOKEN" not in config and "synthetic-test-only" not in config
        assert con.execute("SELECT COUNT(*) FROM bodies").fetchone()[0] == 0


def test_inspection_accepts_evidence_without_promising_a_report(tmp_path, capsys):
    import json

    from seohead.storage import ScanError, read_audit
    from seohead.storage.__main__ import main
    from seohead.storage.native_scan import NativeScan
    from tests.test_scan_native import _metadata

    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()):
        pass
    assert main(["inspect", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["scan"]["source_kind"] == "native"
    with pytest.raises(ScanError, match="no current audit"):
        read_audit(path)
