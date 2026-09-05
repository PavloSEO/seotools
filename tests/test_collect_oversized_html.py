"""An oversized 2xx HTML response must be represented explicitly, not as empty metadata.

Before this test, ``PageRecord`` had no way to say "this body was never parsed" -- an
oversized page and a genuinely empty page produced the identical set of default fields,
so any consumer reading title/meta_description/h1/canonical off the record could not
tell "unknown" from "observed absent" (#243). ``body_unavailable`` closes that gap for
every path that can produce a record: a live fetch, a cache hit, and a 304 revalidation
all funnel through the same ``_apply_body``, so all three must agree.
"""

from __future__ import annotations

from seohead.crawl.cache import ResponseCache
from seohead.crawl.collect import collect_urls, fetch_one


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.text = text
        self.content = text.encode("utf-8")


COMPLIANT_HTML = (
    "<html><head>"
    "<title>Actual title that easily meets the configured minimum</title>"
    '<meta name="description" content="A complete description that easily meets the threshold.">'
    '<link rel="canonical" href="https://example.com/">'
    "</head><body><h1>Actual heading that easily meets the configured minimum</h1>"
    + ("body " * 100)
    + "</body></html>"
)


def test_oversized_html_marks_body_unavailable_and_keeps_status_and_size():
    record, parsed = fetch_one(
        "https://example.com/",
        fetcher=lambda _u: FakeResponse(COMPLIANT_HTML),
        max_response_bytes=50,
    )
    assert parsed is None
    assert record.status_code == 200
    assert record.error == "response too large to parse"
    assert record.body_unavailable == "oversized"
    # The observed facts must survive untouched -- only the parser-derived fields default.
    assert record.size_bytes == len(COMPLIANT_HTML.encode("utf-8"))
    assert record.title == ""
    assert record.meta_description == ""
    assert record.h1 == ""
    assert record.canonical == ""


def test_normal_sized_html_leaves_body_unavailable_unset():
    record, parsed = fetch_one(
        "https://example.com/",
        fetcher=lambda _u: FakeResponse(COMPLIANT_HTML),
    )
    assert parsed is not None
    assert record.body_unavailable == ""
    assert record.title != ""


def test_oversized_non_html_leaves_body_unavailable_unset():
    # A too-large non-HTML asset (e.g. a PDF) already has no parser-derived fields to
    # protect -- the flag exists to guard HTML metadata specifically, and must not fire
    # for a resource where "title" was never going to be measured anyway.
    record, parsed = fetch_one(
        "https://example.com/file.pdf",
        fetcher=lambda _u: FakeResponse("x" * 200, headers={"content-type": "application/pdf"}),
        max_response_bytes=50,
    )
    assert parsed is None
    assert record.error == "response too large to parse"
    assert record.body_unavailable == ""


def test_cache_replay_of_an_oversized_page_still_marks_body_unavailable(tmp_path):
    cache_dir = tmp_path / "cache"
    url = "https://example.com/"

    # First pass: no size limit, so the page is fetched and cached whole.
    live_cache = ResponseCache(cache_dir, mode="live")
    first = collect_urls([url], fetcher=lambda _u: FakeResponse(COMPLIANT_HTML), cache=live_cache)
    first_record = first.pages[0]
    assert first_record.cache_status == "miss"
    assert first_record.body_unavailable == ""
    assert first_record.title != ""

    # Second pass: replayed from disk under a tightened limit -- no network call could
    # possibly happen (the fetcher raises), yet the stored oversized body must still be
    # declared parse-unavailable rather than silently reporting a clean, empty page.
    def _no_network(_url):
        raise AssertionError("cache replay must not hit the network")

    replay_cache = ResponseCache(cache_dir, mode="replay")
    second = collect_urls(
        [url],
        fetcher=_no_network,
        cache=replay_cache,
        max_response_bytes=50,
    )
    second_record = second.pages[0]
    assert second_record.cache_status == "hit"
    assert second_record.status_code == 200
    assert second_record.body_unavailable == "oversized"
    assert second_record.title == ""
