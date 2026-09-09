"""Owned-loopback Chromium regression for #670 dynamic mobile serving."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright.sync_api")

from seohead.tools.render import MOBILE_USER_AGENT, render_check


@pytest.fixture
def dynamic_site(monkeypatch):
    requests: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            user_agent = self.headers.get("User-Agent", "")
            mobile = "Mobile" in user_agent or "iPhone" in user_agent
            requests.append(
                {
                    "path": self.path,
                    "user_agent": user_agent,
                    "branch": "mobile" if mobile else "desktop",
                }
            )
            if mobile:
                html = (
                    '<html><head><title>Mobile shell</title><meta name="viewport" '
                    'content="width=device-width"></head><body><div id="root"></div><script>'
                    'document.getElementById("root").innerHTML = "<h1>Mobile jobs</h1>" '
                    '+ "Job description ".repeat(100)</script></body></html>'
                )
            else:
                html = (
                    '<html><head><title>Desktop jobs</title><meta name="viewport" '
                    'content="width=1210"></head><body><h1>Desktop jobs</h1><p>'
                    + "Job description " * 100
                    + "</p></body></html>"
                )
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Vary", "User-Agent")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_HOSTS", "127.0.0.1")
    try:
        yield f"http://127.0.0.1:{server.server_port}/jobs/", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _render_or_skip(url: str, viewport: str) -> dict:
    result = render_check(url, viewport=viewport, timeout=10)
    if not result.get("ok") and "Executable doesn't exist" in result.get("error", ""):
        pytest.skip("Chromium is not installed for the optional render extra")
    assert result["ok"], result
    return result


def test_dynamic_serving_mobile_identity_reaches_the_mobile_representation(dynamic_site):
    url, requests = dynamic_site

    desktop = _render_or_skip(url, "desktop")
    desktop_requests = list(requests)
    requests.clear()
    mobile = _render_or_skip(url, "mobile")
    mobile_requests = list(requests)

    assert desktop["user_agent"] != MOBILE_USER_AGENT
    assert desktop["raw"]["title"] == desktop["rendered"]["title"] == "Desktop jobs"
    assert all(request["branch"] == "desktop" for request in desktop_requests)

    assert mobile["user_agent"] == MOBILE_USER_AGENT
    assert mobile["viewport_size"] == {"width": 390, "height": 844}
    assert mobile["raw"]["title"] == mobile["rendered"]["title"] == "Mobile shell"
    assert mobile["js_dependent"] is True
    assert all(request["branch"] == "mobile" for request in mobile_requests)
    assert all(request["user_agent"] == MOBILE_USER_AGENT for request in mobile_requests)
