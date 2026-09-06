"""META_REFRESH_REDIRECT could be answered from a Screaming Frog export and never
from a native crawl.

The check reads ``rec["meta_refresh"]``, which normalize.INTERNAL_FIELD_MAP maps
only to SF's *Meta Refresh 1* column. A native crawl's PageRecord carried no such
field, so on that path the check produced nothing -- not a named skip, not a
finding, nothing at all. The declaration was in the parsed document the whole
time: extract_url_sources already reached into the tag to resolve the URL for
link discovery, and threw the declaration itself away.
"""

from __future__ import annotations

from seohead.crawl.collect import collect_urls as _collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules
from seohead.tools.parser import parse_html


def collect_urls(urls, **kw):
    kw.setdefault("sleeper", lambda _seconds: None)
    return _collect_urls(urls, **kw)


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}


def _page(head_extra: str = "") -> str:
    return (
        "<html><head><title>A title long enough for the length rule to pass</title>"
        '<meta name="description" content="A description long enough for the length rule here.">'
        f"{head_extra}</head><body><h1>Heading</h1><p>Some body copy.</p></body></html>"
    )


def _audit(pages: dict[str, str]):
    result = collect_urls(list(pages), fetcher=lambda url: FakeResponse(pages[url]))
    evidence = build_evidence(result)
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])
    ctx = AuditContext(exports, load_config(None))
    run_rules(ctx)
    return ctx


# ── the declaration survives parsing ─────────────────────────────────────────


def test_the_content_attribute_is_kept_as_written():
    """SF's Meta Refresh 1 column carries the raw content attribute, so this does
    too -- the audit has to reach the same verdict whichever source produced it."""
    parsed = parse_html(
        _page('<meta http-equiv="refresh" content="0; url=/target">'), "https://example.com/old"
    )
    assert parsed["meta_refresh"] == "0; url=/target"


def test_a_template_refresh_is_not_a_declaration():
    parsed = parse_html(
        "<html><head></head><body><template>"
        '<meta http-equiv="refresh" content="0; url=/never">'
        "</template></body></html>",
        "https://example.com/",
    )
    assert parsed["meta_refresh"] == ""


def test_a_page_declaring_none_reports_an_empty_string():
    assert parse_html(_page(), "https://example.com/")["meta_refresh"] == ""


# ── and reaches the check that was written for it ────────────────────────────


def test_the_finding_now_fires_on_a_native_crawl():
    ctx = _audit(
        {"https://example.com/old": _page('<meta http-equiv="refresh" content="0; url=/new">')}
    )
    fired = [i for i in ctx.issues if i.check == "META_REFRESH_REDIRECT"]
    assert [i.target_url for i in fired] == ["https://example.com/old"]
    assert fired[0].details["meta_refresh"] == "0; url=/new"


def test_a_page_without_a_refresh_is_silent():
    ctx = _audit({"https://example.com/ok": _page()})
    assert not [i for i in ctx.issues if i.check == "META_REFRESH_REDIRECT"]


def test_a_timed_reload_of_the_same_page_is_not_a_redirect():
    """content="5" with no target reloads this page. Reporting it as a redirect
    would be a wrong finding, and its fix text -- replace it with a 301 -- is
    advice that would break the page."""
    ctx = _audit({"https://example.com/live": _page('<meta http-equiv="refresh" content="5">')})
    assert not [i for i in ctx.issues if i.check == "META_REFRESH_REDIRECT"]


def test_the_evidence_frame_uses_screaming_frogs_own_column_name():
    """Named for SF's column so normalize.INTERNAL_FIELD_MAP needs no change: one
    fact, one mapping, read the same way from either source."""
    result = collect_urls(
        ["https://example.com/old"],
        fetcher=lambda _u: FakeResponse(_page('<meta http-equiv="refresh" content="0; url=/new">')),
        sleeper=lambda _s: None,
    )
    frame = build_evidence(result)["frames"]["internal_all"]
    assert "Meta Refresh 1" in frame.columns
    assert frame["Meta Refresh 1"].tolist() == ["0; url=/new"]
