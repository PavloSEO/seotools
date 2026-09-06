"""Pure direct script/stylesheet declaration inventory tests."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from seohead.tools.asset_weight import _discover_resources
from seohead.tools.parser import extract_resource_declarations, parse_html


def _parsed(html: str, cap: int) -> dict:
    return parse_html(
        html,
        "https://example.test/path/page",
        {"resource_declarations": True, "max_resource_declarations": cap},
    )


def test_declarations_preserve_document_order_duplicates_and_raw_attribute_spelling():
    parsed = _parsed(
        '<script src=" /app.js "></script>'
        '<link rel="preload StyleSheet alternate" href="styles/site.css">'
        '<script src=" /app.js "></script>',
        10,
    )
    assert parsed["resource_declarations"] == [
        {
            "kind": "script",
            "url": "https://example.test/app.js",
            "raw_url": " /app.js ",
        },
        {
            "kind": "stylesheet",
            "url": "https://example.test/path/styles/site.css",
            "raw_url": "styles/site.css",
        },
        {
            "kind": "script",
            "url": "https://example.test/app.js",
            "raw_url": " /app.js ",
        },
    ]
    assert parsed["resource_declarations_omitted"] == 0


def test_declarations_follow_base_skip_templates_and_ignore_non_http_or_empty_values():
    parsed = _parsed(
        '<base href="https://cdn.example.test/assets/">'
        '<template><script src="hidden.js"></script></template>'
        '<script src="module.js"></script><script src="data:text/javascript,x"></script>'
        '<script src="ftp://example.test/file.js"></script><script src=""></script>'
        '<link rel="notstylesheet" href="wrong.css">'
        '<link rel="StyleSheet" href="theme.css">',
        10,
    )
    assert parsed["resource_declarations"] == [
        {
            "kind": "script",
            "url": "https://cdn.example.test/assets/module.js",
            "raw_url": "module.js",
        },
        {
            "kind": "stylesheet",
            "url": "https://cdn.example.test/assets/theme.css",
            "raw_url": "theme.css",
        },
    ]


def test_declaration_cap_reports_every_omitted_valid_occurrence():
    parsed = _parsed(
        '<script src="one.js"></script><link rel="stylesheet" href="two.css">'
        '<script src="three.js"></script>',
        2,
    )
    assert [item["raw_url"] for item in parsed["resource_declarations"]] == ["one.js", "two.css"]
    assert parsed["resource_declarations_omitted"] == 1


def test_declaration_opt_in_requires_a_finite_nonnegative_cap_and_is_off_by_default():
    assert "resource_declarations" not in parse_html(
        '<script src="app.js"></script>', "https://x.test/"
    )
    with pytest.raises(ValueError, match="max_resource_declarations"):
        parse_html(
            '<script src="app.js"></script>', "https://x.test/", {"resource_declarations": True}
        )
    with pytest.raises(ValueError, match="max_resource_declarations"):
        parse_html(
            '<script src="app.js"></script>',
            "https://x.test/",
            {"resource_declarations": True, "max_resource_declarations": -1},
        )


def test_asset_weight_reuses_declarations_but_keeps_css_before_js_deduplication():
    soup = BeautifulSoup(
        '<script src="/app.js"></script><link rel="stylesheet" href="/theme.css">'
        '<script src="/theme.css"></script><link rel="stylesheet" href="/theme.css">',
        "lxml",
    )
    assert _discover_resources(soup, "https://example.test/") == [
        {"url": "https://example.test/theme.css", "kind": "css"},
        {"url": "https://example.test/app.js", "kind": "js"},
    ]


def test_direct_helper_keeps_its_own_bounded_omission_count():
    soup = BeautifulSoup('<script src="one.js"></script><script src="two.js"></script>', "lxml")
    declarations, omitted = extract_resource_declarations(
        soup, "https://example.test/", max_declarations=0
    )
    assert declarations == [] and omitted == 2
