"""UNSUPPORTED_PLUGIN (#385): legacy plugin-dependent elements (<object>/<embed>/<applet>).

An <object> whose type declares an image (an inline SVG or PDF fallback) is a benign,
universally-supported use and must stay silent -- only genuine plugin content fires.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules
from seohead.tools.parser import unsupported_plugin_count


def test_embed_element_counts_as_a_plugin():
    html = '<html><body><embed src="game.swf" type="application/x-shockwave-flash"></body></html>'
    soup = BeautifulSoup(html, features="lxml")
    assert unsupported_plugin_count(soup) == 1


def test_applet_element_always_counts():
    html = "<html><body><applet code='Game.class'></applet></body></html>"
    soup = BeautifulSoup(html, features="lxml")
    assert unsupported_plugin_count(soup) == 1


def test_object_with_image_type_is_excluded():
    """An SVG/raster fallback via <object type="image/..."> renders in every browser,
    mobile included -- it must not be counted as plugin content."""
    html = '<html><body><object type="image/svg+xml" data="icon.svg"></object></body></html>'
    soup = BeautifulSoup(html, features="lxml")
    assert unsupported_plugin_count(soup) == 0


def test_object_with_flash_type_still_counts():
    html = (
        '<html><body><object type="application/x-shockwave-flash" data="movie.swf">'
        "</object></body></html>"
    )
    soup = BeautifulSoup(html, features="lxml")
    assert unsupported_plugin_count(soup) == 1


def test_page_with_no_plugin_elements_counts_zero():
    html = "<html><body><video src='clip.mp4'></video><img src='x.jpg'></body></html>"
    soup = BeautifulSoup(html, features="lxml")
    assert unsupported_plugin_count(soup) == 0


# -- registry check, through the native crawl -> evidence -> rules pipeline --


class _FakeResponse:
    def __init__(self, text: str, headers: dict[str, str]):
        self.text = text
        self.status_code = 200
        self.headers = headers


def _page(body: str) -> str:
    return body + ("Enough body text to be a real page. " * 40)


_FLASH_PAGE = f"""<html><head><title>Flash page</title></head>
<body><embed src="game.swf" type="application/x-shockwave-flash">{_page("")}</body></html>"""

_SVG_PAGE = f"""<html><head><title>SVG page</title></head>
<body><object type="image/svg+xml" data="icon.svg"></object>{_page("")}</body></html>"""


def _fetcher(mapping):
    def fetch(url):
        return mapping[url]

    return fetch


def _run_crawl(mapping):
    crawl_result = collect_urls(list(mapping), fetcher=_fetcher(mapping), sleeper=lambda _s: None)
    evidence = build_evidence(crawl_result)
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])
    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))
    run_rules(ctx)
    return ctx


def _fired(ctx) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for issue in ctx.issues:
        out.setdefault(issue.check, set()).add(issue.target_url)
    return out


def test_unsupported_plugin_fires_only_for_the_flash_page():
    mapping = {
        "https://example.com/flash": _FakeResponse(_FLASH_PAGE, {"content-type": "text/html"}),
        "https://example.com/svg": _FakeResponse(_SVG_PAGE, {"content-type": "text/html"}),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("UNSUPPORTED_PLUGIN", set()) == {"https://example.com/flash"}


def test_unsupported_plugin_skips_honestly_on_a_plain_sf_export(result):
    skipped = {s.id for s in result.skipped}
    assert "UNSUPPORTED_PLUGIN" in skipped
    assert "UNSUPPORTED_PLUGIN" not in {i.check for i in result.issues}
