"""Compare raw HTML with the DOM through pure functions, without launching Chromium."""

from __future__ import annotations

import json
import pathlib
import re

from seohead.tools import dualcrawl
from seohead.tools.render import (
    _jsonld_types,
    _links,
    _snapshot,
    _words,
    compare,
    detect_empty_shell,
    render_check,
)

BASE = "https://example.com/"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _snap(**kw):
    base = {
        "words": 400,
        "links": 30,
        "title": "Pumps",
        "h1": "Pumps",
        "canonical": "https://example.com/",
        "jsonld_types": ["Product"],
        "html_bytes": 5000,
    }
    base.update(kw)
    return base


# ── Document snapshot ────────────────────────────────────────────────────────


def test_scripts_and_styles_are_not_content():
    """Inline JavaScript and CSS must not inflate the visible word count."""
    html = "<body><script>var a=1;var b=2;</script><style>.x{color:red}</style><p>visible text here</p></body>"
    assert _words(html) == 3


def test_only_internal_links_are_counted():
    html = (
        '<a href="/catalog">Catalog</a><a href="https://example.com/about">About</a>'
        '<a href="https://example.org/external">External</a><a href="#top">Top</a>'
        '<a href="tel:+79001112233">Phone</a>'
    )
    assert _links(html, BASE) == {"https://example.com/catalog", "https://example.com/about"}


def test_script_literal_anchor_markup_is_not_raw_link_evidence():
    raw_html = """<body><div id="root"></div><script>
    document.getElementById("root").innerHTML =
    '<nav><a href="/catalog">Catalog</a><a href="/about">About</a></nav>';
    </script></body>"""
    rendered_html = (
        '<body><div id="root"><nav><a href="/catalog">Catalog</a>'
        '<a href="/about">About</a></nav></div></body>'
    )

    raw = _snapshot(raw_html, BASE)
    rendered = _snapshot(rendered_html, BASE)
    assert raw["links"] == 0
    assert rendered["links"] == 2
    assert any(
        "2 of 2 internal links appear only after JavaScript" in finding
        for finding in compare(raw, rendered)
    )

    evidence = dualcrawl.compare_evidence(
        {BASE: {"links": _links(raw_html, BASE), "images": set()}},
        {BASE: {"links": _links(rendered_html, BASE), "images": set()}},
    )
    assert evidence["summary"]["only_in_b"] == 2


def test_jsonld_types_are_pulled_from_nested_graph():
    html = (
        '<script type="application/ld+json">'
        '{"@graph":[{"@type":"Organization"},{"@type":["Product","Offer"]}]}</script>'
    )
    assert _jsonld_types(html) == ["Offer", "Organization", "Product"]


def test_broken_jsonld_is_skipped_not_fatal():
    assert _jsonld_types('<script type="application/ld+json">{broken</script>') == []


def test_empty_spa_shell_is_detected():
    for shell in ("root", "app", "__next", "__nuxt"):
        assert detect_empty_shell(f'<body><div id="{shell}"></div></body>') == shell


def test_shell_with_content_is_not_an_empty_shell():
    assert detect_empty_shell('<body><div id="root"><h1>Already rendered</h1></div></body>') is None


def test_snapshot_is_computed_the_same_way_for_both_sides():
    html = "<html><head><title>T</title></head><body><h1>H</h1><p>one two three</p></body></html>"
    snap = _snapshot(html, BASE)
    assert snap["title"] == "T" and snap["h1"] == "H" and snap["words"] >= 3


def test_snapshot_sees_a_background_image_with_no_img_tag_at_all():
    """The flagship case: a page with images only as CSS backgrounds."""
    html = "<html><head><style>.hero{background-image:url(/hero.png)}</style></head><body></body></html>"
    snap = _snapshot(html, BASE)
    assert snap["images"] == ["https://example.com/hero.png"]


# ── Findings ─────────────────────────────────────────────────────────────────


def test_empty_shell_is_the_headline_finding():
    out = compare(_snap(words=5), _snap(words=800), shell="root")
    assert "empty <div" in out[0]
    assert "receives an empty page" in out[0]


def test_text_appearing_only_after_js_is_reported_with_a_share():
    out = compare(_snap(words=200), _snap(words=1000))
    assert any("80% of page copy appears only after JavaScript" in f for f in out)


def test_small_js_additions_are_not_alarming():
    """A five-percent copy increase is likely a widget, not a rendering problem."""
    out = compare(_snap(words=950), _snap(words=1000))
    assert not any("page copy appears only after JavaScript" in f for f in out)


def test_links_invisible_without_js_are_reported():
    out = compare(_snap(links=0), _snap(links=40))
    assert any("internal links appear" in f for f in out)


def test_title_rewritten_by_script_is_flagged():
    out = compare(_snap(title="Loading…"), _snap(title="Buy CDM Pumps"))
    assert any("title changes after JavaScript" in f for f in out)


def test_canonical_drawn_by_script_is_flagged():
    out = compare(_snap(canonical=""), _snap(canonical="https://example.com/x"))
    assert any("canonical" in f for f in out)


def test_schema_added_only_after_js_is_flagged():
    out = compare(_snap(jsonld_types=[]), _snap(jsonld_types=["Product", "BreadcrumbList"]))
    assert any("Schema.org types appear only after JavaScript" in f for f in out)
    assert any("BreadcrumbList" in f for f in out)


def test_identical_pages_get_an_explicit_all_clear():
    out = compare(_snap(), _snap())
    assert len(out) == 1 and "materially equivalent" in out[0]


def test_background_image_invisible_to_the_raw_pass_is_flagged():
    out = compare(_snap(images=[]), _snap(images=["https://example.com/hero.png"]))
    assert any("visible only after rendering" in f for f in out)


def test_identical_images_on_both_sides_raise_no_finding():
    same = ["https://example.com/hero.png"]
    out = compare(_snap(images=same), _snap(images=same))
    assert not any("visible only after rendering" in f for f in out)


# ── Boundaries ───────────────────────────────────────────────────────────────


def test_empty_url_is_data_not_a_crash():
    assert render_check("")["ok"] is False
    assert render_check("   ")["ok"] is False


def test_missing_playwright_is_reported_with_the_install_command(monkeypatch):
    """A missing browser is result data and includes the exact install command."""
    import builtins

    real_import = builtins.__import__

    def fake(name, *a, **kw):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    r = render_check("https://example.com/")
    assert r["ok"] is False and "Playwright" in r["error"]
    assert "playwright install chromium" in r["install"]


def test_all_clear_is_a_single_shared_constant():
    """The shared all-clear constant keeps ``js_dependent`` and findings aligned."""
    from seohead.tools.render import ALL_CLEAR

    assert compare(_snap(), _snap()) == [ALL_CLEAR]


def test_lcp_is_collected_by_a_buffered_observer():
    """A buffered pre-navigation observer captures LCP when the entry API is empty."""
    from seohead.tools.render import _CLS_INIT_JS, _METRICS_JS

    assert "largest-contentful-paint" in _CLS_INIT_JS
    assert "buffered: true" in _CLS_INIT_JS
    assert "__seohead_lcp" in _METRICS_JS


def test_background_images_js_reads_computed_style_not_html_text():
    """Only getComputedStyle resolves a background declared in an external stylesheet."""
    from seohead.tools.render import _BACKGROUND_IMAGES_JS

    assert "getComputedStyle" in _BACKGROUND_IMAGES_JS
    assert "backgroundImage" in _BACKGROUND_IMAGES_JS
    assert "data:" in _BACKGROUND_IMAGES_JS  # data URIs are skipped, matching the parser


# ── Viewport presets (#18) ───────────────────────────────────────────────────


def test_viewport_presets_cover_desktop_and_mobile():
    from seohead.tools.render import VIEWPORT_PRESETS

    assert VIEWPORT_PRESETS["desktop"] == {"width": 1366, "height": 768}
    assert VIEWPORT_PRESETS["mobile"] == {"width": 390, "height": 844}


# ── Legacy _escaped_fragment_ scheme (#18) ──────────────────────────────────


def test_a_hash_bang_url_produces_its_escaped_fragment_url():
    from seohead.tools.render import legacy_fragment_target

    target = legacy_fragment_target("https://example.com/app#!/product/1", "")
    assert target == "https://example.com/app?_escaped_fragment_=%2Fproduct%2F1"


def test_a_page_wide_fragment_meta_tag_is_honoured_even_without_a_hash_bang():
    from seohead.tools.render import legacy_fragment_target

    html = '<meta name="fragment" content="!">'
    target = legacy_fragment_target("https://example.com/app", html)
    assert target == "https://example.com/app?_escaped_fragment_="


def test_a_page_with_neither_signal_has_no_legacy_fragment_target():
    from seohead.tools.render import legacy_fragment_target

    assert legacy_fragment_target("https://example.com/app", "<html></html>") is None


def test_an_existing_query_string_is_preserved_alongside_the_escaped_fragment():
    from seohead.tools.render import legacy_fragment_target

    target = legacy_fragment_target("https://example.com/app?lang=en#!/x", "")
    assert "lang=en" in target
    assert "_escaped_fragment_=%2Fx" in target


# ── Security preconditions (#18) ─────────────────────────────────────────────


class _FakeRequest:
    def __init__(self, url):
        self.url = url


class _FakeRoute:
    def __init__(self, url):
        self.request = _FakeRequest(url)
        self.continued = False
        self.aborted_with = None

    def continue_(self):
        self.continued = True

    def abort(self, reason):
        self.aborted_with = reason


def test_a_request_to_a_private_address_is_aborted():
    from seohead.tools.render import _guard_browser_route

    route = _FakeRoute("http://127.0.0.1:9222/internal")
    _guard_browser_route(route)
    assert route.aborted_with == "blockedbyclient"
    assert route.continued is False


def test_fallback_browser_route_fails_closed_without_a_pinned_fulfiller():
    from seohead.tools.render import _guard_browser_route

    route = _FakeRoute("https://example.com/style.css")
    _guard_browser_route(route)
    assert route.aborted_with == "blockedbyclient"
    assert route.continued is False


def test_fallback_browser_route_never_continues_a_non_network_scheme():
    from seohead.tools.render import _guard_browser_route

    for scheme_url in ("about:blank", "blob:https://example.com/x", "data:text/plain;base64,aGk="):
        route = _FakeRoute(scheme_url)
        _guard_browser_route(route)
        assert route.aborted_with == "blockedbyclient"
        assert route.continued is False


def test_root_is_refused_rather_than_unsandboxed(monkeypatch):
    import os

    from seohead.tools.render import _refuse_if_root

    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    try:
        _refuse_if_root()
        raise AssertionError("root must be refused")
    except RuntimeError as exc:
        assert "sandbox" in str(exc)


def test_a_non_root_user_is_not_refused(monkeypatch):
    import os

    from seohead.tools.render import _refuse_if_root

    monkeypatch.setattr(os, "geteuid", lambda: 501, raising=False)
    _refuse_if_root()  # must not raise


class _FakeWebSocketRoute:
    def __init__(self, url):
        self.url = url
        self.closed = False
        self.connected = False

    def close(self):
        self.closed = True

    def connect_to_server(self):
        self.connected = True


def test_a_websocket_to_a_private_address_is_closed_not_connected():
    from seohead.tools.render import _guard_websocket_route

    route = _FakeWebSocketRoute("ws://127.0.0.1:9222/socket")
    _guard_websocket_route(route)
    assert route.closed is True
    assert route.connected is False


def test_a_websocket_is_closed_without_a_pinned_transport():
    from seohead.tools.render import _guard_websocket_route

    route = _FakeWebSocketRoute("wss://example.com/socket")
    _guard_websocket_route(route)
    assert route.closed is True
    assert route.connected is False


def test_a_websocket_with_an_unsupported_scheme_is_closed():
    from seohead.tools.render import _guard_websocket_route

    route = _FakeWebSocketRoute("file:///etc/passwd")
    _guard_websocket_route(route)
    assert route.closed is True


# ── Published examples must use real output keys (#271) ─────────────────────


def test_rendering_scenario_examples_use_real_snapshot_keys():
    """docs/scenarios/rendering.md's example output must be reproducible by a reader
    typing it in: a key the tool never returns (e.g. `internal_links`, when `_snapshot`
    actually calls it `links`) would silently mislead every future reader."""
    snapshot_keys = set(_snapshot("<a href='/x'>x</a>", BASE))
    text = (ROOT / "docs" / "scenarios" / "rendering.md").read_text(encoding="utf-8")
    checked_a_block = False
    for block in re.findall(r"```json\n(.*?)\n```", text, re.S):
        payload = json.loads(block)
        for side in ("raw", "rendered"):
            if side in payload:
                checked_a_block = True
                bad = set(payload[side]) - snapshot_keys
                assert not bad, f"{side} example uses keys _snapshot never returns: {bad}"
    assert checked_a_block, "expected at least one raw/rendered example block to check"
