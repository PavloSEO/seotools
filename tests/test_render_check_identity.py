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

    def route(self, _pattern, _handler):
        pass

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

    def add_init_script(self, _script):
        pass

    def route_web_socket(self, _pattern, _handler):
        pass

    def new_page(self):
        return self.page


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
