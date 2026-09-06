"""Occurrence-preserving script/stylesheet declaration inventory (issue #530).

Repeated <script src> / <link rel=stylesheet href> declarations must remain
distinct occurrences, not collapse into one by resolved URL — the defect
extract_url_sources() and asset_weight._discover_resources() each have
because they dedup by URL for their own purposes.
"""

from bs4 import BeautifulSoup

from seohead.tools.parser import (
    document_base_url,
    extract_script_stylesheet_declarations,
    parse_html,
)


def _decls(html: str, base_url: str = "https://example.com/", *, cap=None):
    soup = BeautifulSoup(html, features="lxml")
    return extract_script_stylesheet_declarations(soup, base_url, cap=cap)


def test_repeated_script_declarations_stay_separate_occurrences():
    out, omitted = _decls('<script src="/app.js"></script><script src="/app.js"></script>')
    assert omitted == 0
    assert len(out) == 2
    assert (
        out[0]
        == out[1]
        == {"kind": "script", "url": "https://example.com/app.js", "raw_url": "/app.js"}
    )


def test_document_order_preserved_across_kinds():
    out, _ = _decls(
        '<script src="/first.js"></script>'
        '<link rel="stylesheet" href="/second.css">'
        '<script src="/third.js"></script>'
    )
    assert [d["url"] for d in out] == [
        "https://example.com/first.js",
        "https://example.com/second.css",
        "https://example.com/third.js",
    ]
    assert [d["kind"] for d in out] == ["script", "stylesheet", "script"]


def test_raw_url_spelling_preserved():
    out, _ = _decls('<script src="//cdn.example.com/app.js"></script>')
    assert out[0]["raw_url"] == "//cdn.example.com/app.js"
    assert out[0]["url"] == "https://cdn.example.com/app.js"


def test_stylesheet_rel_matching_is_case_insensitive_and_token_based():
    out, _ = _decls(
        '<link rel="StyleSheet" href="/a.css">'
        '<link rel="alternate stylesheet" href="/b.css">'
        '<link rel="preload" href="/c.js" as="script">'
    )
    urls = {d["url"] for d in out}
    assert urls == {"https://example.com/a.css", "https://example.com/b.css"}


def test_link_without_stylesheet_rel_is_not_a_declaration():
    out, _ = _decls('<link rel="icon" href="/favicon.ico">')
    assert out == []


def test_script_without_src_is_not_a_declaration():
    out, _ = _decls("<script>console.log('inline')</script>")
    assert out == []


def test_empty_src_is_kept_as_an_observed_declaration():
    out, _ = _decls('<script src=""></script>')
    assert out == [{"kind": "script", "url": "", "raw_url": ""}]


def test_non_http_scheme_is_recorded_not_invented_or_dropped():
    out, _ = _decls('<script src="data:text/javascript,void(0)"></script>')
    assert out == [
        {
            "kind": "script",
            "url": "data:text/javascript,void(0)",
            "raw_url": "data:text/javascript,void(0)",
        }
    ]


def test_base_href_changes_resolution():
    html = '<base href="https://cdn.example.com/assets/"><script src="app.js"></script>'
    soup = BeautifulSoup(html, features="lxml")
    base_url = document_base_url(soup, "https://example.com/")
    out, _ = extract_script_stylesheet_declarations(soup, base_url)
    assert out[0]["url"] == "https://cdn.example.com/assets/app.js"


def test_template_descendant_excluded():
    out, _ = _decls('<template><script src="/never.js"></script></template>')
    assert out == []


def test_cap_truncates_and_reports_omitted_count():
    out, omitted = _decls(
        '<script src="/a.js"></script><script src="/b.js"></script><script src="/c.js"></script>',
        cap=2,
    )
    assert len(out) == 2
    assert omitted == 1
    assert [d["url"] for d in out] == ["https://example.com/a.js", "https://example.com/b.js"]


def test_cap_zero_keeps_none_but_still_counts_total():
    out, omitted = _decls('<script src="/a.js"></script>', cap=0)
    assert out == []
    assert omitted == 1


# ── parse_html opt-in wiring ────────────────────────────────────────────────


def test_disabled_by_default_in_parse_html():
    r = parse_html('<script src="/app.js"></script>', "https://example.com/")
    assert "script_stylesheet_declarations" not in r
    assert "script_stylesheet_declarations_omitted" not in r


def test_opt_in_via_cap_option():
    r = parse_html(
        '<script src="/app.js"></script><script src="/app.js"></script>',
        "https://example.com/",
        {"max_script_stylesheet_declarations": 10},
    )
    assert len(r["script_stylesheet_declarations"]) == 2
    assert r["script_stylesheet_declarations_omitted"] == 0


def test_opt_in_cap_truncation_via_parse_html():
    r = parse_html(
        '<script src="/a.js"></script><script src="/b.js"></script>',
        "https://example.com/",
        {"max_script_stylesheet_declarations": 1},
    )
    assert len(r["script_stylesheet_declarations"]) == 1
    assert r["script_stylesheet_declarations_omitted"] == 1


def test_invalid_cap_raises():
    import pytest

    with pytest.raises(ValueError):
        parse_html(
            "<script src='/a.js'></script>",
            "https://example.com/",
            {"max_script_stylesheet_declarations": -1},
        )
