"""AJAX_CRAWLING_SCHEME_URL and AJAX_CRAWLING_SCHEME_META_FRAGMENT (#386).

Google's AJAX crawling scheme -- a ``#!`` hash-bang URL, or a page-wide
``<meta name="fragment" content="!">`` promising a rendered snapshot at an
``?_escaped_fragment_=`` companion URL -- was deprecated in 2015 and switched off in
2018. ``seohead.tools.render`` already read both shapes to pick a fetch mode; neither
was ever recorded, so the audit could not report that a site still carries them.

The evidence has to be real rather than inferred, which is what the parser tests below
pin down, and the finding is informational: a site may still serve the scheme for a
legacy client of its own, so both checks are registered at notice severity and an
ordinary page must stay silent.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.registry import CHECKS
from seohead.sf.core.rules import run_rules
from seohead.tools.parser import meta_fragment_content, parse_html, uses_ajax_crawling_scheme

# ── the URL predicate ───────────────────────────────────────────────────────


def test_both_shapes_of_the_scheme_are_recognised():
    assert uses_ajax_crawling_scheme("https://example.com/#!/products/1")
    assert uses_ajax_crawling_scheme("https://example.com/?_escaped_fragment_=/products/1")
    # The scheme's own empty-fragment form, produced by the meta-fragment opt-in.
    assert uses_ajax_crawling_scheme("https://example.com/page?_escaped_fragment_=")


def test_an_ordinary_url_and_an_ordinary_anchor_are_not_the_scheme():
    """A ``#section`` anchor is the single most common fragment on the web; reading
    it as the scheme would fire on almost every page that has a table of contents."""
    assert not uses_ajax_crawling_scheme("https://example.com/products/1")
    assert not uses_ajax_crawling_scheme("https://example.com/guide#installation")
    assert not uses_ajax_crawling_scheme("https://example.com/?q=hello#top")
    assert not uses_ajax_crawling_scheme("")


# ── the meta tag, as written ────────────────────────────────────────────────


def test_the_meta_fragment_declaration_is_kept_as_the_document_wrote_it():
    soup = BeautifulSoup(
        '<html><head><meta name="fragment" content="!"></head><body></body></html>',
        features="lxml",
    )
    assert meta_fragment_content(soup) == "!"


def test_a_page_without_the_tag_records_an_empty_declaration_not_a_guess():
    soup = BeautifulSoup("<html><head></head><body></body></html>", features="lxml")
    assert meta_fragment_content(soup) == ""


def test_a_meta_fragment_inside_a_template_is_inert_and_is_not_a_declaration():
    soup = BeautifulSoup(
        '<html><head><template><meta name="fragment" content="!"></template></head></html>',
        features="lxml",
    )
    assert meta_fragment_content(soup) == ""


def test_parse_html_returns_the_declaration_so_a_crawl_can_record_it():
    parsed = parse_html(
        '<html><head><meta name="fragment" content="!"></head><body>x</body></html>',
        "https://example.com/",
    )
    assert parsed["meta_fragment"] == "!"


# ── end to end, from a crawl's own evidence ─────────────────────────────────


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200
        self.headers = {"content-type": "text/html; charset=utf-8"}


def _page(head: str = "", body: str = "") -> str:
    filler = "Enough body text to be a real page. " * 40
    return (
        "<html><head><title>A title long enough for the length rule</title>"
        '<meta name="description" content="A description long enough for the length rule.">'
        f"{head}</head><body><h1>Heading</h1>{body}{filler}</body></html>"
    )


_ORDINARY = _page(body='<a href="/other">Other</a><a href="/guide#installation">Guide</a>')
_HASHBANG_LINKS = _page(body='<a href="/#!/products/1">One</a><a href="/#!/products/2">Two</a>')
_META_FRAGMENT = _page(head='<meta name="fragment" content="!">')


def _run_crawl(mapping):
    result = collect_urls(list(mapping), fetcher=lambda url: mapping[url], sleeper=lambda _s: None)
    evidence = build_evidence(result)
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


def test_a_page_publishing_hashbang_urls_fires_and_an_ordinary_page_stays_silent():
    mapping = {
        "https://example.com/ordinary": _FakeResponse(_ORDINARY),
        "https://example.com/spa": _FakeResponse(_HASHBANG_LINKS),
    }
    ctx = _run_crawl(mapping)
    fired = _fired(ctx)
    assert fired.get("AJAX_CRAWLING_SCHEME_URL", set()) == {"https://example.com/spa"}
    details = next(i for i in ctx.issues if i.check == "AJAX_CRAWLING_SCHEME_URL").details
    assert details["outlinks_using_scheme"] == 2
    assert details["own_url_uses_scheme"] is False


def test_a_page_whose_own_url_uses_the_scheme_fires_without_needing_a_linking_page():
    mapping = {
        "https://example.com/ordinary": _FakeResponse(_ORDINARY),
        "https://example.com/app?_escaped_fragment_=/products/1": _FakeResponse(_page()),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("AJAX_CRAWLING_SCHEME_URL", set()) == {
        "https://example.com/app?_escaped_fragment_=/products/1"
    }


def test_the_meta_fragment_opt_in_fires_only_on_the_page_that_declares_it():
    mapping = {
        "https://example.com/ordinary": _FakeResponse(_ORDINARY),
        "https://example.com/legacy": _FakeResponse(_META_FRAGMENT),
    }
    ctx = _run_crawl(mapping)
    fired = _fired(ctx)
    assert fired.get("AJAX_CRAWLING_SCHEME_META_FRAGMENT", set()) == {"https://example.com/legacy"}
    issue = next(i for i in ctx.issues if i.check == "AJAX_CRAWLING_SCHEME_META_FRAGMENT")
    assert issue.details["meta_fragment"] == "!"


def test_a_non_scheme_meta_fragment_value_stays_silent():
    ctx = _run_crawl(
        {
            "https://example.com/ordinary": _FakeResponse(_ORDINARY),
            "https://example.com/custom": _FakeResponse(
                _page(head='<meta name="fragment" content="legacy">')
            ),
        }
    )
    assert "AJAX_CRAWLING_SCHEME_META_FRAGMENT" not in _fired(ctx)


def test_both_checks_are_informational_rather_than_a_warning():
    """The issue asks for this explicitly: the scheme is deprecated, not broken, and a
    site may have kept it on purpose for a legacy client."""
    assert CHECKS["AJAX_CRAWLING_SCHEME_URL"]["severity"] == "notice"
    assert CHECKS["AJAX_CRAWLING_SCHEME_META_FRAGMENT"]["severity"] == "notice"


def test_both_checks_skip_by_name_on_a_plain_sf_export(result):
    """``result`` (conftest.py) is a real-shaped SF export, which carries neither the
    meta-fragment declaration nor an AJAX-scheme outlink count."""
    reasons = {s.id: s.reason for s in result.skipped}
    assert "native crawl only" in reasons["AJAX_CRAWLING_SCHEME_URL"]
    assert "native crawl only" in reasons["AJAX_CRAWLING_SCHEME_META_FRAGMENT"]
    fired = {i.check for i in result.issues}
    assert "AJAX_CRAWLING_SCHEME_URL" not in fired
    assert "AJAX_CRAWLING_SCHEME_META_FRAGMENT" not in fired
