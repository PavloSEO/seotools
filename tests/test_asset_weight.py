"""CSS/JS weight and delivery analysis — no network, every response injected.

Two fixture pages exercise the whole pipeline end to end (acceptance criteria
from issue #24): one page whose stylesheet is oversized, unminified, and
missing font-display must trip every relevant check with a byte-accurate
size; a page whose CSS is minified, compressed, long-cached, and already
carries font-display: swap must produce zero findings.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from seohead.tools.asset_weight import (
    analyze_page_asset_weight,
    check_cache_lifetime,
    check_compression,
    content_hash,
    find_css_imports,
    find_debug_code,
    find_duplicate_libraries,
    find_missing_font_display,
    find_render_blocking_resources,
    find_source_map_comment,
    flag_outlier_pages,
    has_document_write,
    is_render_blocking,
    looks_legacy_transpiled,
    looks_minified,
)


class FakeResponse:
    def __init__(self, *, text="", status_code=200, headers=None, url=None):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status_code
        self.headers = headers or {}
        self.url = url


# ── pure checks ──────────────────────────────────────────────────────────────


def _repeat_readable_css(rule_count: int = 6) -> str:
    block = (
        ".header {\n"
        "    display: flex;\n"
        "    align-items: center;\n"
        "    justify-content: space-between;\n"
        "    padding: 20px 40px;\n"
        "    background-color: #ffffff;\n"
        "}\n\n"
    )
    return block * rule_count


def _minify(css: str) -> str:
    return "".join(line.strip() for line in css.splitlines())


def test_hand_authored_css_is_not_minified():
    assert not looks_minified(_repeat_readable_css())


def test_minified_css_is_recognized():
    assert looks_minified(_minify(_repeat_readable_css()))


def test_tiny_files_are_classified_by_shape_not_size_alone():
    # Too small for the line-length heuristic, but a single unbroken line
    # with little whitespace still reads as minified.
    assert looks_minified(".a{color:red}")
    # A short file with normal formatting-style spacing is not minified just
    # because it is small (issue #479): the whitespace ratio here (0.2) is
    # above the minified threshold, same as it would be at any other size.
    assert not looks_minified("body { color: red; }")


def test_content_hash_ignores_whitespace_reformatting():
    a = ".x { color: red; }\n\n.y { color: blue; }"
    b = ".x{color:red;}.y{color:blue;}"
    assert content_hash(a) == content_hash(b)


def test_content_hash_differs_for_different_content():
    assert content_hash(".x{color:red}") != content_hash(".x{color:blue}")


def test_duplicate_libraries_detected_by_hash_not_filename():
    resources = [
        {
            "url": "https://example.com/vendor/jquery.min.js",
            "kind": "js",
            "ok": True,
            "text": "a  b",
        },
        {"url": "https://example.com/chunks/2.a1b2c3.js", "kind": "js", "ok": True, "text": "a b"},
    ]
    dupes = find_duplicate_libraries(resources)
    assert len(dupes) == 1
    assert dupes[0]["urls"] == [
        "https://example.com/chunks/2.a1b2c3.js",
        "https://example.com/vendor/jquery.min.js",
    ]


def test_no_duplicate_when_content_actually_differs():
    resources = [
        {"url": "https://example.com/a.js", "kind": "js", "ok": True, "text": "console.log(1)"},
        {"url": "https://example.com/b.js", "kind": "js", "ok": True, "text": "console.log(2)"},
    ]
    assert find_duplicate_libraries(resources) == []


def test_same_url_fetched_twice_is_not_a_duplicate():
    resources = [
        {"url": "https://example.com/a.js", "kind": "js", "ok": True, "text": "x"},
        {"url": "https://example.com/a.js", "kind": "js", "ok": True, "text": "x"},
    ]
    assert find_duplicate_libraries(resources) == []


def test_css_and_js_with_the_same_bytes_are_not_cross_matched():
    resources = [
        {"url": "https://example.com/a.css", "kind": "css", "ok": True, "text": "shared"},
        {"url": "https://example.com/b.js", "kind": "js", "ok": True, "text": "shared"},
    ]
    assert find_duplicate_libraries(resources) == []


def test_plain_script_in_head_is_render_blocking():
    assert is_render_blocking("script", {})


def test_deferred_and_async_scripts_are_not_render_blocking():
    assert not is_render_blocking("script", {"defer": ""})
    assert not is_render_blocking("script", {"async": ""})


def test_module_script_is_not_render_blocking():
    assert not is_render_blocking("script", {"type": "module"})


def test_data_island_script_type_is_not_render_blocking():
    assert not is_render_blocking("script", {"type": "application/ld+json"})


def test_plain_stylesheet_link_is_render_blocking():
    assert is_render_blocking("link", {})
    assert is_render_blocking("link", {"media": "screen"})


def test_print_media_stylesheet_is_not_render_blocking():
    assert not is_render_blocking("link", {"media": "print"})


def test_find_render_blocking_resources_in_head():
    html = """
    <html><head>
      <script src="/a.js"></script>
      <script src="/b.js" defer></script>
      <link rel="stylesheet" href="/blocking.css">
      <link rel="stylesheet" href="/print.css" media="print">
      <link rel="icon" href="/favicon.ico">
    </head><body></body></html>
    """
    soup = BeautifulSoup(html, features="lxml")
    found = find_render_blocking_resources(soup, "https://example.com/")
    urls = {f["url"] for f in found}
    assert urls == {"https://example.com/a.js", "https://example.com/blocking.css"}


def test_template_only_resources_are_not_render_blocking():
    """A <template> is a DocumentFragment: nothing inside it blocks a paint that
    never happens for it (issue #236 -- the same exclusion #140 gave links/images)."""
    html = """
    <html><head>
      <template>
        <script src="/never-loaded.js"></script>
        <link rel="stylesheet" href="/never-loaded.css">
      </template>
      <script src="/real.js"></script>
    </head><body></body></html>
    """
    soup = BeautifulSoup(html, features="lxml")
    found = find_render_blocking_resources(soup, "https://example.com/")
    urls = {f["url"] for f in found}
    assert urls == {"https://example.com/real.js"}


def test_font_display_swap_is_compliant():
    css = "@font-face { font-family: A; src: url(a.woff2); font-display: swap; }"
    assert find_missing_font_display(css) == []


def test_font_display_missing_is_flagged():
    css = "@font-face { font-family: A; src: url(a.woff2); }"
    findings = find_missing_font_display(css)
    assert len(findings) == 1
    assert "font-family: A" in findings[0]["excerpt"]


def test_font_display_auto_is_still_flagged():
    css = "@font-face { font-family: A; font-display: auto; }"
    assert len(find_missing_font_display(css)) == 1


def test_multiple_font_face_blocks_each_checked():
    css = "@font-face { font-family: A; font-display: swap; }@font-face { font-family: B; }"
    findings = find_missing_font_display(css)
    assert len(findings) == 1


def test_legacy_transpile_markers_detected():
    assert looks_legacy_transpiled("require('core-js/modules/es.promise')")
    assert looks_legacy_transpiled("var regeneratorRuntime = {}")
    assert not looks_legacy_transpiled("const x = () => 1;")


# ── issue #78: source maps, debug code, document.write, @import chains ──────


def test_source_map_comment_found_in_js():
    assert find_source_map_comment("var x=1;\n//# sourceMappingURL=app.js.map") == "app.js.map"


def test_source_map_comment_found_in_css_block_comment():
    css = "body{color:red}/*# sourceMappingURL=app.css.map */"
    assert find_source_map_comment(css) == "app.css.map"


def test_source_map_comment_absent_returns_none():
    assert find_source_map_comment("var x = 1;") is None


def test_source_map_inline_data_uri_is_not_reported():
    # An inline map is never fetched over the network, so it is not "exposed".
    encoded = "//# sourceMappingURL=data:application/json;base64,eyJ2ZXJzaW9uIjozfQ=="
    assert find_source_map_comment(encoded) is None


def test_source_map_last_comment_wins_when_more_than_one():
    text = "//# sourceMappingURL=old.js.map\nvar x=1;\n//# sourceMappingURL=new.js.map"
    assert find_source_map_comment(text) == "new.js.map"


def _repeat_readable_js(block_count: int = 6) -> str:
    block = (
        "function calculateTotal(items) {\n"
        "    console.log('debugging total calculation');\n"
        "    let total = 0;\n"
        "    for (const item of items) {\n"
        "        total += item.price;\n"
        "    }\n"
        "    return total;\n"
        "}\n\n"
    )
    return block * block_count


def test_debug_code_flagged_in_minified_bundle():
    minified = _minify(_repeat_readable_js())
    assert "console.log(" in find_debug_code(minified)


def test_debug_code_detects_debugger_and_alert():
    minified = _minify("function f(){debugger;alert('hi');return 1;}" * 10)
    markers = find_debug_code(minified)
    assert "debugger" in markers
    assert "alert(" in markers


def test_debug_code_in_unminified_source_produces_nothing():
    # Acceptance criterion: the same console.log call is noise, not a finding,
    # in hand-authored (unminified) source.
    assert find_debug_code(_repeat_readable_js()) == []


def test_debug_code_absent_from_clean_minified_bundle():
    minified = _minify("function f(){return 1;}" * 20)
    assert find_debug_code(minified) == []


def test_debug_code_absent_from_short_hand_authored_script():
    # Issue #479: a short, multi-line, indented script must not be
    # misclassified as minified just because it is under the size floor.
    js = '\nfunction greet(name) {\n    console.log("hello", name);\n}\n'
    assert find_debug_code(js) == []


def test_debug_code_flagged_in_short_genuinely_minified_script():
    # Negative control for #479: a genuinely minified short snippet (single
    # line, no indentation) must still be flagged.
    js = 'function greet(n){console.log("hello",n)}'
    assert find_debug_code(js) == ["console.log("]


def test_document_write_detected():
    assert has_document_write("document.write('<script src=\"x.js\"></script>')")


def test_document_write_absent():
    assert not has_document_write("document.writeln('<p>x</p>')")  # not the same call
    assert not has_document_write("console.log('no document.write here')")


def test_css_import_targets_extracted_in_all_syntaxes():
    css = '@import "a.css";@import url(b.css);@import url("c.css") screen;'
    assert find_css_imports(css) == ["a.css", "b.css", "c.css"]


def test_css_import_absent_returns_empty_list():
    assert find_css_imports("body{color:red}") == []


def test_cache_control_missing():
    result = check_cache_lifetime(None)
    assert result == {"ok": False, "max_age": None, "reason": "no Cache-Control max-age"}


def test_cache_control_short_max_age_is_flagged():
    result = check_cache_lifetime("public, max-age=60")
    assert result["ok"] is False
    assert result["max_age"] == 60


def test_cache_control_long_max_age_is_ok():
    result = check_cache_lifetime("public, max-age=31536000, immutable")
    assert result["ok"] is True


def test_cache_control_no_store_is_flagged():
    result = check_cache_lifetime("no-store")
    assert result["ok"] is False


def test_compression_gzip_is_ok():
    assert check_compression("gzip")["ok"] is True


def test_compression_brotli_is_ok():
    assert check_compression("br")["ok"] is True


def test_compression_missing_is_not_ok():
    assert check_compression(None)["ok"] is False


def test_flag_outlier_pages_needs_more_than_one_page():
    assert flag_outlier_pages({"https://example.com/a": 500_000}) == []


def test_flag_outlier_pages_ignores_near_identical_sites():
    totals = {f"https://example.com/p{i}": 200_000 for i in range(10)}
    totals["https://example.com/p10"] = 201_000
    assert flag_outlier_pages(totals) == []


def test_flag_outlier_pages_catches_a_genuine_outlier():
    totals = {f"https://example.com/p{i}": 200_000 for i in range(10)}
    totals["https://example.com/heavy"] = 900_000
    assert flag_outlier_pages(totals) == ["https://example.com/heavy"]


# ── orchestrator (fetcher injected, no socket ever opened) ───────────────────


def _fetcher(mapping):
    def fetch(url):
        value = mapping.get(url)
        if value is None:
            return FakeResponse(status_code=404)
        return value

    return fetch


def test_page_with_every_delivery_problem_is_flagged_with_byte_accurate_size():
    # Hand-authored (unminified) and, at 50 repeated rule blocks, comfortably over the threshold.
    css_body = "@font-face { font-family: Body; src: url(b.woff2); }\n" + _repeat_readable_css(50)
    assert len(css_body.encode("utf-8")) > 5_000

    site = {
        "https://example.com/": FakeResponse(
            text=(
                "<html><head>"
                '<script src="/blocking.js"></script>'
                '<link rel="stylesheet" href="/heavy.css">'
                "</head><body>hi</body></html>"
            )
        ),
        "https://example.com/blocking.js": FakeResponse(
            text="function legacy(){require('core-js/modules/es.array.map')}"
        ),
        "https://example.com/heavy.css": FakeResponse(
            text=css_body,
            headers={"cache-control": "no-store"},
        ),
    }

    result = analyze_page_asset_weight(
        "https://example.com/",
        file_size_threshold=5_000,
        fetcher=_fetcher(site),
    )

    assert result["ok"] is True
    assert result["total_bytes"] == len(css_body.encode("utf-8")) + len(
        site["https://example.com/blocking.js"].content
    )
    assert {r["url"] for r in result["render_blocking"]} == {
        "https://example.com/blocking.js",
        "https://example.com/heavy.css",
    }
    assert result["oversized"] == [
        {
            "url": "https://example.com/heavy.css",
            "bytes": len(css_body.encode("utf-8")),
            "threshold": 5_000,
        }
    ]
    assert result["unminified"] == ["https://example.com/heavy.css"]
    assert [m["source"] for m in result["missing_font_display"]] == [
        "https://example.com/heavy.css"
    ]
    assert result["legacy_js"] == ["https://example.com/blocking.js"]
    # Neither resource sends a Cache-Control/Content-Encoding at all in this fixture.
    assert {c["url"] for c in result["cache_findings"]} == {
        "https://example.com/blocking.js",
        "https://example.com/heavy.css",
    }
    assert {c["url"] for c in result["compression_findings"]} == {
        "https://example.com/blocking.js",
        "https://example.com/heavy.css",
    }
    assert len(result["findings"]) >= 6
    assert {s["check"] for s in result["skipped"]} == {"unused_css_js", "site_median_outlier"}


def test_clean_page_produces_zero_findings():
    minified_css = _minify(
        "@font-face { font-family: Body; src: url(b.woff2); font-display: swap; }"
        + _repeat_readable_css(10)
    )
    minified_js = "".join(line.strip() for line in "function f(){\n  return 1;\n}".splitlines())
    good_headers = {
        "cache-control": "public, max-age=31536000, immutable",
        "content-encoding": "br",
    }

    site = {
        "https://example.com/": FakeResponse(
            text=(
                "<html><head>"
                '<script src="/app.js" defer></script>'
                # The standard non-blocking stylesheet pattern: load hidden behind
                # media="print", then let it apply everywhere once loaded.
                '<link rel="stylesheet" href="/app.css" media="print" '
                "onload=\"this.media='all'\">"
                "</head><body>hi</body></html>"
            )
        ),
        "https://example.com/app.js": FakeResponse(text=minified_js, headers=good_headers),
        "https://example.com/app.css": FakeResponse(text=minified_css, headers=good_headers),
    }

    result = analyze_page_asset_weight("https://example.com/", fetcher=_fetcher(site))

    assert result["ok"] is True
    assert result["render_blocking"] == []
    assert result["oversized"] == []
    assert result["duplicate_libraries"] == []
    assert result["unminified"] == []
    assert result["missing_font_display"] == []
    assert result["legacy_js"] == []
    assert result["cache_findings"] == []
    assert result["compression_findings"] == []
    assert result["findings"] == []


def test_duplicate_library_bundled_twice_is_caught_end_to_end():
    lib_a = "var lib = { version: 1 };\nlib.run = function () { return 1; };\n" * 20
    lib_b = "".join(line.strip() for line in lib_a.splitlines())  # reformatted, same bytes stripped

    site = {
        "https://example.com/": FakeResponse(
            text=(
                "<html><head>"
                '<script src="/vendor/lib.min.js"></script>'
                '<script src="/chunks/9f.js"></script>'
                "</head><body></body></html>"
            )
        ),
        "https://example.com/vendor/lib.min.js": FakeResponse(text=lib_b),
        "https://example.com/chunks/9f.js": FakeResponse(text=lib_a),
    }

    result = analyze_page_asset_weight("https://example.com/", fetcher=_fetcher(site))
    assert len(result["duplicate_libraries"]) == 1
    assert set(result["duplicate_libraries"][0]["urls"]) == {
        "https://example.com/vendor/lib.min.js",
        "https://example.com/chunks/9f.js",
    }


def test_source_map_reported_only_when_the_target_actually_resolves():
    site = {
        "https://example.com/": FakeResponse(
            text=(
                "<html><head>"
                '<script src="/app.js"></script>'
                '<script src="/legacy.js"></script>'
                "</head><body></body></html>"
            )
        ),
        "https://example.com/app.js": FakeResponse(
            text="var x=1;\n//# sourceMappingURL=app.js.map"
        ),
        "https://example.com/app.js.map": FakeResponse(text='{"version":3}'),
        # legacy.js references a map that 404s: the comment exists but nothing
        # is actually exposed, so it must not be reported.
        "https://example.com/legacy.js": FakeResponse(
            text="var y=2;\n//# sourceMappingURL=legacy.js.map"
        ),
        # /legacy.js.map deliberately absent -> the fetcher returns a 404
    }

    result = analyze_page_asset_weight("https://example.com/", fetcher=_fetcher(site))

    assert result["exposed_source_maps"] == [
        {"source": "https://example.com/app.js", "map_url": "https://example.com/app.js.map"}
    ]
    assert any("source map" in f for f in result["findings"])


def test_debug_code_flagged_end_to_end_only_when_minified():
    minified_with_console = _minify("function f(){console.log('x');return 1;}" * 15)
    site = {
        "https://example.com/": FakeResponse(
            text=(
                "<html><head>"
                '<script src="/bad.js"></script>'
                '<script src="/dev.js"></script>'
                "</head><body></body></html>"
            )
        ),
        "https://example.com/bad.js": FakeResponse(text=minified_with_console),
        # Hand-authored (unminified): the same console.log call is noise here.
        "https://example.com/dev.js": FakeResponse(text=_repeat_readable_js()),
    }

    result = analyze_page_asset_weight("https://example.com/", fetcher=_fetcher(site))

    assert result["debug_code"] == [
        {"url": "https://example.com/bad.js", "markers": ["console.log("]}
    ]
    assert any("debug code" in f for f in result["findings"])


def test_document_write_flagged_end_to_end():
    site = {
        "https://example.com/": FakeResponse(
            text='<html><head><script src="/inject.js"></script></head><body></body></html>'
        ),
        "https://example.com/inject.js": FakeResponse(
            text="document.write('<script src=\"ads.js\"></script>')"
        ),
    }

    result = analyze_page_asset_weight("https://example.com/", fetcher=_fetcher(site))

    assert result["document_write"] == ["https://example.com/inject.js"]
    assert any("document.write" in f for f in result["findings"])


def test_css_import_chain_depth_follows_one_level():
    site = {
        "https://example.com/": FakeResponse(
            text='<html><head><link rel="stylesheet" href="/a.css"></head><body></body></html>'
        ),
        # a.css imports b.css (depth so far: 1)...
        "https://example.com/a.css": FakeResponse(text='@import "b.css"; .a{color:red}'),
        # ...and b.css itself imports c.css, so the chain is confirmed 2 deep.
        "https://example.com/b.css": FakeResponse(text='@import "c.css"; .b{color:blue}'),
        "https://example.com/c.css": FakeResponse(text=".c{color:green}"),
    }

    result = analyze_page_asset_weight("https://example.com/", fetcher=_fetcher(site))

    assert result["css_import_chains"] == [
        {
            "source": "https://example.com/a.css",
            "import_url": "https://example.com/b.css",
            "depth": 2,
        }
    ]
    assert any("@import chain" in f for f in result["findings"])


def test_css_import_without_further_chaining_is_depth_one():
    site = {
        "https://example.com/": FakeResponse(
            text='<html><head><link rel="stylesheet" href="/a.css"></head><body></body></html>'
        ),
        "https://example.com/a.css": FakeResponse(text='@import "b.css"; .a{color:red}'),
        "https://example.com/b.css": FakeResponse(text=".b{color:blue}"),
    }

    result = analyze_page_asset_weight("https://example.com/", fetcher=_fetcher(site))

    assert result["css_import_chains"] == [
        {
            "source": "https://example.com/a.css",
            "import_url": "https://example.com/b.css",
            "depth": 1,
        }
    ]


def test_a_broken_resource_is_reported_not_silently_dropped():
    site = {
        "https://example.com/": FakeResponse(
            text='<html><head><link rel="stylesheet" href="/missing.css"></head><body></body></html>'
        ),
        # /missing.css deliberately absent -> the fetcher returns a 404
    }
    result = analyze_page_asset_weight("https://example.com/", fetcher=_fetcher(site))
    assert result["resources"][0]["ok"] is False
    assert result["resources"][0]["status_code"] == 404
    # A failed fetch must not be silently excluded from every list at once,
    # the way an unmeasured signal must not read as "clean".
    assert result["total_bytes"] == 0


def test_page_fetch_failure_is_reported_as_an_error_not_raised():
    result = analyze_page_asset_weight("https://example.com/gone", fetcher=_fetcher({}))
    assert result == {"ok": False, "url": "https://example.com/gone", "status_code": 404}


def test_template_only_resources_are_never_fetched():
    """A stylesheet/script held only in a <template> is never requested by a browser,
    so it must not be fetched, counted, or reported here either (issue #236). The
    fetcher has no entry for the template-only URLs at all -- a fetch attempt on
    them would 404 through the mapping's default and the test would still fail,
    proving the exclusion happens before any network call, not just after."""
    site = {
        "https://example.com/": FakeResponse(
            text=(
                "<html><head>"
                "<template>"
                '<script src="/never-loaded.js"></script>'
                '<link rel="stylesheet" href="/never-loaded.css">'
                "</template>"
                '<script src="/real.js"></script>'
                "</head><body></body></html>"
            )
        ),
        "https://example.com/real.js": FakeResponse(text="function f(){return 1}"),
    }
    result = analyze_page_asset_weight("https://example.com/", fetcher=_fetcher(site))
    assert result["ok"] is True
    urls = {r["url"] for r in result["resources"]}
    assert urls == {"https://example.com/real.js"}
    assert result["render_blocking"] == [{"url": "https://example.com/real.js", "tag": "script"}]


def test_resource_fan_out_is_bounded():
    many_links = "".join(f'<link rel="stylesheet" href="/s{i}.css">' for i in range(100))
    site = {
        "https://example.com/": FakeResponse(
            text=f"<html><head>{many_links}</head><body></body></html>"
        ),
    }
    for i in range(100):
        site[f"https://example.com/s{i}.css"] = FakeResponse(text=".a{color:red}")

    result = analyze_page_asset_weight(
        "https://example.com/", fetcher=_fetcher(site), max_resources=10
    )
    assert result["resources_truncated"] is True
    assert len(result["resources"]) == 10


def test_discover_resources_dedupes_repeated_declarations_css_before_js():
    """#530: _discover_resources reuses the shared occurrence-preserving parser
    helper but keeps its own by-URL dedup and CSS-before-JS output order."""
    from seohead.tools.asset_weight import _discover_resources

    soup = BeautifulSoup(
        '<script src="/app.js"></script>'
        '<script src="/app.js"></script>'
        '<link rel="stylesheet" href="/style.css">',
        features="lxml",
    )
    out = _discover_resources(soup, "https://example.com/")
    assert out == [
        {"url": "https://example.com/style.css", "kind": "css"},
        {"url": "https://example.com/app.js", "kind": "js"},
    ]


def test_discover_resources_skips_template_and_non_stylesheet_links():
    from seohead.tools.asset_weight import _discover_resources

    soup = BeautifulSoup(
        '<template><script src="/never.js"></script></template>'
        '<link rel="icon" href="/favicon.ico">'
        '<script src="/real.js"></script>',
        features="lxml",
    )
    out = _discover_resources(soup, "https://example.com/")
    assert out == [{"url": "https://example.com/real.js", "kind": "js"}]
