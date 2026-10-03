"""SQLite evidence contract for crawl file-type filters and resource independence."""

from __future__ import annotations

import json
import sqlite3

import pytest

from seohead.crawl.settings import load
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.storage import ScanError
from seohead.storage.native_scan import NativeScan


class _Response:
    def __init__(
        self,
        status_code: int,
        body: str | bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self.content = body.encode("utf-8") if isinstance(body, str) else body
        self.text = self.content.decode("utf-8", errors="replace")
        self.headers = headers or {"content-type": content_type}


def _runtime():
    return {
        "python": "test",
        "sqlite": "test",
        "httpx": "test",
        "lxml": "test",
        "beautifulsoup4": "test",
    }


def test_sqlite_preserves_filtered_page_link_reason_and_independent_resources(tmp_path):
    target = "https://example.test/manual.html?download=.pdf"
    redirect = "https://example.test/go.html"
    excluded_redirect = "https://example.test/redirected.PDF?download=1"
    start_html = (
        "<html><head><title>Start</title></head><body>"
        '<a href="/manual.html?download=.pdf">manual</a>'
        '<a href="/go.html">redirect</a>'
        '<a href="/assets/app.js">script link</a>'
        '<script src="/assets/app.js"></script>'
        '<img src="/images/logo.png" alt="Logo">'
        "</body></html>"
    )
    calls: list[str] = []

    def fetcher(url: str):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: *\nAllow: /\n", "text/plain")
        if url == "https://example.test/":
            return _Response(200, start_html, "text/html; charset=utf-8")
        if url == target:
            return _Response(
                200,
                "P" * 2048,
                "application/pdf; version=1.7",
            )
        if url == redirect:
            return _Response(
                302,
                "",
                "text/html",
                {"content-type": "text/html", "location": "/redirected.PDF?download=1"},
            )
        if url == excluded_redirect:
            return _Response(200, "unexpected", "application/pdf")
        if url == "https://example.test/assets/app.js":
            return _Response(200, "x" * 128, "application/javascript")
        raise AssertionError(f"unexpected fixture request: {url}")

    scan_path = tmp_path / "filtered.sqlite"
    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=load(
            overrides={
                "speed.min_delay_seconds": 0,
                "scope.include_extensions": ["html"],
                "scope.exclude_extensions": ["pdf", "js"],
                "scope.include_media_types": ["text/html"],
                "resources.fetch": True,
                "resources.max_response_bytes": 64,
                "limits.max_response_bytes": 512,
                "limits.max_urls": 10,
                "limits.max_depth": 2,
            }
        ),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions=_runtime(),
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )

    assert run.pages == 3
    NativeScan.inspect(str(scan_path))
    assert target in calls
    assert "https://example.test/assets/app.js" in calls
    assert "https://example.test/images/logo.png" not in calls
    assert excluded_redirect not in calls
    with sqlite3.connect(scan_path) as con:
        con.row_factory = sqlite3.Row
        page = con.execute(
            "SELECT p.content_type,p.title,p.images_total,p.body_unavailable "
            "FROM pages p JOIN urls u USING(url_id) WHERE u.url=?",
            (target,),
        ).fetchone()
        assert page["content_type"] == "application/pdf; version=1.7"
        assert page["title"] == ""
        assert page["body_unavailable"] == "not_included_by_media_type"
        decision = con.execute("SELECT reason FROM decisions WHERE url=?", (target,)).fetchone()
        assert decision["reason"] == "not_included_by_media_type"
        redirect_response = con.execute(
            "SELECT p.status_code,p.redirect_url FROM pages p JOIN urls u USING(url_id) "
            "WHERE u.url=?",
            (redirect,),
        ).fetchone()
        assert tuple(redirect_response) == (302, excluded_redirect)
        redirect_decision = con.execute(
            "SELECT reason FROM decisions WHERE url=?", (excluded_redirect,)
        ).fetchone()
        assert redirect_decision["reason"] == "excluded_by_extension"
        resource_link_decision = con.execute(
            "SELECT reason FROM decisions WHERE url=?",
            ("https://example.test/assets/app.js",),
        ).fetchone()
        assert resource_link_decision["reason"] == "excluded_by_extension"
        link = con.execute(
            "SELECT 1 FROM links l JOIN urls d ON d.url_id=l.destination_url_id WHERE d.url=?",
            (target,),
        ).fetchone()
        assert link is not None

        image_evidence = con.execute(
            "SELECT p.images_total FROM pages p JOIN urls u USING(url_id) WHERE u.url=?",
            ("https://example.test/",),
        ).fetchone()
        assert image_evidence["images_total"] == 1
        page_response = con.execute(
            "SELECT r.status_code,r.content_type,r.response_headers_redacted_json,"
            "r.body_state,r.body_reason,r.body_sha256 FROM responses r "
            "JOIN urls u ON u.url_id=r.request_url_id "
            "WHERE u.url=? AND r.purpose='page'",
            (target,),
        ).fetchone()
        assert page_response["status_code"] == 200
        assert page_response["content_type"] == "application/pdf; version=1.7"
        assert [
            name.lower()
            for name, _value in json.loads(page_response["response_headers_redacted_json"])
        ] == ["content-type"]
        assert (
            page_response["body_state"],
            page_response["body_reason"],
            page_response["body_sha256"],
        ) == ("omitted", "unsupported_media", None)
        resource_response = con.execute(
            "SELECT r.content_type,r.body_state,r.body_reason FROM responses r "
            "JOIN urls u ON u.url_id=r.request_url_id "
            "WHERE u.url=? AND r.purpose='script'",
            ("https://example.test/assets/app.js",),
        ).fetchone()
        assert tuple(resource_response) == ("application/javascript", "truncated", "truncated")

    with pytest.raises(ScanError, match="configuration differs"):
        crawl_to_scan(
            "https://example.test/",
            scan_out=str(scan_path),
            settings=load(
                overrides={
                    "speed.min_delay_seconds": 0,
                    "scope.include_extensions": ["html"],
                    "scope.exclude_extensions": ["docx"],
                    "scope.include_media_types": ["text/html"],
                    "resources.fetch": True,
                    "resources.max_response_bytes": 64,
                    "limits.max_response_bytes": 512,
                    "limits.max_urls": 10,
                    "limits.max_depth": 2,
                }
            ),
            producer_version="3.0.0",
            producer_revision="a" * 40,
            runtime_versions=_runtime(),
            fetcher=lambda _url: pytest.fail("changed filters must refuse resume before fetch"),
            sleeper=lambda _seconds: None,
        )


def test_changed_file_filters_change_the_native_resume_fingerprint():
    first = load(overrides={"scope.exclude_extensions": ["pdf"]})
    second = load(overrides={"scope.exclude_extensions": ["docx"]})
    third = load(overrides={"scope.exclude_media_types": ["application/pdf"]})

    from seohead.crawl.settings import fingerprint

    assert fingerprint(first) != fingerprint(second)
    assert fingerprint(first) != fingerprint(third)


def test_filtered_decision_survives_native_resume_with_the_same_filters(tmp_path):
    from seohead.crawl.sqlite_adapter import crawl_to_scan

    start = "https://example.test/"
    excluded = "https://example.test/manual.pdf"
    next_page = "https://example.test/next.html"
    settings = load(
        overrides={
            "speed.min_delay_seconds": 0,
            "scope.exclude_extensions": ["pdf"],
            "scope.include_media_types": ["text/html"],
        }
    )
    scan_path = tmp_path / "resumed.sqlite"
    calls: list[str] = []

    def response(url):
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: *\nAllow: /\n", "text/plain")
        if url == start:
            return _Response(
                200,
                '<html><body><a href="/manual.pdf">pdf</a>'
                '<a href="/next.html">next</a></body></html>',
                "text/html",
            )
        if url == excluded:
            return _Response(200, "must not be fetched", "application/pdf")
        if url == next_page:
            return _Response(200, "<html><body>resumed</body></html>", "text/html")
        raise AssertionError(f"unexpected fixture request: {url}")

    def interrupt_after_start(pages, _queued):
        if pages:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        crawl_to_scan(
            start,
            scan_out=str(scan_path),
            settings=settings,
            producer_version="3.0.0",
            producer_revision="a" * 40,
            runtime_versions=_runtime(),
            fetcher=lambda url: (calls.append(url), response(url))[1],
            progress=interrupt_after_start,
            sleeper=lambda _seconds: None,
        )

    first_calls = list(calls)
    calls.clear()
    run = crawl_to_scan(
        start,
        scan_out=str(scan_path),
        settings=settings,
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions=_runtime(),
        fetcher=lambda url: (calls.append(url), response(url))[1],
        sleeper=lambda _seconds: None,
    )

    assert run.resumed is True
    assert run.finish_reason == "finished"
    assert excluded not in first_calls and excluded not in calls
    assert calls == [next_page]
    with sqlite3.connect(scan_path) as con:
        reason = con.execute("SELECT reason FROM decisions WHERE url=?", (excluded,)).fetchone()
        assert reason == ("excluded_by_extension",)
        link = con.execute(
            "SELECT 1 FROM links l JOIN urls d ON d.url_id=l.destination_url_id WHERE d.url=?",
            (excluded,),
        ).fetchone()
        assert link is not None


def test_new_filters_accept_existing_config_files_without_filter_keys(tmp_path):
    config_path = tmp_path / "old-crawl.json"
    config_path.write_text('{"scope": {"include_patterns": ["/blog/"]}}', encoding="utf-8")

    resolved = load(str(config_path))

    assert resolved["scope"]["include_extensions"] == []
    assert resolved["scope"]["exclude_extensions"] == []
    assert resolved["scope"]["include_media_types"] == []
    assert resolved["scope"]["exclude_media_types"] == []
