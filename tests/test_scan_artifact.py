"""One offline crawl must survive the new storage boundary without changing reports."""

from __future__ import annotations

import json
import socket

import pytest

from seohead.reports import build_report

BUILD = "25fd2ed032a31d63c5811722619e35c14b631476"


@pytest.fixture
def legacy_run(tmp_path, monkeypatch):
    from seohead.crawl import spider
    from seohead.servers import handlers
    from seohead.sf.core import sitemap_coverage
    from tests.test_crawl_spider import FakeResponse, _fetcher

    def no_network(*args, **kwargs):
        raise AssertionError("storage acceptance must not use the network")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("SEOHEAD_HTTP_CACHE_DIR", "off")
    responses = {
        "https://example.com/robots.txt": FakeResponse("User-agent: *\nAllow: /\n"),
        "https://example.com/": FakeResponse(
            "<!doctype html><html><head><title>Evidence home</title>"
            '<link rel="alternate" hreflang="FR" href="/fr/">'
            '<link rel="alternate" hreflang="x-default" href="/">'
            '</head><body><main><iframe src="/frame"></iframe>'
            '<iframe src="https://outside.example/embed"></iframe>'
            '<h1>Home</h1><a href="/second">Second</a><a href="/second">Second again</a>'
            '<a href="https://outside.example/path">External</a></main></body></html>'
        ),
        "https://example.com/second": FakeResponse(
            "<!doctype html><html><head><title>Second page</title></head>"
            "<body><h1>Second</h1><p>" + "Large response body. " * 200 + "</p></body></html>"
        ),
    }
    original = spider.crawl_site

    def offline_crawl(*args, **kwargs):
        return original(*args, **kwargs, fetcher=_fetcher(responses), sleeper=lambda _: None)

    monkeypatch.setattr(spider, "crawl_site", offline_crawl)
    monkeypatch.setattr(sitemap_coverage, "run_sitemap", lambda *a, **k: {})
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"speed": {"min_delay_seconds": 0.01}, "limits": {"max_response_bytes": 1024}})
    )
    directory = tmp_path / "run"
    result = handlers.crawl_site(
        url="https://example.com/", out_dir=str(directory), config=str(config)
    )
    assert result["urls_collected"] == 2
    audit = json.loads((directory / "audit.json").read_text())
    assert len(audit["pages"]) == 2
    assert audit["issues"]
    assert (directory / "links.jsonl").read_text().count("Second") == 2
    return directory


@pytest.mark.parametrize("fmt", ["json", "md", "csv"])
def test_same_run_reports_are_byte_identical(legacy_run, tmp_path, fmt):
    expected = tmp_path / "directory" / f"report.{fmt}"
    assert build_report(str(legacy_run / "audit.json"), fmt, str(expected))["ok"]

    from seohead.storage import import_run, read_audit

    artifact = tmp_path / "scan.sqlite"
    import_run(legacy_run, artifact, producer_build=BUILD)
    actual = tmp_path / "sqlite" / f"report.{fmt}"
    assert build_report(read_audit(artifact), fmt, str(actual))["ok"]
    assert actual.read_bytes() == expected.read_bytes()
    if fmt == "csv":
        assert (
            actual.with_suffix(".pages.csv").read_bytes()
            == expected.with_suffix(".pages.csv").read_bytes()
        )
