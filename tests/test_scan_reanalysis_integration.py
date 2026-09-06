"""End-to-end offline derivation from synthetic, MIT-licensed retained HTML."""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from seohead import __version__
from seohead.crawl.settings import load
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.storage import ScanError, open_scan

# Synthetic fixture authored for this test; MIT project content, no third-party corpus.
MIT_SYNTHETIC_HTML = b"""<!doctype html><html><head>
<title>Owned iframe fixture</title><script src="/app.js"></script>
</head><body><iframe src="/frame.html"></iframe><a href="/">home</a></body></html>"""


class _Response:
    def __init__(self, content: bytes, content_type: str) -> None:
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.status_code = 200
        self.headers = {"content-type": content_type}


def _settings(*, body_mode: str = "captured_entity_bytes") -> dict:
    return load(
        overrides={
            "speed.min_delay_seconds": 0,
            "limits.max_urls": 1,
            "limits.max_depth": 1,
            "resources.fetch": False,
            "storage.body_mode": body_mode,
        }
    )


def _runtime_versions() -> dict[str, str]:
    return {
        "python": "test",
        "sqlite": "test",
        "httpx": "test",
        "lxml": "test",
        "beautifulsoup4": "test",
    }


def _fetcher(url: str) -> _Response:
    if url.endswith("/robots.txt"):
        return _Response(b"User-agent: SEOHEAD-Tools\nAllow: /\n", "text/plain")
    if url.endswith("/app.js"):
        return _Response(b"window.fixture = true", "application/javascript")
    return _Response(MIT_SYNTHETIC_HTML, "text/html; charset=utf-8")


def _source(path: Path, *, body_mode: str = "captured_entity_bytes") -> dict:
    settings = _settings(body_mode=body_mode)
    crawl_to_scan(
        "https://example.test/",
        scan_out=str(path),
        settings=settings,
        producer_version=__version__,
        producer_revision="a" * 40,
        runtime_versions=_runtime_versions(),
        fetcher=_fetcher,
        sleeper=lambda _seconds: None,
    )
    if body_mode == "captured_entity_bytes":
        _save_native_audit(path, settings)
    return settings


def _save_native_audit(path: Path, settings: dict) -> None:
    """Use the normal native audit pipeline once, over the retained source scan."""
    from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
    from seohead.crawl.sqlite_adapter import retained_start_gate
    from seohead.servers.handlers import _audit_crawl_result
    from seohead.servers.scan_handlers import _rebuild_page_result
    from seohead.storage.native_scan import NativeScan

    with NativeScan.open(path) as scan:
        result = _rebuild_page_result(scan)
        result.start_page_evidence = retained_start_gate(scan, settings)
        with prepare_sitemap_reconciliation(scan.con, start_url="https://example.test/") as sitemap:
            _unused, audit = _audit_crawl_result(
                result,
                settings=settings,
                url="https://example.test/",
                sitemap_seed={"sitemap_url": None, "sitemap_urls": [], "declared": []},
                discovery={
                    "mode": "spider",
                    "directive_policy": settings["robots"]["policy"],
                    "robots_blocked": 0,
                    "sitemap_url": None,
                    "sitemap_urls": [],
                    "sitemap_seeded": 0,
                },
                stored_scan=scan,
                stored_sitemap=sitemap,
            )
        scan.save_audit(audit)
        assert scan.finish_capture("fixture complete")


def _scan(path: Path, *, require_audit: bool = True) -> dict:
    with open_scan(path, require_audit=require_audit) as con:
        return dict(con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())


def _page_outcomes(path: Path, *, require_audit: bool = True) -> list[tuple]:
    with open_scan(path, require_audit=require_audit) as con:
        return [
            tuple(row)
            for row in con.execute(
                "SELECT u.url,p.status_code,p.content_type,p.size_bytes,p.title,p.word_count,"
                "p.outlinks,p.external_outlinks,p.representation "
                "FROM pages p JOIN urls u USING(url_id) ORDER BY p.page_ordinal"
            )
        ]


def _audit_outcomes(path: Path) -> tuple[object, object]:
    with open_scan(path) as con:
        document = json.loads(con.execute("SELECT document_json FROM audit").fetchone()[0])
    return document["pages"], document["issues"]


def _forbid_network(monkeypatch, observed_html: list[str]) -> dict[str, int]:
    attempts: dict[str, int] = {}

    def forbidden(label: str):
        attempts[label] = attempts.get(label, 0) + 1
        raise AssertionError("offline reanalysis attempted network or browser work")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: forbidden("dns"))
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: forbidden("socket"))
    monkeypatch.setattr(httpx.Client, "send", lambda *_args, **_kwargs: forbidden("http"))
    import seohead.storage.reanalysis_pages as pages
    import seohead.tools.render as render

    original = pages._apply_body

    def probe(*args, **kwargs):
        html = args[2] if len(args) > 2 else kwargs.get("html", "")
        if isinstance(html, str):
            observed_html.append(html)
        return original(*args, **kwargs)

    monkeypatch.setattr(pages, "_apply_body", probe)
    monkeypatch.setattr(
        render, "render_document", lambda *_args, **_kwargs: forbidden("render_document")
    )
    monkeypatch.setitem(
        sys.modules,
        "playwright.sync_api",
        SimpleNamespace(sync_playwright=lambda *_args, **_kwargs: forbidden("browser_launch")),
    )
    return attempts


def test_reanalysis_derives_a_valid_audited_scan_without_network_and_can_chain(
    tmp_path, monkeypatch
):
    from seohead.servers.reanalysis_handlers import reanalyze_scan

    source = tmp_path / "source.sqlite"
    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"
    _source(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    source_scan = _scan(source)
    source_pages = _page_outcomes(source)
    source_audit = _audit_outcomes(source)
    observed_html: list[str] = []
    attempts = _forbid_network(monkeypatch, observed_html)

    reanalyze_scan(str(source), str(first), producer_build="b" * 40)

    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha
    first_scan = _scan(first)
    assert first_scan["source_kind"] == "reanalysis"
    assert first_scan["parent_scan_uuid"] == source_scan["scan_uuid"]
    assert first_scan["scan_uuid"] != source_scan["scan_uuid"]
    assert first_scan["writer_revision"] == "b" * 40
    with sqlite3.connect(first) as con:
        audit_revision = con.execute("SELECT evidence_revision FROM audit").fetchone()[0]
    assert first_scan["evidence_revision"] == audit_revision
    assert _page_outcomes(first) == source_pages
    assert _audit_outcomes(first) == source_audit
    assert any('<iframe src="/frame.html">' in html for html in observed_html)

    reanalyze_scan(str(first), str(second), producer_build="b" * 40)

    second_scan = _scan(second)
    assert second_scan["source_kind"] == "reanalysis"
    assert second_scan["parent_scan_uuid"] == first_scan["scan_uuid"]
    assert second_scan["scan_uuid"] != first_scan["scan_uuid"]
    assert _page_outcomes(second) == _page_outcomes(first)
    assert _audit_outcomes(second) == _audit_outcomes(first)
    assert attempts == {}


def test_reanalysis_preserves_partial_capture_reason_across_generations(tmp_path, monkeypatch):
    from seohead.servers.reanalysis_handlers import reanalyze_scan
    from seohead.storage import read_audit
    from seohead.storage.native_scan import NativeScan

    source, first, second = (
        tmp_path / name for name in ("source.sqlite", "first.sqlite", "second.sqlite")
    )
    settings = _settings()
    crawl_to_scan(
        "https://example.test/",
        scan_out=str(source),
        settings=settings,
        producer_version=__version__,
        producer_revision="a" * 40,
        runtime_versions=_runtime_versions(),
        fetcher=_fetcher,
        sleeper=lambda _: None,
    )
    with NativeScan.open(source) as scan:
        scan.interrupt("operator_requested_stop")
    attempts = _forbid_network(monkeypatch, [])
    for before, after in ((source, first), (first, second)):
        reanalyze_scan(str(before), str(after), producer_build="b" * 40)
        audit = read_audit(after)
        assert audit["run"]["crawl_partial"] is True
        assert audit["run"]["crawl_finish_reason"] == "operator_requested_stop"
        assert audit["run"]["crawl_stopped_reason"] == "operator_requested_stop"
    assert attempts == {}


def test_reanalysis_refuses_missing_required_html_without_creating_output(tmp_path, monkeypatch):
    from seohead.servers.reanalysis_handlers import reanalyze_scan

    source = tmp_path / "missing.sqlite"
    output = tmp_path / "must-not-exist.sqlite"
    _source(source, body_mode="off")
    attempts = _forbid_network(monkeypatch, [])

    with pytest.raises(ScanError, match=r"reanalysis unavailable.*not_enabled"):
        reanalyze_scan(str(source), str(output), producer_build="b" * 40)

    assert not output.exists()
    assert attempts == {}


def test_reanalysis_keeps_http_refresh_evidence_and_its_finding(tmp_path, monkeypatch):
    from seohead.servers.reanalysis_handlers import reanalyze_scan
    from seohead.storage import read_audit

    source, output = tmp_path / "refresh.sqlite", tmp_path / "derived.sqlite"
    settings = _settings()

    def fetcher(url):
        response = _fetcher(url)
        if url == "https://example.test/":
            response.headers["refresh"] = "0; url=/next"
        return response

    crawl_to_scan(
        "https://example.test/",
        scan_out=str(source),
        settings=settings,
        producer_version=__version__,
        producer_revision="a" * 40,
        runtime_versions=_runtime_versions(),
        fetcher=fetcher,
        sleeper=lambda _: None,
    )
    _save_native_audit(source, settings)
    before = read_audit(source)
    assert any(issue["check"] == "HTTP_REFRESH_REDIRECT" for issue in before["issues"])
    attempts = _forbid_network(monkeypatch, [])
    reanalyze_scan(str(source), str(output), producer_build="b" * 40)
    after = read_audit(output)
    assert after["issues"] == before["issues"]
    assert attempts == {}
