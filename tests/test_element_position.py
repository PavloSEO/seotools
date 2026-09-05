"""Element position and document skeleton (issue #123).

Eleven catalogued issues reduced to one missing fact: the parser recorded *what* an
element said and never *where in the document it was*. A browser closes <head> at the
first element that does not belong there, so a canonical or robots directive placed
after it is silently read from <body> instead — the page still looks correct in the
source. These tests cover both halves: the pure parser fact
(``seohead.tools.parser.parse_html``'s ``position`` key) and the registry checks built
on top of it, through the same native-crawl path ``test_lighthouse_checks.py`` uses for
the other fields no Screaming Frog export carries by default.
"""

from __future__ import annotations

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules
from seohead.tools.parser import invalid_head_elements, parse_html

# -- pure parser facts --------------------------------------------------------

CLEAN_HEAD = """
<html><head>
<title>Clean</title>
<link rel="canonical" href="https://example.com/clean">
</head><body>hi</body></html>
"""

# The classic real-world cause: a stray element the head content model does not
# allow closes <head> early, and the canonical after it is read from <body>.
BROKEN_HEAD = """
<html><head>
<title>Broken</title>
<script>ignore()</script>
<div>oops</div>
<link rel="canonical" href="https://example.com/broken">
</head><body>hi</body></html>
"""


def test_clean_head_reports_nothing_outside_it():
    pos = parse_html(CLEAN_HEAD, "https://example.com/clean")["position"]
    assert pos["canonical_outside_head"] is False
    assert pos["title_outside_head"] is False
    assert pos["invalid_head_elements"] == []
    assert pos["head_count"] == 1
    assert pos["body_count"] == 1
    assert pos["head_not_first"] is False


def test_canonical_after_a_stray_element_is_read_as_outside_head():
    pos = parse_html(BROKEN_HEAD, "https://example.com/broken")["position"]
    assert pos["canonical_outside_head"] is True
    assert pos["invalid_head_elements"] == ["div"]
    # Still extracted — just from the wrong place.
    assert (
        parse_html(BROKEN_HEAD, "https://example.com/broken")["canonical"]
        == "https://example.com/broken"
    )


def test_absent_element_is_neither_inside_nor_outside_head():
    html = "<html><head></head><body>no title, no canonical</body></html>"
    pos = parse_html(html, "https://example.com/x")["position"]
    assert pos["title_outside_head"] is None
    assert pos["canonical_outside_head"] is None
    assert pos["meta_description_outside_head"] is None
    assert pos["directives_outside_head"] is None
    assert pos["hreflang_outside_head"] is None


def test_directives_and_hreflang_outside_head():
    html = """
    <html><head>
      <title>T</title>
      <div>oops</div>
      <meta name="robots" content="noindex">
      <link rel="alternate" hreflang="fr" href="/fr">
    </head><body>hi</body></html>
    """
    pos = parse_html(html, "https://example.com/x")["position"]
    assert pos["directives_outside_head"] is True
    assert pos["hreflang_outside_head"] is True
    assert pos["invalid_head_elements"] == ["div"]


def test_valid_head_only_elements_never_close_head():
    """title/base/link/meta/style/script/noscript/template never force a close."""
    html = """
    <html><head>
      <base href="/">
      <title>T</title>
      <style>a{color:red}</style>
      <noscript><img src="x"></noscript>
      <link rel="canonical" href="/c">
    </head><body>hi</body></html>
    """
    pos = parse_html(html, "https://example.com/x")["position"]
    assert pos["canonical_outside_head"] is False
    assert pos["invalid_head_elements"] == []


def test_two_head_and_two_body_tags_are_both_counted():
    two_heads = (
        '<html><head><title>T1</title></head><head><meta name="description" '
        'content="d"></head><body>a</body></html>'
    )
    assert parse_html(two_heads, "https://example.com/x")["position"]["head_count"] == 2

    two_bodies = "<html><head><title>T</title></head><body>a</body><body>b</body></html>"
    assert parse_html(two_bodies, "https://example.com/x")["position"]["body_count"] == 2


def test_missing_head_or_body_is_zero_not_none():
    no_head = "<html><body>hi</body></html>"
    assert parse_html(no_head, "https://example.com/x")["position"]["head_count"] == 0

    no_body = "<html><head><title>T</title></head></html>"
    assert parse_html(no_body, "https://example.com/x")["position"]["body_count"] == 0


def test_body_before_html_style_markup_is_head_not_first():
    """Covers both catalogue rows that collapse into the same resolved shape."""
    malformed = (
        "<body><p>early</p></body><html><head><title>T</title></head><body><p>a</p></body></html>"
    )
    pos = parse_html(malformed, "https://example.com/x")["position"]
    assert pos["head_not_first"] is True


def test_head_not_first_is_false_when_there_is_no_head_to_misplace():
    assert (
        parse_html("<html><body>hi</body></html>", "https://example.com/x")["position"][
            "head_not_first"
        ]
        is False
    )


def test_invalid_head_elements_reads_the_literal_source_span():
    # Regression guard for the fact that a *resolved* tree can never show an
    # invalid element still inside <head> -- the parser has already moved it
    # to <body> by the time anything inspects the tree (see parser.py).
    assert invalid_head_elements("<head><title>T</title><p>x</p></head>") == ["p"]
    assert invalid_head_elements("<head><title>T</title></head>") == []
    assert invalid_head_elements("<body>no head tag here</body>") == []


# Issue #267: a raw opening-tag regex over the head span cannot tell markup
# from text that merely looks like markup. Each case below plants the literal
# text "<div>" somewhere a real element could never sit -- script/style CDATA,
# title RCDATA, a comment, a quoted attribute value, and template content --
# next to a positive control (a real, unquoted <div> directly in head) so the
# fix cannot pass by simply going silent on every case.
_RAW_TEXT_HEAD = """
<head>
  <script>const t = "<div class='card'>fake</div>";</script>
  <style>.card::before { content: "<div>"; }</style>
  <title>Has a literal &lt;div&gt; look-alike: <div></title>
  <!-- a comment mentioning <div> -->
  <meta name="x" content="looks like <div> but is an attribute value">
</head>
"""


def test_script_content_is_not_an_invalid_head_element():
    assert invalid_head_elements(_RAW_TEXT_HEAD) == []


def test_style_content_is_not_an_invalid_head_element():
    only_style = '<head><style>.x::before{content:"<div>"}</style></head>'
    assert invalid_head_elements(only_style) == []


def test_title_text_is_not_an_invalid_head_element():
    only_title = "<head><title>Guide to &lt;div&gt; and <div> tags</title></head>"
    assert invalid_head_elements(only_title) == []


def test_comment_text_is_not_an_invalid_head_element():
    only_comment = "<head><!-- stray <div> mentioned here --></head>"
    assert invalid_head_elements(only_comment) == []


def test_quoted_attribute_value_is_not_an_invalid_head_element():
    only_attr = '<head><meta name="d" content="a <div> in an attribute"></head>'
    assert invalid_head_elements(only_attr) == []


def test_template_content_is_not_an_invalid_head_element():
    only_template = "<head><template><div>inert fragment content</div></template></head>"
    assert invalid_head_elements(only_template) == []


def test_a_real_div_directly_in_head_still_fires():
    # Positive control paired with every negative case above: an actual
    # element in the head content model must still be caught.
    real_div = "<head><title>T</title><div>real stray element</div></head>"
    assert invalid_head_elements(real_div) == ["div"]


def test_real_div_after_all_the_look_alikes_still_fires():
    combined = _RAW_TEXT_HEAD.replace("</head>", "<div>real</div></head>")
    assert invalid_head_elements(combined) == ["div"]


# -- registry checks, through a native crawl (no Screaming Frog export carries this) --


class _FakeResponse:
    def __init__(self, text: str, headers: dict[str, str]):
        self.text = text
        self.status_code = 200
        self.headers = headers


def _page(body: str) -> str:
    return body + ("Enough body text to be a real page. " * 40)


_CLEAN_PAGE = f"""<html><head>
<title>Clean page</title>
<link rel="canonical" href="https://example.com/clean">
</head><body>{_page("")}</body></html>"""

_BROKEN_PAGE = f"""<html><head>
<title>Broken page</title>
<script>ignore()</script>
<div>oops</div>
<link rel="canonical" href="https://example.com/broken">
</head><body>{_page("")}</body></html>"""

_TWO_BODIES_PAGE = (
    f"<html><head><title>Two bodies</title></head><body>{_page('')}</body><body>extra</body></html>"
)

_NO_HEAD_PAGE = f"<html><body>{_page('')}</body></html>"


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


_RAW_TEXT_PAGE = f"""<html><head>
<title>Raw text page</title>
<script>const template = "<div class='card'>not an HTML element</div>";</script>
<style>.card::before {{ content: "<div>"; }}</style>
</head><body>{_page("")}</body></html>"""


def test_native_pipeline_does_not_flag_raw_text_look_alikes_but_still_flags_a_real_one():
    """Issue #267 acceptance: the native collect -> evidence -> rules path must
    not fire INVALID_HEAD_ELEMENT for script/style/title/comment/template text
    that only looks like markup, while a genuine stray element (the existing
    _BROKEN_PAGE fixture) still fires."""
    mapping = {
        "https://example.com/raw-text": _FakeResponse(
            _RAW_TEXT_PAGE, {"content-type": "text/html"}
        ),
        "https://example.com/broken": _FakeResponse(_BROKEN_PAGE, {"content-type": "text/html"}),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("INVALID_HEAD_ELEMENT", set()) == {"https://example.com/broken"}


def test_canonical_outside_head_fires_only_for_the_broken_fixture():
    """Acceptance criterion: the classic real-world cause is reported; a clean head is not."""
    mapping = {
        "https://example.com/clean": _FakeResponse(_CLEAN_PAGE, {"content-type": "text/html"}),
        "https://example.com/broken": _FakeResponse(_BROKEN_PAGE, {"content-type": "text/html"}),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired["CANONICAL_OUTSIDE_HEAD"] == {"https://example.com/broken"}
    assert fired["INVALID_HEAD_ELEMENT"] == {"https://example.com/broken"}
    assert fired.get("TITLE_OUTSIDE_HEAD", set()) == set()


def test_two_body_tags_are_one_finding_not_one_per_element():
    """Acceptance criterion: a page with two <body> elements is reported once."""
    mapping = {
        "https://example.com/two-bodies": _FakeResponse(
            _TWO_BODIES_PAGE, {"content-type": "text/html"}
        ),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired["BODY_MULTIPLE"] == {"https://example.com/two-bodies"}
    assert len([i for i in fired if i == "BODY_MULTIPLE"]) == 1


def test_head_missing_fires_and_body_missing_does_not_double_fire_head_multiple():
    mapping = {
        "https://example.com/no-head": _FakeResponse(_NO_HEAD_PAGE, {"content-type": "text/html"}),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired["HEAD_MISSING"] == {"https://example.com/no-head"}
    assert "HEAD_MULTIPLE" not in fired
    assert "BODY_MISSING" not in fired


def test_position_checks_skip_honestly_on_a_plain_sf_export(result):
    """``result`` (conftest.py) is a real-shaped SF export with none of these
    columns — exactly what a default Screaming Frog export looks like."""
    skipped = {s.id for s in result.skipped}
    position_checks = (
        "TITLE_OUTSIDE_HEAD",
        "DESC_OUTSIDE_HEAD",
        "CANONICAL_OUTSIDE_HEAD",
        "DIRECTIVES_OUTSIDE_HEAD",
        "HREFLANG_OUTSIDE_HEAD",
        "HEAD_MISSING",
        "HEAD_MULTIPLE",
        "BODY_MISSING",
        "BODY_MULTIPLE",
        "INVALID_HEAD_ELEMENT",
        "HEAD_NOT_FIRST",
    )
    for check_id in position_checks:
        assert check_id in skipped
    fired = {i.check for i in result.issues}
    for check_id in position_checks:
        assert check_id not in fired
