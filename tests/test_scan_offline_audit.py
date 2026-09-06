"""Offline audit mode must not recover absent evidence by touching the network."""

from __future__ import annotations

import socket

from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import load
from seohead.crawl.spider import SpiderResult
from seohead.servers import handlers


def _result() -> SpiderResult:
    return SpiderResult(
        pages=[
            PageRecord(
                url="https://example.test/",
                status_code=200,
                content_type="text/html",
                size_bytes=42,
                title="Stored page",
                word_count=2,
                final_url="https://example.test/",
            )
        ],
        start_page_evidence={
            "html": "<html><head><title>Stored page</title></head><body>stored</body></html>",
            "outlinks": 0,
            "external_outlinks": 0,
        },
    )


def _call(monkeypatch, *, captured_render_summary=None):
    import seohead.recon.net as net
    import seohead.sf.core.sitemap_coverage as sitemap_coverage
    import seohead.tools.render as render

    def fail(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(net, "http_client", fail)
    monkeypatch.setattr(sitemap_coverage, "_fetch", fail)
    monkeypatch.setattr(sitemap_coverage, "http_client", fail)
    monkeypatch.setattr(render, "render_check", fail)
    monkeypatch.setattr(render, "render_document", fail)
    monkeypatch.setattr(handlers, "_run_render_escalation", fail)
    settings = load(overrides={"speed.min_delay_seconds": 0, "rendering.mode": "js"})
    return handlers._audit_crawl_result(
        _result(),
        settings=settings,
        url="https://example.test/",
        sitemap_seed={
            "sitemap_url": "https://example.test/sitemap.xml",
            "sitemap_urls": ["https://example.test/sitemap.xml"],
            "declared": [],
        },
        discovery={"mode": "offline-reanalysis"},
        offline=True,
        captured_render_summary=captured_render_summary,
    )


def test_offline_audit_never_calls_network_or_renderer_and_names_missing_inputs(monkeypatch):
    response, audit = _call(monkeypatch)

    assert response["render_escalation"] == {
        "mode": "js",
        "state": "unavailable",
        "reason": "offline reanalysis has no captured render summary",
    }
    skipped = {entry["id"]: entry["reason"] for entry in audit["run"]["checks_skipped"]}
    for check in (
        "SITEMAP_NOT_IN_ROBOTS",
        "ROBOTS_BLOCKS_RESOURCES",
        "SITEMAP_FETCH_INCOMPLETE",
        "SITEMAP_TOO_MANY_URLS",
        "SITEMAP_TOO_LARGE",
        "SITEMAP_URL_DUPLICATED",
        "SITEMAP_STALE_LASTMOD",
    ):
        assert skipped[check].startswith("offline reanalysis has no retained sitemap XML")
    assert "sitemap" not in audit


def test_offline_audit_surfaces_a_captured_render_summary_without_replaying_it(monkeypatch):
    summary = {"mode": "js", "render_requests": 3, "captured": True}
    response, audit = _call(monkeypatch, captured_render_summary=summary)

    assert response["render_escalation"] == summary
    assert audit["run"]["render_escalation"] == summary
