"""Finite, network-free handler bridge coverage for native scan collection."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from seohead.crawl.sqlite_adapter import ScanRun
from seohead.servers import scan_handlers
from tests.test_scan_artifact_office import frozen_office_clock as frozen_office_clock


class _Con:
    def __init__(self, counts, has_audit=False):
        self.counts = counts
        self.has_audit = has_audit

    def execute(self, statement, _params=()):
        if "FROM audit" in statement:
            return SimpleNamespace(fetchone=lambda: (1,) if self.has_audit else None)
        table = statement.rsplit(" ", 1)[-1]
        return SimpleNamespace(fetchone=lambda: (self.counts[table],))


class _Scan:
    current = None

    def __init__(self, counts):
        self.con = _Con(counts)
        self.saved = None
        self.finished = []
        self.audit_unavailable = None

    @classmethod
    def open(cls, *_args, **_kwargs):
        return cls.current

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def save_audit(self, audit):
        self.saved = audit

    def note_audit_unavailable(self, reason):
        self.audit_unavailable = reason

    def finish_capture(self, *, reason):
        self.finished.append(reason)
        return True

    def resume_snapshot(self, *, include_edges=False):
        assert include_edges
        return {"counts": self.con.counts}


def _run(**overrides):
    fields = {
        "path": "scan.sqlite",
        "pages": 1,
        "links": 1,
        "forms": 0,
        "lifecycle": "running",
        "finish_reason": "finished",
        "partial": False,
        "start_page_gate": {"html": "<html></html>", "outlinks": 0, "external_outlinks": 0},
    }
    fields.update(overrides)
    return ScanRun(**fields)


@pytest.fixture
def bridge(monkeypatch):
    scan = _Scan({"pages": 1, "links": 1, "forms": 0})
    _Scan.current = scan
    monkeypatch.setattr("seohead.storage.native_scan.NativeScan", _Scan)
    monkeypatch.setattr(
        scan_handlers, "_producer_provenance", lambda _build: ("test", "a" * 40, {})
    )
    monkeypatch.setattr(
        scan_handlers,
        "_seed_urls",
        lambda *_args: {"sitemap_url": None, "sitemap_urls": [], "declared": []},
    )
    monkeypatch.setattr(
        "seohead.crawl.sqlite_adapter.crawl_to_scan", lambda *_args, **_kwargs: _run()
    )
    return scan


def test_small_scan_uses_transient_gate_and_existing_audit_path(bridge, monkeypatch):
    from seohead.crawl.spider import SpiderResult

    result = SpiderResult()
    captured = {}
    monkeypatch.setattr(scan_handlers, "_rebuild_spider_result", lambda _scan: result)

    def audit_bridge(actual, **kwargs):
        captured["result"] = actual
        captured["kwargs"] = kwargs
        return {"ignored": True}, {"schema_version": "2.0", "pages": []}

    monkeypatch.setattr("seohead.servers.handlers._audit_crawl_result", audit_bridge)
    response = scan_handlers.crawl_site_scan(
        "https://example.test/",
        scan_out="scan.sqlite",
        settings={"robots": {"policy": "respect"}},
        producer_build="a" * 40,
    )
    assert response["audit_available"] is True
    assert bridge.saved == {"schema_version": "2.0", "pages": []}
    assert bridge.finished == ["finished"]
    assert captured["result"].start_page_evidence == {
        "html": "<html></html>",
        "outlinks": 0,
        "external_outlinks": 0,
    }
    assert captured["kwargs"]["out_dir"] is None


def test_large_scan_is_guarded_before_materialization_or_audit(bridge, monkeypatch):
    bridge.con.counts["links"] = scan_handlers.MAX_AUDIT_LINKS + 1
    monkeypatch.setattr(
        scan_handlers, "_rebuild_spider_result", lambda _scan: pytest.fail("materialized")
    )
    monkeypatch.setattr(
        "seohead.servers.handlers._audit_crawl_result",
        lambda *_args, **_kwargs: pytest.fail("audited"),
    )
    response = scan_handlers.crawl_site_scan(
        "https://example.test/",
        scan_out="scan.sqlite",
        settings={"robots": {"policy": "respect"}},
        producer_build="a" * 40,
    )
    assert response["audit_available"] is False
    assert "links=" in response["audit_reason"]
    assert bridge.saved is None and bridge.finished


def test_resumed_scan_without_transient_html_is_named_no_audit(bridge, monkeypatch):
    monkeypatch.setattr(
        "seohead.crawl.sqlite_adapter.crawl_to_scan",
        lambda *_args, **_kwargs: _run(start_page_gate=None),
    )
    monkeypatch.setattr(
        scan_handlers, "_rebuild_spider_result", lambda _scan: pytest.fail("materialized")
    )
    response = scan_handlers.crawl_site_scan(
        "https://example.test/",
        scan_out="scan.sqlite",
        settings={"robots": {"policy": "respect"}},
        producer_build="a" * 40,
    )
    assert response["audit_available"] is False
    assert "start-page" in response["audit_reason"]
    assert bridge.finished


def test_interrupted_finalization_reuses_an_already_saved_audit(bridge, monkeypatch):
    bridge.con.has_audit = True
    monkeypatch.setattr(
        "seohead.crawl.sqlite_adapter.crawl_to_scan",
        lambda *_args, **_kwargs: _run(start_page_gate=None),
    )
    monkeypatch.setattr(
        scan_handlers, "_rebuild_spider_result", lambda _scan: pytest.fail("materialized")
    )
    response = scan_handlers.crawl_site_scan(
        "https://example.test/",
        scan_out="scan.sqlite",
        settings={"robots": {"policy": "respect"}},
        producer_build="a" * 40,
    )
    assert response["audit_available"] is True
    assert "reused" in response["audit_reason"]
    assert bridge.saved is None and bridge.finished == ["finished"]


def test_explicit_producer_build_never_invokes_git(monkeypatch):
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: pytest.fail("git invoked"))
    _version, revision, runtime = scan_handlers._producer_provenance("a" * 40)
    assert revision == "a" * 40
    assert set(runtime) == {"python", "sqlite", "httpx", "lxml", "beautifulsoup4"}


def test_invalid_native_mode_does_not_start_sitemap_requests(tmp_path, monkeypatch):
    from seohead.crawl.settings import load

    monkeypatch.setattr(
        scan_handlers, "_seed_urls", lambda *args: pytest.fail("invalid mode fetched a sitemap")
    )
    with pytest.raises(ValueError, match=r"cache\.mode=off"):
        scan_handlers.crawl_site_scan(
            "https://example.test/",
            scan_out=str(tmp_path / "scan.sqlite"),
            settings=load(overrides={"cache.mode": "live"}),
            sitemap="https://example.test/sitemap.xml",
            producer_build="a" * 40,
        )
    assert not (tmp_path / "scan.sqlite").exists()


@pytest.mark.parametrize("min_delay", [0.0, 0.01])
def test_real_handler_adapter_native_audit_and_all_report_formats(
    tmp_path, monkeypatch, frozen_office_clock, min_delay
):
    """The handler uses injected transport, but every storage and audit layer is real."""
    import httpx

    from seohead.crawl import sqlite_adapter
    from seohead.crawl.collect import fetch_one as real_fetch_one
    from seohead.crawl.settings import load
    from seohead.crawl.spider import _fetch_robots as real_fetch_robots
    from seohead.reports import build_report
    from seohead.sf.core import sitemap_coverage
    from seohead.storage import read_audit

    sitemap_url = "https://example.test/sitemap.xml"
    documents = {
        "https://example.test/robots.txt": (
            "text/plain",
            "User-agent: SEOHEAD-Tools\nAllow: /\nSitemap: https://example.test/sitemap.xml\n",
        ),
        "https://example.test/": (
            "text/html",
            "<html><head><title>Home</title></head><body><main><h1>Home</h1>"
            "<a href='/next'>Next</a><form method='post' action='/send'>"
            "<input type='password'></form></main></body></html>",
        ),
        "https://example.test/next": (
            "text/html",
            "<html><head><title>Next</title></head><body><h1>Next</h1></body></html>",
        ),
    }

    class Response:
        def __init__(self, url):
            content_type, text = documents[url]
            self.status_code = 200
            self.text = text
            self.content = text.encode("utf-8")
            self.headers = {"content-type": content_type}

    def collector_transport(url):
        return Response(url)

    def fetch_with_injected_transport(url, **kwargs):
        kwargs.pop("fetcher", None)
        return real_fetch_one(url, fetcher=collector_transport, **kwargs)

    @contextmanager
    def no_client(*_args, **_kwargs):
        yield None

    def robots_with_injected_transport(start, _fetcher, _client):
        return real_fetch_robots(start, collector_transport, None)

    def sitemap_response(request):
        if str(request.url).endswith("robots.txt"):
            return httpx.Response(200, text=documents["https://example.test/robots.txt"][1])
        if str(request.url) == sitemap_url:
            return httpx.Response(
                200,
                text=(
                    "<?xml version='1.0'?><urlset "
                    "xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                    "<url><loc>https://example.test/</loc></url>"
                    "<url><loc>https://example.test/next</loc></url></urlset>"
                ),
            )
        return httpx.Response(404)

    monkeypatch.setattr(sqlite_adapter, "_client_context", no_client)
    monkeypatch.setattr(sqlite_adapter, "fetch_one", fetch_with_injected_transport)
    monkeypatch.setattr(sqlite_adapter, "_fetch_robots", robots_with_injected_transport)
    monkeypatch.setattr(
        scan_handlers,
        "_seed_urls",
        lambda *_args: {
            "sitemap_url": sitemap_url,
            "sitemap_urls": [sitemap_url],
            "declared": ["https://example.test/", "https://example.test/next"],
        },
    )
    monkeypatch.setattr(sitemap_coverage, "validate_url", lambda _url: None)
    monkeypatch.setattr(
        sitemap_coverage,
        "http_client",
        lambda *_args, **_kwargs: (
            httpx.Client(transport=httpx.MockTransport(sitemap_response)),
            False,
        ),
    )

    scan = tmp_path / "scan.sqlite"
    response = scan_handlers.crawl_site_scan(
        "https://example.test/",
        scan_out=str(scan),
        settings=load(overrides={"speed.min_delay_seconds": min_delay}),
        sitemap=sitemap_url,
        producer_build="a" * 40,
    )
    assert response["audit_available"] is True
    assert response["urls_collected"] == 2
    assert response["links_collected"] == 1
    assert response["forms_collected"] == 1

    audit = read_audit(scan)
    assert audit["run"]["effective_max_requests_per_second"] == (
        "unbounded" if min_delay == 0 else 100.0
    )
    assert audit["pages"] and audit["summary"]["totals"]["urls_crawled"] == 2
    for fmt in ("json", "md", "csv", "xlsx", "docx"):
        from_scan = tmp_path / "from-scan" / f"report.{fmt}"
        from_document = tmp_path / "from-document" / f"report.{fmt}"
        assert build_report(str(scan), fmt, str(from_scan))["ok"]
        assert build_report(audit, fmt, str(from_document))["ok"]
        assert from_scan.read_bytes() == from_document.read_bytes()
        if fmt == "csv":
            assert (
                from_scan.with_suffix(".pages.csv").read_bytes()
                == from_document.with_suffix(".pages.csv").read_bytes()
            )
