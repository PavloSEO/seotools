"""CLI and MCP reach the same settings-driven crawl filters without network access."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from seohead import cli
from seohead.crawl import spider
from tests.test_crawl_spider import FakeResponse, page

START = "https://example.test/"
TARGET = "https://example.test/manual.html"


def _config(path, *, exclude_extensions=None):
    scope = {
        "include_extensions": ["html"],
        "include_media_types": ["text/html"],
    }
    if exclude_extensions is not None:
        scope["exclude_extensions"] = exclude_extensions
    path.write_text(
        json.dumps(
            {
                "robots": {"policy": "ignore"},
                "scope": scope,
            }
        ),
        encoding="utf-8",
    )
    return path


def _stub_spider_transport(monkeypatch):
    real_spider = spider.crawl_site
    calls = []

    def offline_spider(url, **kwargs):
        def fetch(target):
            calls.append(target)
            if target == START:
                return page("/manual.html", "/skip.pdf")
            if target == TARGET:
                return FakeResponse(
                    "<html><head><title>must not parse</title></head><body>pdf</body></html>",
                    headers={"content-type": "application/pdf"},
                )
            if target == "https://example.test/skip.pdf":
                return FakeResponse("should not fetch", headers={"content-type": "application/pdf"})
            raise AssertionError(f"unexpected request: {target}")

        kwargs["fetcher"] = fetch
        kwargs["min_delay"] = 0
        kwargs["sleeper"] = lambda _seconds: None
        return real_spider(url, **kwargs)

    monkeypatch.setattr(spider, "crawl_site", offline_spider)
    return calls


def _assert_filtered_result(result, out_dir):
    assert result["discovery"]["excluded"] == {
        "excluded_by_extension": 1,
        "not_included_by_media_type": 1,
    }
    audit = json.loads((Path(out_dir) / "audit.json").read_text(encoding="utf-8"))
    assert audit["run"]["crawl_config"]["scope.include_extensions"] == ["html"]
    assert audit["run"]["crawl_config"]["scope.exclude_extensions"] == ["pdf"]
    assert audit["run"]["crawl_config"]["scope.include_media_types"] == ["text/html"]


def test_cli_config_file_applies_file_filters_to_the_real_shared_crawl(
    monkeypatch, tmp_path, capsys
):
    config = _config(tmp_path / "crawl.json")
    calls = _stub_spider_transport(monkeypatch)
    out_dir = tmp_path / "cli-legacy"

    exit_code = cli.main(
        [
            "crawl-site",
            "--url",
            START,
            "--config",
            str(config),
            "--out-dir",
            str(out_dir),
            "--set",
            "scope.exclude_extensions=pdf",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    _assert_filtered_result(output, out_dir)
    assert "https://example.test/skip.pdf" not in calls


def test_mcp_crawl_site_config_file_uses_the_same_filters(monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    from seohead.servers.mcp_server import build_server

    config = _config(tmp_path / "crawl.json", exclude_extensions=["pdf"])
    calls = _stub_spider_transport(monkeypatch)
    tool = build_server()._tool_manager.get_tool("seo_crawl_site")
    out_dir = tmp_path / "mcp-legacy"

    result = asyncio.run(
        tool.run(
            {
                "url": START,
                "config": str(config),
                "out_dir": str(out_dir),
            }
        )
    )

    _assert_filtered_result(result, out_dir)
    assert "https://example.test/skip.pdf" not in calls
