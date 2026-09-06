"""render_document() against a stub Playwright, never a real browser or the
network (#18): the stub records every call so the wiring — which settings
reach which Playwright call — is what gets asserted, not Chromium's actual
behaviour.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

from seohead.crawl import settings as crawl_config
from seohead.tools.render import render_document


class _ConsoleMsg:
    def __init__(self, type_, text):
        self.type = type_
        self.text = text


class _FakePage:
    def __init__(self, html="<html><body>rendered</body></html>", scroll_height=500):
        self.html = html
        self.url = "https://example.com/"
        self.scroll_height = scroll_height
        self.routes = []
        self.handlers = {}
        self.evaluated = []
        self.wait_calls = []
        self.viewport_calls = []
        self.screenshot_calls = []
        self.goto_calls = []
        # Console messages "emitted" during this fake page's navigation, fed
        # to whatever handler render_document() registered via page.on().
        self.console_messages: list[_ConsoleMsg] = []

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def on(self, event, handler):
        self.handlers[event] = handler

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        console_handler = self.handlers.get("console")
        if console_handler:
            for msg in self.console_messages:
                console_handler(msg)

    def wait_for_timeout(self, ms):
        self.wait_calls.append(ms)

    def evaluate(self, script):
        self.evaluated.append(script)
        if "scrollHeight" in script:
            return self.scroll_height
        return None

    def set_viewport_size(self, size):
        self.viewport_calls.append(size)

    def content(self):
        return self.html

    def screenshot(self, path=None, full_page=None):
        self.screenshot_calls.append({"path": path, "full_page": full_page})


class _FakeContext:
    def __init__(self, page, **options):
        self.page = page
        self.options = options
        self.routes = []
        self.new_page_route_snapshots = []
        self.ws_routes = []
        self.closed = False

    def new_page(self):
        self.new_page_route_snapshots.append(list(self.routes))
        return self.page

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def route_web_socket(self, pattern, handler):
        self.ws_routes.append((pattern, handler))

    def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, context):
        self._context = context
        self.closed = False

    def new_context(self, **options):
        self._context.options.update(options)
        return self._context

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser, context):
        self._browser = browser
        self._context = context
        self.launched = False
        self.launch_persistent_calls = []

    def launch(self):
        self.launched = True
        return self._browser

    def launch_persistent_context(self, user_data_dir, **options):
        self.launch_persistent_calls.append((user_data_dir, options))
        self._context.options.update(options)
        return self._context


class _FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_stack(monkeypatch):
    """Stand in for the real ``playwright`` package via ``sys.modules``.

    render_document() does ``from playwright.sync_api import sync_playwright``
    at call time, so it never needs the real package to be installed --
    which is the point: rendering tests must pass in the plain ``pytest``
    matrix, where the optional ``render`` extra is not installed at all.
    ``monkeypatch.setattr("playwright.sync_api.sync_playwright", ...)`` would
    still import the real module just to resolve that dotted path, so a
    fake module registered directly in ``sys.modules`` is what actually
    keeps this test file independent of whether playwright is installed.
    """
    page = _FakePage()
    context = _FakeContext(page)
    browser = _FakeBrowser(context)
    chromium = _FakeChromium(browser, context)
    pw = _FakePlaywright(chromium)

    fake_playwright = types.ModuleType("playwright")
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: pw
    fake_playwright.sync_api = fake_sync_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    return {"page": page, "context": context, "browser": browser, "chromium": chromium}


def _rendering_config(**browser_overrides):
    resolved = crawl_config.load(overrides={"rendering.mode": "js"})
    resolved["rendering"]["browser"].update(browser_overrides)
    return resolved["rendering"]


def test_happy_path_returns_the_rendered_html(fake_stack):
    result = render_document("https://example.com/", _rendering_config())
    assert result["ok"] is True
    assert result["html"] == "<html><body>rendered</body></html>"
    assert result["final_url"] == "https://example.com/"


def test_pinned_route_is_registered_on_context_before_its_new_page(fake_stack):
    result = render_document("https://example.com/", _rendering_config())

    assert result["ok"] is True
    assert fake_stack["context"].routes[0][0] == "**/*"
    assert fake_stack["context"].new_page_route_snapshots == [fake_stack["context"].routes]
    assert fake_stack["page"].routes == []


def test_service_workers_are_blocked_by_default(fake_stack):
    render_document("https://example.com/", _rendering_config())
    assert fake_stack["context"].options["service_workers"] == "block"


def test_a_blocked_websocket_marks_the_rendered_dom_unavailable(fake_stack, monkeypatch):
    class WebSocket:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    websocket = WebSocket()
    original_goto = fake_stack["page"].goto

    def goto(url, **kwargs):
        original_goto(url, **kwargs)
        _pattern, handler = fake_stack["context"].ws_routes[0]
        handler(websocket)

    monkeypatch.setattr(fake_stack["page"], "goto", goto)

    result = render_document("https://example.com/", _rendering_config())

    assert result["ok"] is False
    assert "WebSocket" in result["error"]
    assert websocket.closed


def test_navigation_honours_the_configured_wait_until(fake_stack):
    render_document("https://example.com/", _rendering_config(wait_until="networkidle"))
    assert fake_stack["page"].goto_calls[0]["wait_until"] == "networkidle"


def test_script_timeout_is_a_wait_after_navigation(fake_stack):
    render_document("https://example.com/", _rendering_config(script_timeout_seconds=5))
    assert fake_stack["page"].wait_calls == [5000]


def test_zero_script_timeout_waits_not_at_all(fake_stack):
    render_document("https://example.com/", _rendering_config(script_timeout_seconds=0))
    assert fake_stack["page"].wait_calls == []


def test_resize_to_content_is_capped(fake_stack):
    fake_stack["page"].scroll_height = 99999
    render_document(
        "https://example.com/",
        _rendering_config(resize_to_content=True, resize_to_content_max_height_px=2000),
    )
    assert fake_stack["page"].viewport_calls[-1]["height"] == 2000


def test_resize_to_content_off_never_resizes(fake_stack):
    render_document("https://example.com/", _rendering_config(resize_to_content=False))
    assert fake_stack["page"].viewport_calls == []


# The dotted DEFAULTS paths behind the four tests below, spelled out so the coverage canary in
# test_crawl_settings_wired.py can see that they are exercised behaviourally here:
#   rendering.browser.flatten_shadow_dom
#   rendering.browser.flatten_iframes
#   rendering.browser.mobile_emulation
#   rendering.browser.touch_emulation
def test_flatten_shadow_dom_evaluates_the_flatten_script(fake_stack):
    render_document("https://example.com/", _rendering_config(flatten_shadow_dom=True))
    assert any("shadowRoot" in call for call in fake_stack["page"].evaluated)


def test_flatten_iframes_evaluates_the_flatten_script(fake_stack):
    render_document("https://example.com/", _rendering_config(flatten_iframes=True))
    assert any("contentDocument" in call for call in fake_stack["page"].evaluated)


def test_neither_flatten_script_runs_by_default(fake_stack):
    render_document("https://example.com/", _rendering_config())
    assert not any("shadowRoot" in call for call in fake_stack["page"].evaluated)
    assert not any("contentDocument" in call for call in fake_stack["page"].evaluated)


def test_device_emulation_settings_reach_the_context(fake_stack):
    render_document(
        "https://example.com/",
        _rendering_config(device_pixel_ratio=2.0, mobile_emulation=True, touch_emulation=True),
    )
    options = fake_stack["context"].options
    assert options["device_scale_factor"] == 2.0
    assert options["is_mobile"] is True
    assert options["has_touch"] is True


def test_console_errors_are_captured_when_enabled(fake_stack):
    config = _rendering_config()
    config["artifacts"]["console_errors"] = True
    fake_stack["page"].console_messages = [
        _ConsoleMsg("error", "boom"),
        _ConsoleMsg("log", "not an error"),
    ]
    result = render_document("https://example.com/", config)
    assert result["console_errors"] == ["boom"]


def test_console_messages_are_ignored_when_disabled(fake_stack):
    config = _rendering_config()
    config["artifacts"]["console_errors"] = False
    fake_stack["page"].console_messages = [_ConsoleMsg("error", "boom")]
    result = render_document("https://example.com/", config)
    assert result["console_errors"] == []


def test_screenshot_is_saved_when_enabled(fake_stack, tmp_path):
    config = _rendering_config()
    config["artifacts"]["screenshots"] = True
    result = render_document("https://example.com/", config, artifacts_dir=str(tmp_path))
    assert result["screenshot_path"] is not None
    assert fake_stack["page"].screenshot_calls[0]["full_page"] is True


def test_no_screenshot_without_an_artifacts_dir(fake_stack):
    config = _rendering_config()
    config["artifacts"]["screenshots"] = True
    result = render_document("https://example.com/", config, artifacts_dir=None)
    assert result["screenshot_path"] is None
    assert fake_stack["page"].screenshot_calls == []


def test_persistent_profile_is_refused_until_pinned_cookie_continuity_is_proven(
    fake_stack, tmp_path
):
    config = _rendering_config(
        persistent_profile=True, persistent_profile_dir=str(tmp_path / "profile")
    )
    result = render_document("https://example.com/", config)
    assert result["ok"] is False
    assert "cookie continuity" in result["error"]
    assert not fake_stack["chromium"].launch_persistent_calls
    assert fake_stack["chromium"].launched is False


def test_without_a_persistent_profile_the_ordinary_launch_path_is_used(fake_stack):
    render_document("https://example.com/", _rendering_config())
    assert fake_stack["chromium"].launched is True
    assert fake_stack["chromium"].launch_persistent_calls == []


def test_root_is_refused_before_any_playwright_call(fake_stack, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    result = render_document("https://example.com/", _rendering_config())
    assert result["ok"] is False
    assert "sandbox" in result["error"]
    assert fake_stack["chromium"].launched is False


def test_missing_playwright_is_reported_as_data(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake(name, *a, **kw):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    result = render_document("https://example.com/", _rendering_config())
    assert result["ok"] is False
    assert "playwright install chromium" in result["install"]


def test_a_private_target_is_refused_before_launching_a_browser(fake_stack):
    result = render_document("http://127.0.0.1:9222/", _rendering_config())
    assert result["ok"] is False
    assert fake_stack["chromium"].launched is False


# ── the rendered page must be fetched as the same client as the raw crawl ────


class _VaryingPage(_FakePage):
    """A page whose body depends on who asked for it.

    Stands in for a server sending ``Vary: User-Agent`` — legal, common, and
    nothing to do with JavaScript. The context's options are read at
    ``content()`` time because that is when render_document has finished
    configuring the browser identity.
    """

    def __init__(self, context_options: dict, bodies: dict[str, str]):
        super().__init__()
        self._options = context_options
        self._bodies = bodies

    def content(self):
        seen = self._options.get("user_agent")
        return self._bodies.get(seen, self._bodies["__other__"])


def test_the_browser_identifies_as_the_crawl_did(fake_stack):
    """The fuller fetch replaces a page's body-derived evidence — title, word count,
    links — so it must ask the origin as the same client the static crawl used. Without
    an explicit User-Agent the context gets Chromium's own, which in headless mode
    advertises HeadlessChrome, and a report then mixes two populations: escalated pages
    described from what the server serves a headless browser, every other page from what
    it serves the toolkit. #199 fixed this for the single-page render_check() probe; this
    is the same confound at the crawl-wide call site.
    """
    from seohead.recon.net import UA

    render_document("https://example.com/", _rendering_config(), user_agent=UA)

    assert fake_stack["context"].options["user_agent"] == UA


def test_an_explicit_crawl_user_agent_reaches_the_browser(fake_stack):
    """``http.user_agent`` is a setting an operator sets deliberately — often because a
    site is allow-listed by it. A rendered fetch that ignores it is refused entry, or
    served something different, on exactly the sites where the setting was needed."""
    render_document("https://example.com/", _rendering_config(), user_agent="AcmeAudit/2.0")

    assert fake_stack["context"].options["user_agent"] == "AcmeAudit/2.0"


def test_a_varying_server_returns_the_crawls_own_body(monkeypatch):
    """End to end through the stub: the body render_document brings back is the one the
    origin serves the crawl's identity, not the one it serves an unrecognised client."""
    from seohead.recon.net import UA

    context_options: dict = {}
    page = _VaryingPage(
        context_options,
        {
            UA: "<html><body>the real page</body></html>",
            "__other__": "<html><body>are you a robot?</body></html>",
        },
    )
    context = _FakeContext(page)
    context.options = context_options
    browser = _FakeBrowser(context)
    chromium = _FakeChromium(browser, context)
    pw = _FakePlaywright(chromium)

    fake_playwright = types.ModuleType("playwright")
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: pw
    fake_playwright.sync_api = fake_sync_api
    monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

    result = render_document("https://example.com/", _rendering_config(), user_agent=UA)

    assert result["ok"] is True
    assert "the real page" in result["html"]
