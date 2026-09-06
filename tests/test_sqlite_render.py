"""Offline orchestration tests for retained native render documents."""

from __future__ import annotations

import json
from types import SimpleNamespace

from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import load
from seohead.crawl.spider import LinkEdge
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.crawl.sqlite_render import run_render_escalation
from seohead.storage.native_scan import NativeScan


class _Scan:
    def __init__(self):
        self.preflight_calls = 0
        self.calls = []

    def preflight_capture(self):
        self.preflight_calls += 1

    def commit_render(self, url, record, **kwargs):
        self.calls.append((url, record, kwargs))
        return len(self.calls)


class _Response:
    def __init__(self, status_code: int, text: str, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {"content-type": "text/html"}


def _settings(**overrides):
    values = {
        "rendering.mode": "js",
        "rendering.escalation.sample_per_pattern": 1,
        "rendering.escalation.max_render_urls": 1,
        "speed.min_delay_seconds": 0,
    }
    values.update(overrides)
    return load(overrides=values)


def _renderer(target):
    return {
        "engine": "playwright-chromium",
        "engine_version": "test",
        "navigation": {
            "requested_url": target,
            "final_url": target,
            "wait_until": "load",
            "timeout_seconds": 30.0,
        },
        "settings": {
            "viewport": {"width": 1366, "height": 768},
            "device_pixel_ratio": 1.0,
            "mobile_emulation": False,
            "touch_emulation": False,
            "script_timeout_seconds": 0.0,
            "resize_to_content": False,
            "resize_to_content_max_height_px": 15000,
            "persistent_profile": False,
        },
        "transforms": {
            "flatten_shadow_dom_requested": False,
            "flatten_shadow_dom_applied": 0,
            "flatten_iframes_requested": False,
            "flatten_iframes_applied": 0,
        },
        "policy": {"credentials_used": False, "cache_control_no_store": False},
    }


def test_native_render_commits_each_dom_then_discards_html(monkeypatch):
    from seohead.crawl import sqlite_render
    from seohead.tools import render as render_tool

    target = "https://example.test/"
    record = PageRecord(
        url=target,
        content_type="text/html",
        title="Raw",
        word_count=1,
        crawl_depth=0,
        outlinks=1,
    )
    result = SimpleNamespace(
        pages=[record],
        links=[LinkEdge(target, "https://example.test/raw", "raw", False)],
    )
    scan = _Scan()
    seen = {}

    monkeypatch.setattr(
        sqlite_render,
        "_static_html",
        lambda *_args: "<html><head><title>Raw</title></head><body>raw</body></html>",
    )

    def fake_document(url, _config, **kwargs):
        seen.update(kwargs)
        return {
            "ok": True,
            "url": url,
            "final_url": url,
            "html": "<html><head><title>DOM</title></head><body>"
            "<a href='/raw'>raw</a><a href='/new'>new</a>"
            "<form action='/send' method='post'><input type='password'></form>"
            "</body></html>",
            "renderer": _renderer(url),
        }

    monkeypatch.setattr(render_tool, "render_document", fake_document)
    escalation = run_render_escalation(scan, result, _settings())

    assert scan.preflight_calls >= 2
    assert seen["max_html_bytes"] == 5 * 1024 * 1024
    assert len(scan.calls) == 2  # bounded probe DOM, then accepted DOM
    url, committed, kwargs = next(call for call in scan.calls if call[1] is not None)
    assert url == target
    assert committed["representation"] == "rendered"
    assert {edge["destination"] for edge in kwargs["links"]} == {
        "https://example.test/raw",
        "https://example.test/new",
    }
    assert kwargs["forms"][0]["has_password"] is True
    assert kwargs["html"].startswith("<html>")
    assert result.pages[0].title == "DOM"
    # Native analysis reads the SQL graph, so the transient result does not
    # accumulate rendered links after their transaction commits.
    assert {edge.destination for edge in result.links} == {"https://example.test/raw"}
    assert "html" not in escalation.rendered[target]
    assert escalation.representations[target] == "rendered"


def test_native_render_keeps_raw_when_dom_is_degenerate(monkeypatch):
    from seohead.crawl import sqlite_render
    from seohead.tools import render as render_tool

    target = "https://example.test/"
    record = PageRecord(url=target, content_type="text/html", title="Raw", word_count=100)
    result = SimpleNamespace(pages=[record], links=[])
    scan = _Scan()
    monkeypatch.setattr(
        sqlite_render,
        "_static_html",
        lambda *_args: (
            "<html><head><title>Raw</title></head><body>" + "word " * 100 + "</body></html>"
        ),
    )
    monkeypatch.setattr(
        render_tool,
        "render_document",
        lambda url, *_args, **_kwargs: {
            "ok": True,
            "url": url,
            "final_url": url,
            "html": "<html><body></body></html>",
            "renderer": _renderer(url),
        },
    )

    escalation = run_render_escalation(scan, result, _settings())

    assert len(scan.calls) == 2  # probe plus retained rejected full DOM
    assert all(call[1] is None for call in scan.calls)
    assert result.pages[0].title == "Raw"
    assert escalation.representations[target] == "static"
    assert escalation.rendered[target]["capture"] == {
        "accepted": False,
        "state": "unavailable",
        "reason": "rendered body is degenerate",
    }


def test_native_raw_mode_never_invokes_renderer():
    scan = _Scan()
    result = SimpleNamespace(pages=[PageRecord(url="https://example.test/")], links=[])

    escalation = run_render_escalation(scan, result, _settings(**{"rendering.mode": "raw"}))

    assert escalation.mode == "raw"
    assert not scan.calls
    assert scan.preflight_calls == 0


def test_native_render_updates_a_real_scan_without_materializing_its_graph(monkeypatch, tmp_path):
    from seohead.servers.scan_handlers import _rebuild_page_result
    from seohead.tools import render as render_tool

    target = "https://example.test/"
    responses = {
        "https://example.test/robots.txt": _Response(200, "User-agent: *\nAllow: /\n"),
        target: _Response(
            200,
            "<html><head><title>Raw</title></head><body><a href='/raw'>raw</a></body></html>",
        ),
    }
    path = tmp_path / "scan.sqlite"
    settings = _settings(**{"discovery.hyperlinks.crawl": False})
    crawl_to_scan(
        target,
        scan_out=str(path),
        settings=settings,
        producer_version="test",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=responses.__getitem__,
        sleeper=lambda _seconds: None,
    )
    monkeypatch.setattr(
        render_tool,
        "render_document",
        lambda url, *_args, **_kwargs: {
            "ok": True,
            "url": url,
            "final_url": url,
            "html": "<html><head><title>DOM</title></head><body>"
            "<a href='/raw'>raw</a><a href='/new'>new</a></body></html>",
            "renderer": _renderer(url),
        },
    )

    with NativeScan.open(path) as scan:
        result = _rebuild_page_result(scan)
        escalation = run_render_escalation(scan, result, settings)
        page = scan.con.execute("SELECT title,representation FROM pages").fetchone()
        links = scan.con.execute(
            "SELECT evidence_representation,COUNT(*) FROM links GROUP BY evidence_representation"
        ).fetchall()
        documents = scan.con.execute(
            "SELECT representation,COUNT(*) FROM documents GROUP BY representation"
        ).fetchall()

    assert escalation.representations[target] == "rendered"
    assert result.links == []
    assert tuple(page) == ("DOM", "rendered")
    assert {tuple(row) for row in links} == {("static", 1), ("rendered", 2)}
    # Static HTML, bounded probe DOM, accepted DOM: all are durable rows;
    # only the current accepted DOM is attached to pages.
    assert {tuple(row) for row in documents} == {("static", 1), ("rendered", 2)}


def test_native_render_marks_missing_raw_body_unavailable_without_a_new_request(
    monkeypatch, tmp_path
):
    from seohead.servers.scan_handlers import _rebuild_page_result
    from seohead.tools import render as render_tool

    target = "https://example.test/"
    path = tmp_path / "off.sqlite"
    settings = _settings(
        **{
            "storage.body_mode": "off",
            "storage.max_body_bytes": 0,
            "discovery.hyperlinks.crawl": False,
        }
    )
    crawl_to_scan(
        target,
        scan_out=str(path),
        settings=settings,
        producer_version="test",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=lambda url: _Response(
            200,
            "User-agent: *\nAllow: /\n"
            if url.endswith("robots.txt")
            else "<html><head><title>Raw</title></head><body>raw</body></html>",
        ),
        sleeper=lambda _seconds: None,
    )
    monkeypatch.setattr(
        render_tool,
        "render_document",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not render")),
    )

    with NativeScan.open(path) as scan:
        result = _rebuild_page_result(scan)
        escalation = run_render_escalation(scan, result, settings)
        latest = scan.con.execute(
            "SELECT body_state,body_reason FROM documents WHERE representation='rendered' "
            "ORDER BY document_id DESC LIMIT 1"
        ).fetchone()

    assert escalation.representations[target] == "static"
    assert tuple(latest) == ("unavailable", "not_in_corpus")


def test_native_legacy_fragment_keeps_escaped_navigation_as_response_provenance(
    monkeypatch, tmp_path
):
    from seohead.crawl import sqlite_render
    from seohead.crawl.capture import CaptureEvent
    from seohead.servers.scan_handlers import _rebuild_page_result

    target = "https://example.test/"
    escaped = "https://example.test/?_escaped_fragment_="
    snapshot = (
        "<html><head><title>Snapshot</title></head><body><a href='/new'>new</a></body></html>"
    )
    path = tmp_path / "legacy.sqlite"
    settings = _settings(
        **{
            "rendering.mode": "legacy_fragment",
            "discovery.hyperlinks.crawl": False,
        }
    )
    crawl_to_scan(
        target,
        scan_out=str(path),
        settings=settings,
        producer_version="test",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=lambda url: _Response(
            200,
            "User-agent: *\nAllow: /\n"
            if url.endswith("robots.txt")
            else "<html><head><meta name='fragment' content='!'></head><body>raw</body></html>",
        ),
        sleeper=lambda _seconds: None,
    )
    event = CaptureEvent(
        method="GET",
        requested_url=escaped,
        effective_url=escaped,
        redirect_history=(),
        requested_at="2026-01-01T00:00:00Z",
        received_at="2026-01-01T00:00:01Z",
        status_code=200,
        request_headers=(),
        credentials_used=False,
        response_headers=(("content-type", "text/html; charset=utf-8"),),
        content_type="text/html; charset=utf-8",
        content_encoding="",
        entity_bytes=snapshot.encode(),
        body_fidelity="entity_bytes",
        body_state="complete",
        body_reason="none",
        error="",
        error_kind="",
        effective_status_code=200,
        effective_headers=(("content-type", "text/html; charset=utf-8"),),
    )
    monkeypatch.setattr(
        sqlite_render,
        "_legacy_fetch",
        lambda *_args: {
            "ok": True,
            "url": target,
            "final_url": escaped,
            "html": snapshot,
            "captures": (event,),
            "captured_at": event.received_at,
            "renderer": {
                **_renderer(target),
                "engine": "legacy-escaped-fragment",
                "engine_version": "scan.v1",
                "navigation_transform": "legacy_escaped_fragment",
                "navigation": {
                    "requested_url": escaped,
                    "final_url": escaped,
                    "wait_until": "load",
                    "timeout_seconds": 30.0,
                },
            },
        },
    )

    with NativeScan.open(path) as scan:
        result = _rebuild_page_result(scan)
        escalation = run_render_escalation(scan, result, settings)
        document = scan.con.execute(
            "SELECT d.representation,logical.url,d.renderer_json FROM documents d "
            "JOIN urls logical ON logical.url_id=d.url_id "
            "WHERE d.representation='legacy_fragment'"
        ).fetchone()
        navigation = scan.con.execute(
            "SELECT url FROM urls WHERE url_id=?",
            (json.loads(document[2])["navigation_url_id"],),
        ).fetchone()[0]

    assert escalation.representations[target] == "legacy_fragment"
    assert (document[0], document[1], navigation) == ("legacy_fragment", target, escaped)
