"""render_check() against a stub Playwright and a stub HTTP client, never a real browser or
the network -- see test_render_document.py for the same discipline applied to
render_document().

#199: the raw fetch and the rendered fetch must present the same request identity to the
origin. Without that, Chromium's own default User-Agent reaches the origin while the raw
fetch used the toolkit's identifiable one, and a server that varies its response by
User-Agent -- legal, common, and unrelated to JavaScript -- becomes indistinguishable from a
page that genuinely needs a renderer.
"""

from __future__ import annotations

import sys
import types

import httpx
import pytest

from seohead.recon.net import UA
from seohead.tools import render as render_module
from seohead.tools.render import render_check


class _FakeResponse:
    def __init__(self, text, url="https://example.com/", status_code=200):
        self.text = text
        self.url = url
        self.status_code = status_code


class _FakeHttpClient:
    def __init__(self, response):
        self._response = response
        self.closed = False

    def get(self, _url):
        return self._response

    def close(self):
        self.closed = True


class _FakePage:
    def __init__(self, html):
        self.html = html
        self.url = "https://example.com/"
        self.routes = []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def goto(self, _url, wait_until=None, timeout=None):
        pass

    def content(self):
        return self.html

    def evaluate(self, script):
        # Stands in for both _METRICS_JS and _BACKGROUND_IMAGES_JS -- render_check only
        # needs a shape it can iterate/index, not real Core Web Vitals.
        return [] if "backgroundImage" in script else {}


class _FakeContext:
    def __init__(self, page):
        self.page = page
        self.options: dict[str, object] = {}
        self.routes = []
        self.new_page_route_snapshots = []

    def add_init_script(self, _script):
        pass

    def route_web_socket(self, _pattern, _handler):
        pass

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def new_page(self):
        self.new_page_route_snapshots.append(list(self.routes))
        return self.page

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, context):
        self._context = context

    def new_context(self, **options):
        self._context.options.update(options)
        return self._context

    def close(self):
        pass


class _FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self):
        return self._browser


class _FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_stack(monkeypatch):
    """A raw fetch and a rendered fetch of two script-free documents -- shaped after #199's
    own offline reproducer, so the harness stands for exactly the case it describes: a
    server-side User-Agent variant with no JavaScript involved anywhere.
    """
    raw_html = (
        "<html><head><title>Raw</title></head><body><p>" + "raw " * 100 + "</p></body></html>"
    )
    rendered_html = (
        "<html><head><title>Raw</title></head><body><p>"
        + "raw " * 100
        + "chromium " * 220
        + "</p></body></html>"
    )
    page = _FakePage(rendered_html)
    context = _FakeContext(page)
    browser = _FakeBrowser(context)
    chromium = _FakeChromium(browser)
    pw = _FakePlaywright(chromium)

    fake_playwright = types.ModuleType("playwright")
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: pw
    fake_playwright.sync_api = fake_sync_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    monkeypatch.setattr(
        render_module,
        "http_client",
        lambda _timeout, **_kwargs: (_FakeHttpClient(_FakeResponse(raw_html)), True),
    )
    monkeypatch.setattr(render_module, "validate_url", lambda url: url)
    monkeypatch.setattr(render_module, "_refuse_if_root", lambda: None)

    return {"page": page, "context": context, "browser": browser, "chromium": chromium}


def test_the_rendered_browser_context_shares_the_raw_fetchs_user_agent(fake_stack):
    render_check("https://example.com/")
    assert fake_stack["context"].options.get("user_agent") == UA


def test_the_shared_identity_is_recorded_in_the_result(fake_stack):
    result = render_check("https://example.com/")
    assert result["ok"] is True
    assert result["user_agent"] == UA


def test_each_render_entry_registers_the_pinned_route_before_new_page(fake_stack):
    checked = render_check("https://example.com/")
    rendered = render_module.rendered_html("https://example.com/")

    assert checked["ok"] is True
    assert rendered["ok"] is True
    assert all(
        snapshot[-1][0] == "**/*" for snapshot in fake_stack["context"].new_page_route_snapshots
    )
    assert [pattern for pattern, _handler in fake_stack["context"].routes] == ["**/*", "**/*"]
    assert fake_stack["page"].routes == []


class _PinnedRequest:
    url = "https://example.com/"
    method = "GET"

    def all_headers(self):
        return {"accept": "text/html", "host": "example.com"}


class _PinnedRoute:
    request = _PinnedRequest()

    def __init__(self):
        self.fulfilled = []
        self.aborted = []

    def fulfill(self, **kwargs):
        self.fulfilled.append(kwargs)

    def abort(self, reason):
        self.aborted.append(reason)


class _PinnedStream(httpx.SyncByteStream):
    def __init__(self, body):
        self.body = body

    def __iter__(self):
        yield self.body

    def close(self):
        pass


def _deliver_route_during_navigation(monkeypatch, fake_stack, route):
    original_goto = fake_stack["page"].goto

    def goto(url, **kwargs):
        original_goto(url, **kwargs)
        fake_stack["context"].routes[-1][1](route)

    monkeypatch.setattr(fake_stack["page"], "goto", goto)


def test_rendered_html_fulfils_its_registered_pinned_route(monkeypatch, fake_stack):
    expected_html = "<html><body>pinned response</body></html>"

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, stream=_PinnedStream(expected_html.encode()))
        )
    )
    monkeypatch.setattr(render_module, "http_client", lambda *_args, **_kwargs: (client, False))
    fake_stack["page"].html = expected_html
    route = _PinnedRoute()
    gate_calls = []
    _deliver_route_during_navigation(monkeypatch, fake_stack, route)

    result = render_module.rendered_html(
        "https://example.com/", request_gate=lambda: gate_calls.append("called")
    )

    assert result == {
        "ok": True,
        "url": "https://example.com/",
        "html": expected_html,
    }
    assert route.fulfilled[0]["body"] == expected_html.encode()
    assert route.aborted == []
    assert gate_calls == ["called"]
    assert client.is_closed


def test_render_check_gates_each_raw_redirect_and_pinned_browser_request(monkeypatch, fake_stack):
    clients = []
    raw_requests = []

    def raw_transport(request):
        raw_requests.append(str(request.url))
        if len(raw_requests) == 1:
            return httpx.Response(302, headers={"location": "/redirected"}, request=request)
        return httpx.Response(200, text=fake_stack["page"].html, request=request)

    def http_client(_timeout, **kwargs):
        if "event_hooks" in kwargs:
            client = httpx.Client(
                transport=httpx.MockTransport(raw_transport),
                follow_redirects=True,
                event_hooks=kwargs["event_hooks"],
            )
        else:
            client = httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        200, stream=_PinnedStream(b"<html><body>pinned response</body></html>")
                    )
                )
            )
        clients.append(client)
        return client, False

    monkeypatch.setattr(render_module, "http_client", http_client)
    route = _PinnedRoute()
    gate_calls = []
    _deliver_route_during_navigation(monkeypatch, fake_stack, route)

    result = render_check("https://example.com/", request_gate=lambda: gate_calls.append("called"))

    assert result["ok"] is True
    assert len(raw_requests) == 2
    assert len(gate_calls) == 3
    assert route.fulfilled[0]["body"] == b"<html><body>pinned response</body></html>"
    assert all(client.is_closed for client in clients)
