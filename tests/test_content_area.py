"""Offline tests for the configurable content area (issue #19, part 1)."""

import subprocess
import sys

from bs4 import BeautifulSoup

from seohead.tools import content_area
from seohead.tools.parser import parse_html

_HTML = """<html><body>
<nav>Home Products Services About Contact Blog Careers Support Login Sign up now</nav>
<main id="content"><h1>Widget</h1><p>A short but real product description.</p>
<a href="/related">related</a></main>
<footer>Copyright policy terms privacy sitemap careers investors press newsletter</footer>
</body></html>"""


def _soup() -> BeautifulSoup:
    return BeautifulSoup(_HTML, features="lxml")


# ── resolve_content_area ──────────────────────────────────────────────────────


def test_default_detects_main_and_says_which_container_it_used():
    root, strategy = content_area.resolve_content_area(_soup())
    assert strategy == "auto_main"
    text = content_area.extract_area_text(root)
    assert "Widget" in text and "short but real product description" in text
    assert "Sign up now" not in text  # nav
    assert "newsletter" not in text  # footer


def test_include_selector_wins_and_is_recorded():
    root, strategy = content_area.resolve_content_area(_soup(), {"include_selector": "#content"})
    assert strategy == "include_selector"
    text = content_area.extract_area_text(root)
    assert "Widget" in text
    assert "Sign up now" not in text


def test_template_candidates_never_win_visible_content_selection():
    html = """<html><body>
    <template><main id="draft">Unreleased draft</main></template>
    <main id="published">Published guide</main>
    </body></html>"""
    soup = BeautifulSoup(html, features="lxml")

    root, strategy = content_area.resolve_content_area(soup)
    assert strategy == "auto_main"
    assert content_area.extract_area_text(root) == "Published guide"

    root, strategy = content_area.resolve_content_area(soup, {"include_selector": "main"})
    assert strategy == "include_selector"
    assert content_area.extract_area_text(root) == "Published guide"

    root, strategy = content_area.resolve_content_area(soup, {"include_selector": "#draft"})
    assert strategy == "fallback_default_body"
    assert content_area.extract_area_text(root) == "Published guide"


def test_content_area_direct_import_resolves_visible_candidates():
    code = """from bs4 import BeautifulSoup
from seohead.tools.content_area import extract_area_text, resolve_content_area
soup = BeautifulSoup('<template><main>draft</main></template><main>live</main>', 'lxml')
root, strategy = resolve_content_area(soup)
assert strategy == 'auto_main'
assert extract_area_text(root) == 'live'
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr


def test_missing_selector_falls_back_and_says_so():
    root, strategy = content_area.resolve_content_area(_soup(), {"include_selector": "#nope"})
    assert strategy == "fallback_default_body"
    # Still falls back to the whole (exclusion-filtered) body, not an empty region.
    assert "Widget" in content_area.extract_area_text(root)


def test_exclude_selectors_removes_class_or_id_based_boilerplate():
    html = (
        '<html><body><div class="mega-menu">Deals Sale Clearance</div>'
        "<p>Real content here.</p></body></html>"
    )
    soup = BeautifulSoup(html, features="lxml")
    root, strategy = content_area.resolve_content_area(soup, {"exclude_selectors": [".mega-menu"]})
    assert strategy == "default_body"
    text = content_area.extract_area_text(root)
    assert "Real content here." in text
    assert "Deals" not in text


def test_exclude_tags_empty_list_disables_the_default_exclusions():
    # Detection would scope this to <main>, where there is no nav or footer to keep, so the
    # exclusion behaviour is exercised on the body itself.
    root, _strategy = content_area.resolve_content_area(
        _soup(), {"root_selector": "body", "exclude_tags": []}
    )
    text = content_area.extract_area_text(root)
    assert "Sign up now" in text  # nav kept this time
    assert "newsletter" in text  # footer kept this time


# ── parser.parse_html integration: acceptance criteria 1 and 2 ───────────────


def test_word_count_changes_with_content_area_but_link_count_does_not():
    # A content area covering the whole body (exclusions disabled) versus one
    # scoped to the article: the same page, two different regions.
    full = parse_html(
        _HTML,
        "https://example.com/page",
        {"content_area": {"root_selector": "body", "exclude_tags": []}},
    )
    scoped = parse_html(
        _HTML, "https://example.com/page", {"content_area": {"include_selector": "#content"}}
    )
    assert scoped["word_count"] < full["word_count"]
    # Restricting the content area is a statement about text, not about links.
    assert len(scoped["links"]) == len(full["links"])
    assert any(link["href"].endswith("/related") for link in full["links"])


def test_content_area_strategy_appears_per_page():
    default = parse_html(_HTML, "https://example.com/page")
    assert default["content_area_strategy"] == "auto_main"
    scoped = parse_html(
        _HTML, "https://example.com/page", {"content_area": {"root_selector": "#nope"}}
    )
    assert scoped["content_area_strategy"] == "fallback_default_body"


def test_content_area_strategy_none_when_text_disabled():
    off = parse_html(_HTML, "https://example.com/page", {"text": False})
    assert off["content_area_strategy"] is None
    assert off["word_count"] == 0


# ── automatic detection (issue #96) ──────────────────────────────────────────

# The live shape the issue measured: a skip link and a masthead that are outside <main> and
# inside neither <nav> nor <footer>, so tag-stripping alone never removed them.
_WORDPRESS_SHAPE = """<html><body>
<a class="skip-link" href="#content">Skip to content</a>
<header><div class="site-branding">header</div><p>Call us any time on 555 0100</p></header>
<main id="content"><article><h1>Post</h1><p>The body of the article itself.</p></article></main>
<footer>Copyright</footer>
</body></html>"""


def test_content_outside_main_is_not_counted_as_content():
    soup = BeautifulSoup(_WORDPRESS_SHAPE, features="lxml")
    root, strategy = content_area.resolve_content_area(soup)
    text = content_area.extract_area_text(root)
    assert strategy == "auto_main"
    assert "The body of the article itself." in text
    assert "Skip to content" not in text
    assert "header" not in text
    assert "555 0100" not in text


def test_role_main_is_used_when_there_is_no_main_element():
    html = '<html><body><header>masthead</header><div role="main"><p>Real text.</p></div></body></html>'
    root, strategy = content_area.resolve_content_area(BeautifulSoup(html, features="lxml"))
    assert strategy == "auto_role_main"
    assert "Real text." in content_area.extract_area_text(root)
    assert "masthead" not in content_area.extract_area_text(root)


def test_article_is_used_when_there_is_neither():
    html = "<html><body><header>masthead</header><article><p>Real text.</p></article></body></html>"
    root, strategy = content_area.resolve_content_area(BeautifulSoup(html, features="lxml"))
    assert strategy == "auto_article"
    assert "Real text." in content_area.extract_area_text(root)


def test_a_page_with_none_of_the_three_still_resolves_to_the_body():
    html = "<html><body><div class='wrap'><p>Real text.</p></div></body></html>"
    root, strategy = content_area.resolve_content_area(BeautifulSoup(html, features="lxml"))
    assert strategy == "default_body"
    assert "Real text." in content_area.extract_area_text(root)


def test_an_explicit_selector_still_wins_over_detection():
    root, strategy = content_area.resolve_content_area(
        BeautifulSoup(_WORDPRESS_SHAPE, features="lxml"), {"include_selector": "header"}
    )
    assert strategy == "include_selector"
    assert "header" in content_area.extract_area_text(root)


def test_a_configured_selector_that_matches_nothing_does_not_silently_auto_detect():
    """Substituting a different region for the one that was named would hide the mistake
    the strategy field exists to show."""
    root, strategy = content_area.resolve_content_area(
        BeautifulSoup(_WORDPRESS_SHAPE, features="lxml"), {"include_selector": "#nope"}
    )
    assert strategy == "fallback_default_body"
    assert "The body of the article itself." in content_area.extract_area_text(root)


def test_header_and_aside_are_excluded_on_the_fallback_path():
    html = (
        "<html><body><header>masthead</header><aside>promo block</aside>"
        "<div><p>Real text.</p></div></body></html>"
    )
    root, strategy = content_area.resolve_content_area(BeautifulSoup(html, features="lxml"))
    text = content_area.extract_area_text(root)
    assert strategy == "default_body"
    assert "Real text." in text
    assert "masthead" not in text
    assert "promo block" not in text


# ── inline SVG/MathML descendant text is not body copy (issue #140) ──────────


def test_svg_icon_titles_do_not_inflate_word_count():
    """A 20-icon sprite header, each with an accessibility <title>, must not double the count."""
    icons = "".join(f"<svg><title>search icon {i}</title></svg>" for i in range(20))
    body = " ".join(f"word{i}" for i in range(40))
    html = f"<html><body>{icons}<p>{body}</p></body></html>"
    parsed = parse_html(html, "https://example.com/")
    assert parsed["word_count"] == 40


def test_extract_area_text_excludes_svg_and_math():
    html = (
        "<html><body><svg><text>decorative label</text></svg>"
        "<math><mtext>x plus y</mtext></math><p>Real body copy.</p></body></html>"
    )
    root, _ = content_area.resolve_content_area(BeautifulSoup(html, features="lxml"))
    text = content_area.extract_area_text(root)
    assert text == "Real body copy."
