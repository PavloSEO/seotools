"""Real HTTPX/native-capture smoke against a synthetic loopback site only."""

from __future__ import annotations

import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from seohead.crawl.settings import load
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.storage.native_scan import NativeScan


def test_local_http_smoke_filters_extensions_responses_and_redirects(monkeypatch, tmp_path):
    requests: list[str] = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                requests.append(self.path)
            if self.path == "/":
                status, content_type, body, extra = (
                    200,
                    "text/html; charset=utf-8",
                    '<html><body><a href="/misleading.html">html</a>'
                    '<a href="/go.html">redirect</a>'
                    '<a href="/skip.PDF?download=1">pdf</a></body></html>',
                    {},
                )
            elif self.path == "/misleading.html":
                status, content_type, body, extra = (
                    200,
                    "application/pdf; version=1.7",
                    "P" * 4096,
                    {},
                )
            elif self.path == "/go.html":
                status, content_type, body, extra = (
                    302,
                    "text/html",
                    "",
                    {"Location": "/redirected.pdf"},
                )
            else:
                status, content_type, body, extra = (200, "application/pdf", "unexpected", {})
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            if data:
                self.wfile.write(data)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_HOSTS", "127.0.0.1")
    start = f"http://127.0.0.1:{server.server_address[1]}/"
    scan_path = tmp_path / "loopback.sqlite"
    try:
        run = crawl_to_scan(
            start,
            scan_out=str(scan_path),
            settings=load(
                overrides={
                    "speed.min_delay_seconds": 0,
                    "scope.include_extensions": ["html"],
                    "scope.exclude_extensions": ["pdf"],
                    "scope.include_media_types": ["text/html"],
                    "limits.max_response_bytes": 512,
                    "limits.max_urls": 10,
                    "limits.max_depth": 2,
                }
            ),
            producer_version="3.0.0",
            producer_revision="a" * 40,
            runtime_versions={
                "python": "test",
                "sqlite": "test",
                "httpx": "test",
                "lxml": "test",
                "beautifulsoup4": "test",
            },
            sleeper=lambda _seconds: None,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert run.pages == 3
    assert "/skip.PDF?download=1" not in requests
    assert "/redirected.pdf" not in requests
    NativeScan.inspect(str(scan_path))
    with sqlite3.connect(scan_path) as con:
        con.row_factory = sqlite3.Row
        page = con.execute(
            "SELECT p.status_code,p.content_type,p.body_unavailable,d.body_state,d.body_reason "
            "FROM pages p JOIN urls u USING(url_id) "
            "JOIN documents d USING(url_id) WHERE u.url=? AND d.representation='static'",
            (f"http://127.0.0.1:{server.server_address[1]}/misleading.html",),
        ).fetchone()
        assert tuple(page) == (
            200,
            "application/pdf; version=1.7",
            "not_included_by_media_type",
            "omitted",
            "unsupported_media",
        )
        redirect = con.execute(
            "SELECT p.status_code,p.redirect_url FROM pages p JOIN urls u USING(url_id) "
            "WHERE u.url LIKE '%/go.html'"
        ).fetchone()
        assert tuple(redirect) == (
            302,
            f"http://127.0.0.1:{server.server_address[1]}/redirected.pdf",
        )
        decision = con.execute(
            "SELECT reason FROM decisions WHERE url=?",
            (f"http://127.0.0.1:{server.server_address[1]}/redirected.pdf",),
        ).fetchone()
        assert decision["reason"] == "excluded_by_extension"
