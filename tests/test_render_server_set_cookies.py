"""Issue #656: a cookie the site set is not the operator's credential, in the render lane.

``render.py::_capture_request`` upgraded ``credentials_used`` the moment the browser
sent ``Cookie:`` on any request. A headless browser accumulates the cookies the site
itself set, so a page that sets a session cookie and then asks for one same-origin
subresource -- the ordinary shape of the web -- had its own serialized DOM stored as
``omitted``/``credentialed``, on a run configured with no credentials at all.
``sqlite_render._policy_facts`` already derives that fact from configuration.

Three properties, on a loopback fixture site driven by a stub browser -- no network
and no Chromium, the same discipline as ``test_render_document.py``:

- a site that sets a session cookie keeps its rendered DOMs;
- a run the operator configured a credential header for still omits them, because
  that rule is the correct one and this must not weaken it;
- a run that discarded any DOM says how many, and why, where an operator reads it.
"""

from __future__ import annotations

import http.server
import json
import re
import sqlite3
import sys
import threading
import types
from collections.abc import Iterator

import pytest

from seohead import cli
from seohead.crawl import sqlite_render
from seohead.servers import handlers
from seohead.storage.corpus import rendered_body_retention

BUILD = "a" * 40
_SRC = re.compile(r"src=['\"]([^'\"]+)['\"]")

# One page, so the fixture measures the rendering lane alone. The crawl's own
# shared client picks the same cookie up on any second page, which is #647 --
# a separate defect in a separate lane, and not what this file is about.
HOME = (
    "<html><head><title>Home</title>"
    "<meta name='description' content='Home of the cookie fixture site, described at length.'>"
    "</head><body><h1>Home</h1><p>Home of the cookie fixture site.</p>"
    "<script src='/app.js'></script></body></html>"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Sets one session cookie on its home page, like most of the web."""

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
        if path == "/app.js":
            return self._send(b"window.hydrated=1;\n", "text/javascript; charset=utf-8")
        if path == "/":
            return self._send(
                HOME.encode(),
                "text/html; charset=utf-8",
                extra={"Set-Cookie": "session=abc123; Path=/"},
            )
        return self._send(b"not found", "text/plain; charset=utf-8", status=404)

    def log_message(self, format: str, *args) -> None:
        pass


class _Request:
    def __init__(self, url: str, headers: dict[str, str]):
        self.url = url
        self.method = "GET"
        self._headers = headers

    def all_headers(self) -> dict[str, str]:
        return dict(self._headers)


class _Response:
    def __init__(self, headers: dict[str, str]):
        self._headers = headers

    def all_headers(self) -> dict[str, str]:
        return dict(self._headers)


class _Route:
    def __init__(self, request: _Request):
        self.request = request
        self.status = 0
        self.headers: dict[str, str] = {}
        self.body = b""
        self.aborted = ""

    def fulfill(self, *, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body

    def abort(self, reason: str) -> None:
        self.aborted = reason


class _Page:
    """A stub page that fetches through the pinned route and keeps a cookie jar.

    The jar is what the defect fed on: a browser stores what ``Set-Cookie`` gave it
    and puts it back on every later request from the same context, subresources
    included.
    """

    def __init__(self, context: _Context):
        self.context = context
        self.url = ""
        self.html = ""
        self.handlers: dict[str, object] = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def _fetch(self, url: str) -> bytes:
        headers = {"accept": "*/*", "user-agent": "stub"}
        if self.context.cookies:
            headers["cookie"] = "; ".join(
                f"{name}={value}" for name, value in self.context.cookies.items()
            )
        request = _Request(url, headers)
        if "request" in self.handlers:
            self.handlers["request"](request)
        route = _Route(request)
        self.context.route_handler(route)
        for line in str(route.headers.get("set-cookie", "")).splitlines():
            pair = line.split(";", 1)[0].strip()
            if "=" in pair:
                name, _, value = pair.partition("=")
                self.context.cookies[name] = value
        if "response" in self.handlers:
            self.handlers["response"](_Response(route.headers))
        return route.body

    def goto(self, url, wait_until=None, timeout=None):
        document = self._fetch(url).decode("utf-8")
        self.url = url
        origin = "/".join(url.split("/")[:3])
        for src in _SRC.findall(document):
            self._fetch(origin + src if src.startswith("/") else src)
        # What a browser hands back after scripts ran, never the response bytes.
        self.html = document.replace("</body>", "<p>hydrated</p></body>")

    def wait_for_timeout(self, ms):
        return None

    def evaluate(self, script):
        if "scrollHeight" in script:
            return 500
        if "TextEncoder" in script:
            return {"complete": True, "bytes": len(self.html.encode()), "html": self.html}
        return 0

    def set_viewport_size(self, size):
        return None

    def content(self):
        return self.html

    def screenshot(self, path=None, full_page=None):
        return None


class _Context:
    def __init__(self):
        self.cookies: dict[str, str] = {}
        self.route_handler = None

    def route(self, pattern, handler):
        self.route_handler = handler

    def route_web_socket(self, pattern, handler):
        return None

    def new_page(self):
        return _Page(self)

    def close(self):
        return None


class _Browser:
    version = "stub-chromium"

    def new_context(self, **options):
        # One context per render, exactly as render_document builds it: the jar
        # a real browser keeps lives and dies with the context.
        return _Context()

    def close(self):
        return None


class _Chromium:
    def launch(self, **options):
        return _Browser()


class _Playwright:
    chromium = _Chromium()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def site(monkeypatch) -> Iterator[str]:
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    fake_playwright = types.ModuleType("playwright")
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = _Playwright
    fake_playwright.sync_api = fake_sync_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)
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
    settings = {
        "rendering.mode": "js",
        "rendering.escalation.sample_per_pattern": 1,
        "rendering.escalation.max_render_urls": 10,
    }
    settings.update(overrides or {})
    result = handlers.crawl_site(
        url=f"{site}/",
        scan_out=scan_out,
        max_urls=10,
        min_delay=0,
        concurrency=1,
        overrides=settings,
        producer_build=BUILD,
    )
    con = sqlite3.connect(scan_out)
    try:
        documents = dict(
            con.execute(
                "SELECT body_state || '/' || body_reason, COUNT(*) FROM documents "
                "WHERE representation='rendered' GROUP BY 1"
            ).fetchall()
        )
    finally:
        con.close()
    return result, documents


def test_a_server_set_cookie_does_not_discard_rendered_doms(site, tmp_path):
    result, documents = _crawl(site, tmp_path)

    assert documents and set(documents) == {"complete/none"}, (
        "a session cookie is not the operator's credential"
    )
    assert result["rendered_bodies"]["omitted"] == {}


def test_the_session_cookie_value_is_nowhere_in_the_artifact(site, tmp_path):
    """Retaining the DOM is not permission to store the cookie that came with it."""
    _crawl(site, tmp_path)

    assert b"abc123" not in (tmp_path / "scan.sqlite").read_bytes()


def test_an_operator_credential_header_still_discards_rendered_doms(site, tmp_path, monkeypatch):
    monkeypatch.setenv("SEOHEAD_TEST_TOKEN", "Bearer operator-secret")
    # A credentialed run retains no static body either -- the same rule, one lane
    # earlier -- so the probe would find nothing to compare against and never reach
    # a browser at all. The fixture supplies the retained body it would read, so
    # this test measures the rendering lane's own decision and nothing else.
    monkeypatch.setattr(sqlite_render, "_static_html", lambda *_args: HOME)
    result, documents = _crawl(
        site,
        tmp_path,
        overrides={
            "http.credential_headers": [
                {"host": "127.0.0.1", "headers": {"authorization": "env:SEOHEAD_TEST_TOKEN"}}
            ],
            "http.credentials_acknowledged": True,
        },
    )

    assert documents == {"omitted/credentialed": 1}, "the privacy rule must not weaken"
    assert result["rendered_bodies"] == {
        "total": 1,
        "retained": 0,
        "omitted": {"credentialed": 1},
    }


def test_a_discarded_dom_keeps_its_reason_in_the_artifact(site, tmp_path, monkeypatch):
    """A finished scan still answers why a DOM is missing, and what configured that."""
    monkeypatch.setenv("SEOHEAD_TEST_TOKEN", "Bearer operator-secret")
    monkeypatch.setattr(sqlite_render, "_static_html", lambda *_args: HOME)
    _crawl(
        site,
        tmp_path,
        overrides={
            "http.credential_headers": [
                {"host": "127.0.0.1", "headers": {"authorization": "env:SEOHEAD_TEST_TOKEN"}}
            ],
            "http.credentials_acknowledged": True,
        },
    )

    con = sqlite3.connect(str(tmp_path / "scan.sqlite"))
    con.row_factory = sqlite3.Row
    try:
        recorded = rendered_body_retention(con)
        document = con.execute(
            "SELECT body_reason,renderer_json FROM documents WHERE representation='rendered'"
        ).fetchone()
        config = json.loads(con.execute("SELECT config_json FROM scan").fetchone()[0])
    finally:
        con.close()

    assert recorded == {"total": 1, "retained": 0, "omitted": {"credentialed": 1}}
    assert document["body_reason"] == "credentialed"
    assert json.loads(document["renderer_json"])["capture_limitations"] == ["credentialed"]
    # What made it credentialed, in the artifact's own redacted configuration:
    # the header name and its host, never the value.
    assert config["http"]["credential_headers"] == [
        {"host": "127.0.0.1", "headers": {"authorization": "REDACTED"}}
    ]
    assert config["rendering"]["browser"]["persistent_profile"] is False
    assert b"operator-secret" not in (tmp_path / "scan.sqlite").read_bytes()


def test_the_summary_names_how_many_doms_a_run_discarded(capsys):
    cli._print_crawl_outcome(
        {
            "finish_reason": "finished",
            "urls_collected": 40920,
            "rendered_bodies": {
                "total": 40904,
                "retained": 7903,
                "omitted": {"credentialed": 33001},
            },
        }
    )

    assert (
        "crawl-site: 33001 of 40904 rendered DOMs were not retained (credentialed 33001). "
        "Anything computed from a stored rendered DOM covers 7903 of 40904 pages"
    ) in capsys.readouterr().err


def test_a_run_that_discarded_nothing_says_nothing_extra(capsys):
    cli._print_crawl_outcome(
        {
            "finish_reason": "finished",
            "urls_collected": 9,
            "rendered_bodies": {"total": 9, "retained": 9, "omitted": {}},
        }
    )

    assert capsys.readouterr().err == "crawl-site: finished; 9 URLs fetched\n"
