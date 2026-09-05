"""Selective rendering escalation and the pre-flight health-score gate (#18).

Pure orchestration: probe and render_fetch are always fakes here, so this
whole file runs with no network and no browser.
"""

from __future__ import annotations

from dataclasses import dataclass

from seohead.crawl import settings as crawl_config
from seohead.crawl.render_escalation import (
    EscalationResult,
    apply_rendered_evidence,
    escalate,
    select_samples,
    start_page_gate,
    url_pattern,
)


@dataclass
class _Page:
    url: str
    outlinks: int = 0
    external_outlinks: int = 0


# ── url_pattern / select_samples ─────────────────────────────────────────────


def test_numeric_path_segments_collapse_to_one_pattern():
    assert url_pattern("https://example.com/product/1") == url_pattern(
        "https://example.com/product/2"
    )


def test_slug_like_segments_collapse_too():
    assert url_pattern("https://example.com/blog/how-to-fix-pumps") == url_pattern(
        "https://example.com/blog/another-long-slug-here"
    )


def test_short_static_segments_do_not_collapse():
    assert url_pattern("https://example.com/about") != url_pattern("https://example.com/contact")


def test_query_and_fragment_are_ignored_in_the_pattern_key():
    assert url_pattern("https://example.com/x?a=1#top") == url_pattern("https://example.com/x")


def test_distinct_root_level_static_pages_do_not_collapse():
    """#149: a bare `/<slug>` at the root has no shared parent segment, so a long
    descriptive slug there is a hand-written page name, not a template instance --
    unlike `/blog/<slug>`, which does share one (see test_slug_like_segments_collapse_too).
    """
    urls = [
        "https://example.com/contact-us",
        "https://example.com/case-studies",
        "https://example.com/testimonials",
        "https://example.com/documentation",
        "https://example.com/pricing-plans",
    ]
    assert len({url_pattern(u) for u in urls}) == len(urls)


def test_root_level_numeric_and_date_segments_still_collapse():
    """Unlike a descriptive slug, a number, UUID or date is structurally an
    identifier wherever it sits -- no shared parent segment needed."""
    assert url_pattern("https://example.com/42") == url_pattern("https://example.com/43")
    assert url_pattern("https://example.com/2024-01-15") == url_pattern(
        "https://example.com/2024-06-30"
    )
    assert url_pattern("https://example.com/3fa85f64-5717-4562-b3fc-2c963f66afa6") == url_pattern(
        "https://example.com/7c9e6679-7425-40de-944b-e07fc1f90ae7"
    )


def test_a_js_only_page_is_still_probed_when_it_no_longer_shares_a_pattern():
    """The end-to-end case #149 reported: five root-level static pages plus one
    genuinely JS-only page (documentation), sample_per_pattern=2. Before the fix,
    all five shared one pattern key and only two of them (never `documentation`)
    were ever probed, so the JS-only page's representation stayed "static" with no
    error and no flag -- a silent false clean.
    """
    pages = [
        _Page("https://example.com/contact-us"),
        _Page("https://example.com/case-studies"),
        _Page("https://example.com/testimonials"),
        _Page("https://example.com/documentation"),
        _Page("https://example.com/pricing-plans"),
    ]
    probed_urls = []

    def probe(u):
        probed_urls.append(u)
        return {"ok": True, "needs_escalation": "documentation" in u}

    def render_fetch(u):
        return {"ok": True, "html": "<html><body>full</body></html>", "final_url": u}

    result = escalate(
        pages,
        _config(sample_per_pattern=2),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
    )
    assert "https://example.com/documentation" in probed_urls
    assert result.representations["https://example.com/documentation"] == "rendered"


def test_select_samples_caps_each_pattern_at_n():
    urls = [f"https://example.com/product/{i}" for i in range(10)]
    samples = select_samples(urls, sample_per_pattern=2)
    assert len(samples) == 1
    assert len(next(iter(samples.values()))) == 2


def test_select_samples_treats_zero_as_at_least_one():
    samples = select_samples(["https://example.com/"], sample_per_pattern=0)
    assert len(next(iter(samples.values()))) == 1


# ── start_page_gate ──────────────────────────────────────────────────────────


def test_zero_internal_links_requires_rendering():
    gate = start_page_gate("https://example.com/", 0, "<html><body>hi</body></html>")
    assert gate.requires_rendering is True
    assert "zero internal links" in gate.reason


def test_a_normal_start_page_does_not_require_rendering():
    gate = start_page_gate("https://example.com/", 5, "<html><body>hi</body></html>")
    assert gate.requires_rendering is False
    assert gate.reason == ""


def test_an_empty_spa_shell_requires_rendering_even_with_no_outlinks_check():
    html = '<html><body><div id="root"></div></body></html>'
    gate = start_page_gate("https://example.com/", 3, html)
    assert gate.requires_rendering is True
    assert "empty SPA shell" in gate.reason


def test_gate_works_with_no_html_at_all():
    """A resumed run that never re-fetched the start page still gets the outlink check."""
    gate = start_page_gate("https://example.com/", 0, "")
    assert gate.requires_rendering is True


# ── escalate() ───────────────────────────────────────────────────────────────


def _config(mode="js", sample_per_pattern=1, max_render_urls=100, max_render_seconds=0):
    resolved = crawl_config.load(
        overrides={
            "rendering.mode": mode,
            "rendering.escalation.sample_per_pattern": sample_per_pattern,
            "rendering.escalation.max_render_urls": max_render_urls,
            "rendering.escalation.max_render_seconds": max_render_seconds,
        }
    )
    return resolved["rendering"]


def test_a_pattern_that_probes_clean_is_never_rendered():
    pages = [_Page(f"https://example.com/blog/{i}") for i in range(3)]

    def probe(_url):
        return {"ok": True, "needs_escalation": False}

    def render_fetch(_url):
        raise AssertionError("must not render a pattern that did not need it")

    result = escalate(
        pages,
        _config(),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
    )
    assert result.patterns_escalated == []
    assert result.render_requests == 0
    assert all(rep == "static" for rep in result.representations.values())


def test_an_escalated_pattern_renders_every_page_in_it_not_just_the_sample():
    pages = [_Page(f"https://example.com/blog/{i}") for i in range(5)]
    rendered_calls = []

    def probe(_url):
        return {"ok": True, "needs_escalation": True}

    def render_fetch(u):
        rendered_calls.append(u)
        return {"ok": True, "html": "<html><body>full</body></html>", "final_url": u}

    result = escalate(
        pages,
        _config(sample_per_pattern=1),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
    )
    assert result.patterns_sampled == 1
    assert result.patterns_escalated
    # Selective: only 1 probe request for the whole pattern (proves sampling).
    assert result.probe_requests == 1
    # But every page sharing the escalated pattern gets rendered.
    assert result.render_requests == 5
    assert set(rendered_calls) == {p.url for p in pages}
    assert all(rep == "rendered" for rep in result.representations.values())


def test_the_render_budget_is_a_separate_ceiling_from_the_sample():
    pages = [_Page(f"https://example.com/blog/{i}") for i in range(10)]

    def probe(_url):
        return {"ok": True, "needs_escalation": True}

    def render_fetch(u):
        return {"ok": True, "html": "<html></html>", "final_url": u}

    result = escalate(
        pages,
        _config(max_render_urls=3),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
    )
    assert result.render_requests == 3
    assert result.render_budget_exhausted is True


def test_max_render_seconds_stops_rendering_once_the_first_render_crosses_the_deadline():
    """#198: max_render_seconds was accepted and validated but never read by escalate(), so a
    slow site ran every probe and every render regardless of the wall-clock budget the operator
    configured. A fake clock makes this deterministic: it only advances when render_fetch is
    called, so the first render is always allowed (the deadline has not passed yet when it
    starts) and the second is refused once that first render's simulated duration crosses it."""
    pages = [_Page(f"https://example.com/blog/{i}") for i in range(3)]

    clock_seconds = [0.0]

    def clock() -> float:
        return clock_seconds[0]

    def probe(_url):
        return {"ok": True, "needs_escalation": True}

    def render_fetch(u):
        clock_seconds[0] += 10.0  # simulates a render that alone blows the budget
        return {"ok": True, "html": "<html></html>", "final_url": u}

    result = escalate(
        pages,
        _config(max_render_urls=3, max_render_seconds=1),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
        clock=clock,
    )
    assert result.render_requests == 1
    assert result.time_budget_exhausted is True
    # render_budget_exhausted is also true here (not every escalated page got rendered), but
    # max_render_seconds -- not max_render_urls (3, never reached) -- is what cut this short;
    # time_budget_exhausted is the field that says so.
    assert result.patterns_partially_rendered == ["https://example.com/blog/*"]


def test_max_render_seconds_zero_means_unlimited():
    pages = [_Page(f"https://example.com/blog/{i}") for i in range(3)]

    clock_seconds = [0.0]

    def clock() -> float:
        return clock_seconds[0]

    def probe(_url):
        return {"ok": True, "needs_escalation": True}

    def render_fetch(u):
        clock_seconds[0] += 1000.0
        return {"ok": True, "html": "<html></html>", "final_url": u}

    result = escalate(
        pages,
        _config(max_render_urls=3, max_render_seconds=0),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
        clock=clock,
    )
    assert result.render_requests == 3
    assert result.time_budget_exhausted is False


def test_the_render_budget_is_spent_breadth_first_across_escalated_patterns():
    """#147: two probe-positive patterns of 5 URLs each and a budget of 5. Spending
    the budget sequentially (in patterns_escalated's sorted order) rendered the
    first pattern's 5 pages and left the second at zero -- both patterns still
    appeared, indistinguishably, in patterns_escalated. Breadth-first spending
    means the second pattern is not shut out just because it sorts second, and
    render_counts/patterns_partially_rendered make the shortfall visible either way.
    """
    blog = [_Page(f"https://example.com/blog/{i}") for i in range(5)]
    docs = [_Page(f"https://example.com/docs/{i}") for i in range(5)]

    def probe(_url):
        return {"ok": True, "needs_escalation": True}

    def render_fetch(u):
        return {"ok": True, "html": "<html></html>", "final_url": u}

    result = escalate(
        blog + docs,
        _config(max_render_urls=5),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
    )
    assert result.patterns_escalated == [
        "https://example.com/blog/*",
        "https://example.com/docs/*",
    ]
    assert result.render_requests == 5
    assert result.render_budget_exhausted is True
    # Neither escalated pattern is silently starved -- both got at least one
    # render, and the counts are exact rather than merely non-zero.
    assert result.render_counts == {
        "https://example.com/blog/*": 3,
        "https://example.com/docs/*": 2,
    }
    # Both are audibly incomplete: 3 of 5 and 2 of 5, not "escalated" in a way
    # indistinguishable from a pattern that got all 5.
    assert result.patterns_partially_rendered == [
        "https://example.com/blog/*",
        "https://example.com/docs/*",
    ]


def test_a_pattern_fully_rendered_within_budget_is_not_flagged_partial():
    pages = [_Page(f"https://example.com/blog/{i}") for i in range(3)]

    def probe(_url):
        return {"ok": True, "needs_escalation": True}

    def render_fetch(u):
        return {"ok": True, "html": "<html></html>", "final_url": u}

    result = escalate(
        pages,
        _config(max_render_urls=3),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
    )
    assert result.render_counts == {"https://example.com/blog/*": 3}
    assert result.patterns_partially_rendered == []
    assert result.render_budget_exhausted is False


def test_only_patterns_that_probe_positive_are_escalated_others_stay_static():
    blog = [_Page(f"https://example.com/blog/{i}") for i in range(3)]
    docs = [_Page(f"https://example.com/docs/{i}") for i in range(3)]

    def probe(u):
        return {"ok": True, "needs_escalation": "/blog/" in u}

    def render_fetch(u):
        return {"ok": True, "html": "<html></html>", "final_url": u}

    result = escalate(
        blog + docs,
        _config(),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
    )
    assert all(result.representations[p.url] == "rendered" for p in blog)
    assert all(result.representations[p.url] == "static" for p in docs)


def test_empty_shell_probes_are_collected_regardless_of_escalation_outcome():
    pages = [_Page("https://example.com/")]

    def probe(_url):
        return {"ok": True, "needs_escalation": False, "empty_shell": "root"}

    result = escalate(
        pages,
        _config(),
        probe=probe,
        render_fetch=lambda u: {"ok": False},
        representation_label="rendered",
    )
    assert result.empty_shell_urls == ["https://example.com/"]


def test_a_failed_probe_is_not_counted_as_a_positive_signal():
    pages = [_Page("https://example.com/x")]

    def probe(_url):
        return {"ok": False}

    result = escalate(
        pages,
        _config(),
        probe=probe,
        render_fetch=lambda u: {"ok": False},
        representation_label="rendered",
    )
    assert result.patterns_escalated == []


def test_a_failed_render_fetch_leaves_the_page_static():
    pages = [_Page("https://example.com/x")]

    def probe(_url):
        return {"ok": True, "needs_escalation": True}

    def render_fetch(_url):
        return {"ok": False, "error": "timeout"}

    result = escalate(
        pages,
        _config(),
        probe=probe,
        render_fetch=render_fetch,
        representation_label="rendered",
    )
    assert result.representations["https://example.com/x"] == "static"
    assert result.rendered == {}


# ── apply_rendered_evidence ──────────────────────────────────────────────────


class _Edge:
    def __init__(self, source, destination):
        self.source = source
        self.destination = destination


def test_rendered_outlinks_are_the_union_with_raw_not_a_replacement():
    from seohead.crawl.collect import PageRecord

    record = PageRecord(url="https://example.com/", outlinks=1, external_outlinks=0)
    raw_links = [_Edge("https://example.com/", "https://example.com/only-in-raw")]
    rendered_html = (
        '<html><body><a href="/only-in-raw">a</a><a href="/only-after-js">b</a></body></html>'
    )
    result = EscalationResult()
    result.representations["https://example.com/"] = "rendered"
    result.rendered["https://example.com/"] = {
        "ok": True,
        "html": rendered_html,
        "final_url": "https://example.com/",
    }

    apply_rendered_evidence([record], raw_links, result)

    assert record.representation == "rendered"
    # Both the raw-only and the rendered-only link survive the merge.
    assert record.outlinks == 2


def test_a_page_never_rendered_is_left_untouched():
    from seohead.crawl.collect import PageRecord

    record = PageRecord(url="https://example.com/", title="Original")
    apply_rendered_evidence([record], [], EscalationResult())
    assert record.title == "Original"
    assert record.representation == "static"


# ── #245: a rendered-only href must also become an all_inlinks edge ─────────


def test_a_rendered_only_link_becomes_an_edge_in_raw_links():
    """apply_rendered_evidence already folds a rendered-only href into
    PageRecord.outlinks -- the union tested above -- but until #245 it never
    told raw_links, the sole source build_evidence reads for the all_inlinks
    frame every graph check runs against. A page whose outlinks count grew
    from a rendered fetch must produce a matching new edge, or the graph the
    audit measures stays smaller than the page counts it reports claim."""
    from seohead.crawl.collect import PageRecord
    from seohead.crawl.spider import LinkEdge

    source = PageRecord(url="https://example.com/app/1", content_type="text/html")
    target = PageRecord(url="https://example.com/products/1", content_type="text/html")
    raw_links: list[LinkEdge] = []
    result = EscalationResult()
    result.representations[source.url] = "rendered"
    result.rendered[source.url] = {
        "ok": True,
        "final_url": source.url,
        "html": (
            "<html><head><title>App</title></head><body>"
            '<a href="/products/1">Rendered product link</a>'
            "</body></html>"
        ),
    }

    apply_rendered_evidence([source, target], raw_links, result)

    assert source.outlinks == 1
    assert len(raw_links) == 1
    edge = raw_links[0]
    assert edge.source == source.url
    assert edge.destination == target.url
    assert edge.anchor == "Rendered product link"
    assert edge.nofollow is False


def test_a_rendered_link_already_seen_in_raw_html_is_not_duplicated():
    """The raw crawl already recorded this exact edge -- apply_rendered_evidence
    must not add a second one just because the rendered DOM repeats it, or a
    check counting inlinks would see the rendered pass as new evidence of a
    link that was never lost in the first place."""
    from seohead.crawl.collect import PageRecord
    from seohead.crawl.spider import LinkEdge

    source = PageRecord(url="https://example.com/app/1", content_type="text/html")
    target = PageRecord(url="https://example.com/products/1", content_type="text/html")
    raw_links = [LinkEdge(source.url, target.url, "Product link", False, "content")]
    result = EscalationResult()
    result.representations[source.url] = "rendered"
    result.rendered[source.url] = {
        "ok": True,
        "final_url": source.url,
        "html": (
            "<html><head><title>App</title></head><body>"
            '<a href="/products/1">Product link</a>'
            "</body></html>"
        ),
    }

    apply_rendered_evidence([source, target], raw_links, result)

    assert source.outlinks == 1
    assert len(raw_links) == 1


# ── #143: an empty-shell render must never overwrite a healthy raw record ───


def test_an_empty_shell_render_leaves_a_healthy_raw_record_untouched():
    """render_fetch can return ok:True for a page that crashed client-side after
    load: no exception, no navigation error, just a blank mount point. That must
    not be indistinguishable from a real, fuller render.
    """
    from seohead.crawl.collect import PageRecord

    record = PageRecord(
        url="https://example.com/product/1",
        title="Wireless Mouse - Acme Store",
        meta_description="Buy the Acme wireless mouse, free shipping.",
        h1="Wireless Mouse",
        canonical="https://example.com/product/1",
        word_count=420,
        representation="static",
    )
    result = EscalationResult()
    result.representations[record.url] = "rendered"
    result.rendered[record.url] = {
        "ok": True,
        "html": '<html><body><div id="root"></div></body></html>',
        "final_url": record.url,
    }

    apply_rendered_evidence([record], [], result)

    assert record.title == "Wireless Mouse - Acme Store"
    assert record.meta_description == "Buy the Acme wireless mouse, free shipping."
    assert record.h1 == "Wireless Mouse"
    assert record.canonical == "https://example.com/product/1"
    assert record.word_count == 420
    assert record.representation == "static"
    assert result.degenerate_render_urls == [record.url]


def test_a_render_that_is_thinner_but_still_real_is_applied_and_visible():
    """The floor guard rejects a blank shell, not a page that is genuinely
    thinner once JavaScript runs (hydration removing content a non-rendering
    crawler cannot see is a real finding, not a failed render) -- that finding
    must reach the report as the rendered numbers, not be silently kept as the
    raw ones.
    """
    from seohead.crawl.collect import PageRecord

    record = PageRecord(
        url="https://example.com/product/1",
        title="Wireless Mouse - Acme Store",
        h1="Wireless Mouse",
        canonical="https://example.com/product/1",
        word_count=200,
        representation="static",
    )
    words = " ".join(f"word{i}" for i in range(60))
    rendered_html = (
        f"<html><head><title>Acme Store</title></head><body><p>{words}</p></body></html>"
    )
    result = EscalationResult()
    result.representations[record.url] = "rendered"
    result.rendered[record.url] = {
        "ok": True,
        "html": rendered_html,
        "final_url": record.url,
    }

    apply_rendered_evidence([record], [], result)

    assert result.degenerate_render_urls == []
    assert record.representation == "rendered"
    assert record.title == "Acme Store"
    # The reduced word count is the real, rendered figure -- not swallowed.
    assert record.word_count == 60


# ── #139: every body-derived field is recomputed from the rendered body ─────


def test_render_escalation_recomputes_size_bytes_text_ratio_and_jsonld():
    from seohead.crawl.collect import PageRecord

    # An empty SPA shell, exactly as a static-only fetch would have measured it.
    record = PageRecord(
        url="https://example.com/article/1",
        size_bytes=109,
        text_ratio=0.0,
        word_count=0,
        jsonld_blocks_found=0,
        jsonld_blocks_parsed=0,
        representation="static",
    )
    words = " ".join(f"word{i}" for i in range(200))
    rendered_html = (
        "<html><head><title>Real Article</title>"
        '<script type="application/ld+json">'
        '{"@context": "https://schema.org", "@type": "Article", "headline": "Real Article"}'
        "</script></head>"
        f"<body><h1>Real Article</h1><p>{words}</p></body></html>"
    )
    result = EscalationResult()
    result.representations[record.url] = "rendered"
    result.rendered[record.url] = {
        "ok": True,
        "html": rendered_html,
        "final_url": record.url,
    }

    apply_rendered_evidence([record], [], result)

    assert record.representation == "rendered"
    assert record.word_count >= 200
    # None of these four stayed at the static shell's pre-render values.
    assert record.size_bytes == len(rendered_html.encode("utf-8"))
    assert record.text_ratio is not None and record.text_ratio > 0.0
    assert record.jsonld_blocks_found == 1
    assert record.jsonld_blocks_parsed == 1


def test_rendered_html_clears_the_static_body_unavailable_marker():
    """A rendered DOM supplies the body fields an oversized static fetch lacked.

    The static ``error`` remains transport evidence, but ``body_unavailable``
    describes the fields now on the record and must therefore clear after a
    successful rendered parse.
    """
    from seohead.crawl.collect import PageRecord

    record = PageRecord(
        url="https://example.com/article/1",
        content_type="text/html",
        error="response too large to parse",
        body_unavailable="oversized",
    )
    result = EscalationResult()
    result.representations[record.url] = "rendered"
    result.rendered[record.url] = {
        "ok": True,
        "html": "<html><head><title>Rendered article</title></head><body><h1>Article</h1></body></html>",
        "final_url": record.url,
    }

    apply_rendered_evidence([record], [], result)

    assert record.representation == "rendered"
    assert record.title == "Rendered article"
    assert record.body_unavailable == ""
    assert record.error == "response too large to parse"


def test_the_crawl_passes_its_own_user_agent_to_the_rendered_fetch(monkeypatch, tmp_path):
    """The fix in render_document is only worth anything if the crawl reaches it.

    Pinning the identity inside render_document and then not passing the crawl's own
    would be the shape this repository keeps finding (#128, #154, #165): a correct
    module nothing is wired to. This asserts the wiring, not the renderer.
    """
    from seohead.crawl.spider import SpiderResult
    from seohead.servers import handlers
    from seohead.tools import render as render_tool

    seen: dict = {}

    def fake_render_document(target, rendering_config, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "url": target, "final_url": target, "html": "<html></html>"}

    monkeypatch.setattr(render_tool, "render_document", fake_render_document)
    monkeypatch.setattr(
        render_tool,
        "render_check",
        lambda *a, **k: {"ok": True, "js_dependent": True, "empty_shell": ""},
    )

    settings = crawl_config.load(
        overrides={"rendering.mode": "js", "http.user_agent": "AcmeAudit/2.0"}
    )
    result = SpiderResult()
    result.pages = [_Page("https://example.com/")]

    handlers._run_render_escalation(result, settings["rendering"], settings)

    assert seen.get("user_agent") == "AcmeAudit/2.0"
