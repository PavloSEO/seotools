"""Offline tests for custom search: a query language over a crawled corpus
(issue #20, part 1). No network access."""

import pytest

from seohead.tools.custom_search import run_filter, run_search


def _doc(url, html=None, ok=True, text=None, rendered=False):
    return {"url": url, "ok": ok, "html": html, "text": text, "rendered": rendered}


def test_not_contains_reports_exactly_the_pages_lacking_the_string():
    """Acceptance criterion: a fixture corpus of 900-ish pages, some missing a
    banner; failed fetches must be excluded from the denominator, not counted
    as missing."""
    documents = (
        [
            _doc(f"https://example.com/with/{i}", html="<div>consent-banner</div>")
            for i in range(1, 861)
        ]
        + [
            _doc(f"https://example.com/without/{i}", html="<div>no banner here</div>")
            for i in range(1, 41)
        ]
        + [_doc(f"https://example.com/failed/{i}", ok=False) for i in range(1, 6)]
    )
    result = run_filter(
        documents,
        {
            "name": "consent",
            "mode": "not_contains",
            "kind": "text",
            "scope": "raw",
            "query": "consent-banner",
        },
    )
    assert result["pages_considered"] == 900  # 860 + 40, the fetched corpus
    assert result["pages_excluded_fetch_failed"] == 5
    assert result["count"] == 40
    assert set(result["matching_pages"]) == {
        f"https://example.com/without/{i}" for i in range(1, 41)
    }
    # A failed fetch must never appear as "missing" evidence.
    assert not any("failed" in u for u in result["matching_pages"])


def test_contains_reports_the_presence_set():
    documents = [
        _doc("https://example.com/a", html="<script>gtag('config')</script>"),
        _doc("https://example.com/b", html="<p>nothing here</p>"),
    ]
    result = run_filter(
        documents, {"mode": "contains", "kind": "text", "scope": "raw", "query": "gtag("}
    )
    assert result["matching_pages"] == ["https://example.com/a"]
    assert result["fraction"] == 0.5


def test_regex_kind_supports_a_pattern_query():
    documents = [
        _doc("https://example.com/a", html="phone: +1-555-000-1111"),
        _doc("https://example.com/b", html="no phone here"),
    ]
    result = run_filter(
        documents,
        {"mode": "contains", "kind": "regex", "scope": "raw", "query": r"\+1-555-\d{3}-\d{4}"},
    )
    assert result["matching_pages"] == ["https://example.com/a"]


def test_text_scope_uses_visible_text_not_markup():
    documents = [_doc("https://example.com/a", html="<script>trackingId</script><p>Hello</p>")]
    raw = run_filter(
        documents, {"mode": "contains", "kind": "text", "scope": "raw", "query": "trackingId"}
    )
    visible = run_filter(
        documents, {"mode": "contains", "kind": "text", "scope": "text", "query": "trackingId"}
    )
    assert raw["count"] == 1  # present in raw source
    assert visible["count"] == 0  # not in visible text (it's inside <script>)


def test_element_scope_targets_a_named_element():
    documents = [
        _doc("https://example.com/a", html='<div class="price">$19.99</div>'),
        _doc("https://example.com/b", html="<div>no price div</div>"),
    ]
    result = run_filter(
        documents,
        {
            "mode": "not_contains",
            "kind": "regex",
            "scope": "element",
            "selector": ".price",
            "query": r"\$\d",
        },
    )
    # /b has no .price element at all -> empty target -> counted as lacking it.
    assert result["matching_pages"] == ["https://example.com/b"]


def test_xpath_scope_targets_a_named_node():
    documents = [
        _doc("https://example.com/a", html="<html><body><h1>Special Title</h1></body></html>"),
        _doc("https://example.com/b", html="<html><body><h1>Other</h1></body></html>"),
    ]
    result = run_filter(
        documents,
        {
            "mode": "contains",
            "kind": "text",
            "scope": "xpath",
            "selector": "//h1/text()",
            "query": "Special",
        },
    )
    assert result["matching_pages"] == ["https://example.com/a"]


def test_xpath_scope_without_a_selector_is_rejected():
    with pytest.raises(ValueError, match="selector"):
        run_filter(
            [_doc("https://example.com/a", html="<h1>x</h1>")],
            {"mode": "contains", "scope": "xpath", "query": "x"},
        )


# ── malformed selectors are rejected, not silently empty (issue #232) ────────


def test_malformed_css_selector_is_rejected_not_treated_as_no_match():
    """A typo'd selector must never reach the corpus scan: an always-empty
    target from a syntax error is indistinguishable from a legitimately empty
    one, and not_contains would otherwise report every fetched page as
    lacking the term despite no filter ever having run successfully."""
    documents = [
        _doc("https://example.com/a", html="<p>banner</p>"),
        _doc("https://example.com/b", html="<p>banner</p>"),
    ]
    with pytest.raises(ValueError, match="invalid CSS selector"):
        run_filter(
            documents,
            {
                "mode": "not_contains",
                "scope": "element",
                "selector": "div[",
                "query": "banner",
            },
        )


def test_malformed_xpath_expression_is_rejected_not_treated_as_no_match():
    documents = [_doc("https://example.com/a", html="<p>banner</p>")]
    with pytest.raises(ValueError, match="invalid XPath expression"):
        run_filter(
            documents,
            {
                "mode": "not_contains",
                "scope": "xpath",
                "selector": "//h1[",
                "query": "banner",
            },
        )


def test_malformed_regex_is_rejected_not_treated_as_no_match():
    documents = [_doc("https://example.com/a", html="banner text")]
    with pytest.raises(ValueError, match="invalid regex"):
        run_filter(
            documents,
            {"mode": "not_contains", "kind": "regex", "scope": "raw", "query": "banner("},
        )


def test_valid_selector_with_zero_matches_is_still_a_real_absence_finding():
    """A syntactically valid selector that simply never matches anything on the
    corpus must still work exactly as before -- only a broken selector is
    rejected, not a selector that legitimately finds nothing."""
    documents = [
        _doc("https://example.com/a", html="<p>banner</p>"),
        _doc("https://example.com/b", html="<p>banner</p>"),
    ]
    result = run_filter(
        documents,
        {
            "mode": "not_contains",
            "scope": "element",
            "selector": ".nonexistent-class",
            "query": "banner",
        },
    )
    assert result["count"] == 2
    assert set(result["matching_pages"]) == {"https://example.com/a", "https://example.com/b"}


# ── XPath element scope keeps nested and tail text (issue #233) ─────────────


def test_xpath_element_scope_includes_nested_inline_markup():
    """//h1 over <h1><strong>Special</strong> Title</h1> must match "Special
    Title": node.text alone covers only the text before the first child and
    silently drops everything nested or trailing after it."""
    documents = [_doc("https://example.com/a", html="<h1><strong>Special</strong> Title</h1>")]
    result = run_filter(
        documents,
        {"mode": "contains", "scope": "xpath", "selector": "//h1", "query": "Special Title"},
    )
    assert result["matching_pages"] == ["https://example.com/a"]


def test_xpath_element_scope_includes_tail_text_after_a_nested_tag():
    documents = [_doc("https://example.com/a", html="<p>Hello <b>world</b> and more</p>")]
    result = run_filter(
        documents,
        {"mode": "contains", "scope": "xpath", "selector": "//p", "query": "and more"},
    )
    assert result["matching_pages"] == ["https://example.com/a"]


def test_xpath_text_node_result_is_still_the_plain_string():
    """A //h1/text() result already returns a plain string per node; the
    element-node fix above must not change that path."""
    documents = [_doc("https://example.com/a", html="<h1>Plain</h1>")]
    result = run_filter(
        documents,
        {"mode": "contains", "scope": "xpath", "selector": "//h1/text()", "query": "Plain"},
    )
    assert result["matching_pages"] == ["https://example.com/a"]


def test_representation_reports_static_or_rendered():
    static_docs = [_doc("https://example.com/a", html="<p>x</p>", rendered=False)]
    rendered_docs = [_doc("https://example.com/a", html="<p>x</p>", rendered=True)]
    mixed_docs = static_docs + rendered_docs

    assert (
        run_filter(static_docs, {"mode": "contains", "scope": "raw", "query": "x"})[
            "representation"
        ]
        == "static_markup"
    )
    assert (
        run_filter(rendered_docs, {"mode": "contains", "scope": "raw", "query": "x"})[
            "representation"
        ]
        == "rendered_dom"
    )
    assert run_filter(mixed_docs, {"mode": "contains", "scope": "raw", "query": "x"})[
        "representation"
    ] == [
        "rendered_dom",
        "static_markup",
    ]


def test_unknown_scope_is_rejected():
    with pytest.raises(ValueError, match="scope"):
        run_filter(
            [_doc("https://example.com/a", html="x")],
            {"mode": "contains", "scope": "bogus", "query": "x"},
        )


def test_run_search_applies_every_filter():
    documents = [_doc("https://example.com/a", html="foo bar")]
    out = run_search(
        documents,
        [
            {"name": "foo", "mode": "contains", "scope": "raw", "query": "foo"},
            {"name": "baz", "mode": "contains", "scope": "raw", "query": "baz"},
        ],
    )
    assert out["ok"] is True
    assert [f["name"] for f in out["filters"]] == ["foo", "baz"]
    assert out["filters"][0]["count"] == 1
    assert out["filters"][1]["count"] == 0


def test_empty_corpus_reports_zero_not_a_division_error():
    result = run_filter([], {"mode": "not_contains", "scope": "raw", "query": "x"})
    assert result["pages_considered"] == 0
    assert result["fraction"] == 0.0
    assert result["matching_pages"] == []


def test_text_scope_ignores_svg_labels():
    """Issue #544: a phrase drawn inside <svg> is not text the page states.

    ``_visible_text`` kept a shorter tag list than
    ``content_area.TEXT_EXCLUDED_TAGS`` and so left SVG labels in, which made a
    "contains" rule match on markup and, worse, silenced the ``not_contains``
    absence finding an operator asked for.
    """
    documents = [
        _doc(
            "https://example.com/a",
            html="<html><body><svg><text>consent-banner</text></svg><p>Hello</p></body></html>",
        )
    ]
    rule = {"kind": "text", "scope": "text", "query": "consent-banner"}
    present = run_filter(documents, {**rule, "mode": "contains"})
    absent = run_filter(documents, {**rule, "mode": "not_contains"})
    assert present["count"] == 0  # the phrase is an SVG label, not page copy
    assert absent["count"] == 1  # so the absence finding is still made
