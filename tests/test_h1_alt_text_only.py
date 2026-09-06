"""H1_ALT_TEXT_ONLY (#385): an H1 whose only text comes from an image's alt attribute.

A logo image sitting *beside* real heading text is normal and must not fire -- the finding
is that the H1 has no text of its own, not that it contains an image at all.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules
from seohead.tools.parser import h1_alt_only_text, parse_html


def test_h1_with_only_an_alt_bearing_image_returns_its_alt_text():
    html = "<html><body><h1><img src='logo.png' alt='Acme Pumps'></h1></body></html>"
    soup = BeautifulSoup(html, features="lxml")
    assert h1_alt_only_text(soup) == "Acme Pumps"


def test_logo_beside_real_heading_text_is_not_flagged():
    """A logo image inside a text H1 is not an image-only heading (explicit acceptance
    criterion, #385)."""
    html = (
        "<html><body><h1><img src='logo.png' alt='Logo'> Foundation Repair Guide</h1></body></html>"
    )
    soup = BeautifulSoup(html, features="lxml")
    assert h1_alt_only_text(soup) is None


def test_plain_empty_h1_with_no_image_returns_none():
    html = "<html><body><h1></h1></body></html>"
    soup = BeautifulSoup(html, features="lxml")
    assert h1_alt_only_text(soup) is None


def test_h1_with_real_text_and_no_image_returns_none():
    html = "<html><body><h1>Ordinary Heading</h1></body></html>"
    soup = BeautifulSoup(html, features="lxml")
    assert h1_alt_only_text(soup) is None


def test_image_with_empty_alt_inside_an_otherwise_empty_h1_does_not_qualify():
    """An empty alt is not text either -- it must not manufacture a false positive."""
    html = "<html><body><h1><img src='deco.png' alt=''></h1></body></html>"
    soup = BeautifulSoup(html, features="lxml")
    assert h1_alt_only_text(soup) is None


def test_headings_dict_still_reads_this_h1_as_empty():
    """h1_alt_only_text is additive: the existing headings extraction (and therefore
    H1_MISSING) must not change its own reading of the tag."""
    html = "<html><body><h1><img src='logo.png' alt='Acme Pumps'></h1></body></html>"
    parsed = parse_html(html, "https://example.com/")
    assert "h1" not in parsed["headings"]
    assert parsed["h1_alt_only_text"] == "Acme Pumps"


# -- registry check, through the native crawl -> evidence -> rules pipeline --


class _FakeResponse:
    def __init__(self, text: str, headers: dict[str, str]):
        self.text = text
        self.status_code = 200
        self.headers = headers


def _page(body: str) -> str:
    return body + ("Enough body text to be a real page. " * 40)


_ALT_ONLY_PAGE = f"""<html><head><title>Alt only</title></head>
<body><h1><img src="logo.png" alt="Acme Pumps"></h1>{_page("")}</body></html>"""

_LOGO_WITH_TEXT_PAGE = f"""<html><head><title>Logo with text</title></head>
<body><h1><img src="logo.png" alt="Logo"> Foundation Repair Guide</h1>{_page("")}</body></html>"""


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


def test_h1_alt_text_only_fires_for_the_alt_only_page_and_not_the_logo_plus_text_page():
    mapping = {
        "https://example.com/alt-only": _FakeResponse(
            _ALT_ONLY_PAGE, {"content-type": "text/html"}
        ),
        "https://example.com/logo-text": _FakeResponse(
            _LOGO_WITH_TEXT_PAGE, {"content-type": "text/html"}
        ),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("H1_ALT_TEXT_ONLY", set()) == {"https://example.com/alt-only"}
    # H1_MISSING still fires for the alt-only page (no *visible* text either way),
    # matching how a text-based reading of the page (and SF's own H1-1 column) sees it.
    assert "https://example.com/alt-only" in fired.get("H1_MISSING", set())
    assert "https://example.com/logo-text" not in fired.get("H1_MISSING", set())


def test_h1_alt_text_only_skips_honestly_on_a_plain_sf_export(result):
    skipped = {s.id for s in result.skipped}
    assert "H1_ALT_TEXT_ONLY" in skipped
    assert "H1_ALT_TEXT_ONLY" not in {i.check for i in result.issues}
