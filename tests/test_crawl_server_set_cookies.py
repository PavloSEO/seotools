"""Issue #647: a cookie the server set is not the operator's credential.

The crawl shares one HTTP client, and that client keeps a cookie jar. The moment
any response carried ``Set-Cookie``, every later request went out with ``Cookie:``
and the corpus writer read it as an authenticated fetch -- omitting the body under
``credentialed``. On a public site with ``http.credential_headers = []`` and
``credentials_acknowledged = false`` that discarded 33 001 of 40 920 page bodies,
and the run reported its pages and links exactly as a complete one would.

Three properties, on a loopback fixture and no network:

- a site that sets a session cookie keeps its bodies;
- a run the operator configured a credential header for still omits them, because
  that rule is the correct one and this must not weaken it;
- a run that discarded any body says how many, and why, where an operator reads it.
"""

from __future__ import annotations

import http.server
import sqlite3
import threading
from collections.abc import Iterator

import pytest

from seohead import cli
from seohead.servers import handlers

PAGES = [f"/p{index}/" for index in range(1, 9)]
HOME = (
    "<html><head><title>Home</title>"
    "<meta name='description' content='Home of the cookie fixture site, described at length.'>"
    "</head><body><h1>Home</h1>"
    + " ".join(f"<a href='{path}'>{path}</a>" for path in PAGES)
    + "</body></html>"
)
BUILD = "a" * 40


def _page(path: str) -> str:
    return (
        f"<html><head><title>{path}</title>"
        "<meta name='description' content='A fixture page described at length enough to pass.'>"
        f"</head><body><h1>{path}</h1><p>Body text.</p><a href='/'>Home</a></body></html>"
    )


class _Handler(http.server.BaseHTTPRequestHandler):
    """Sets one session cookie on the home page, then serves ordinary pages."""

    def _send(self, body: bytes, content_type: str, status: int = 200, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/robots.txt":
            return self._send(b"User-agent: *\nAllow: /\n", "text/plain; charset=utf-8")
        if path == "/":
            return self._send(
                HOME.encode(),
                "text/html; charset=utf-8",
                extra={"Set-Cookie": "session=abc123; Path=/"},
            )
        if path in PAGES:
            return self._send(_page(path).encode(), "text/html; charset=utf-8")
        return self._send(b"not found", "text/plain; charset=utf-8", status=404)

    def log_message(self, format: str, *args) -> None:
        pass


@pytest.fixture
def site(monkeypatch) -> Iterator[str]:
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _crawl(site: str, tmp_path, overrides=None):
    scan_out = str(tmp_path / "scan.sqlite")
    result = handlers.crawl_site(
        url=f"{site}/",
        scan_out=scan_out,
        max_urls=20,
        min_delay=0,
        concurrency=1,
        overrides=overrides,
        producer_build=BUILD,
    )
    con = sqlite3.connect(scan_out)
    try:
        documents = dict(
            con.execute(
                "SELECT body_state || '/' || body_reason, COUNT(*) FROM documents GROUP BY 1"
            ).fetchall()
        )
        credentials = dict(
            con.execute("SELECT credentials_used, COUNT(*) FROM responses GROUP BY 1").fetchall()
        )
    finally:
        con.close()
    return result, documents, credentials


def test_a_server_set_cookie_does_not_discard_page_bodies(site, tmp_path):
    result, documents, credentials = _crawl(site, tmp_path)

    assert result["urls_collected"] == 9
    assert documents == {"complete/none": 9}, "a session cookie is not the operator's credential"
    assert credentials == {0: 9}
    assert result["capabilities"]["html_bodies"]["state"] == "complete"
    assert result["html_bodies"] == {"total": 9, "retained": 9, "omitted": {}}


def test_an_operator_credential_header_still_discards_page_bodies(site, tmp_path, monkeypatch):
    monkeypatch.setenv("SEOHEAD_TEST_TOKEN", "Bearer operator-secret")
    result, documents, credentials = _crawl(
        site,
        tmp_path,
        overrides={
            "http.credential_headers": [
                {"host": "127.0.0.1", "headers": {"authorization": "env:SEOHEAD_TEST_TOKEN"}}
            ],
            "http.credentials_acknowledged": True,
        },
    )

    assert documents == {"omitted/credentialed": 9}, "the privacy rule must not weaken"
    assert credentials == {1: 9}
    assert result["html_bodies"] == {"total": 9, "retained": 0, "omitted": {"credentialed": 9}}


def test_a_credentialed_response_is_distinguishable_in_the_artifact(site, tmp_path, monkeypatch):
    """The stored request headers used to be identical whatever suppressed a body."""
    monkeypatch.setenv("SEOHEAD_TEST_TOKEN", "Bearer operator-secret")
    scan_out = str(tmp_path / "scan.sqlite")
    handlers.crawl_site(
        url=f"{site}/",
        scan_out=scan_out,
        max_urls=20,
        min_delay=0,
        concurrency=1,
        overrides={
            "http.credential_headers": [
                {"host": "127.0.0.1", "headers": {"authorization": "env:SEOHEAD_TEST_TOKEN"}}
            ],
            "http.credentials_acknowledged": True,
        },
        producer_build=BUILD,
    )
    con = sqlite3.connect(scan_out)
    try:
        recorded = [
            headers
            for (headers,) in con.execute(
                "SELECT request_headers_redacted_json FROM responses ORDER BY request_ordinal"
            )
        ]
    finally:
        con.close()

    assert all("x-seohead-redacted-headers" in headers for headers in recorded)
    assert any("authorization" in headers for headers in recorded)
    assert not any("operator-secret" in headers for headers in recorded), "no value may be stored"


def test_the_summary_names_how_many_bodies_a_run_discarded(capsys):
    cli._print_crawl_outcome(
        {
            "finish_reason": "finished",
            "urls_collected": 40920,
            "html_bodies": {"total": 40904, "retained": 7903, "omitted": {"credentialed": 33001}},
        }
    )

    assert (
        "crawl-site: 33001 of 40904 fetched HTML page bodies were not retained "
        "(credentialed 33001). Anything computed from stored HTML covers 7903 of 40904 pages"
    ) in capsys.readouterr().err


def test_a_run_that_discarded_nothing_says_nothing_extra(capsys):
    cli._print_crawl_outcome(
        {
            "finish_reason": "finished",
            "urls_collected": 9,
            "html_bodies": {"total": 9, "retained": 9, "omitted": {}},
        }
    )

    assert capsys.readouterr().err == "crawl-site: finished; 9 URLs fetched\n"
