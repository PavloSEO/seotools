"""Offline tests for issue #19's remaining work: wiring markdown_extract and
boilerplate_report into the CLI/MCP surface, exposing duplicate_check's
only_indexable over MCP, and the content-area scoping decision for
citability_check's URL path.
"""

from __future__ import annotations

import json

from seohead import cli
from seohead.servers import handlers
from seohead.tools import parser

NAV_HTML = (
    "<html><body>"
    "<nav>Home Products Services About Contact Blog Careers Support Login Sign up now</nav>"
    '<main id="content"><h1>Caching guide</h1>'
    "<p>Caching stores a copy of a server response and serves it again without "
    "querying the database, cutting response time in half according to a 2024 "
    "benchmark.</p></main>"
    "<footer>Copyright policy terms privacy sitemap careers investors press newsletter</footer>"
    "</body></html>"
)


class _FakeResponse:
    def __init__(self, html: str, status_code: int = 200, url: str = "https://example.com/"):
        self.text = html
        self.status_code = status_code
        self.url = url


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def get(self, _url):
        return self._response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _mock_fetch(monkeypatch, html: str, status_code: int = 200):
    response = _FakeResponse(html, status_code=status_code)
    monkeypatch.setattr(parser, "http_client", lambda timeout, **kw: (_FakeClient(response), None))


# ── parser.fetch_html ─────────────────────────────────────────────────────────


def test_fetch_html_returns_raw_body_and_status(monkeypatch):
    _mock_fetch(monkeypatch, NAV_HTML, status_code=200)
    out = parser.fetch_html("https://example.com/")
    assert out["ok"] is True
    assert out["status_code"] == 200
    assert out["html"] == NAV_HTML


def test_fetch_html_tolerates_non_2xx_like_parse_url_always_has(monkeypatch):
    """A transport-successful 404 is still ``ok`` -- the body is evidence, not noise."""
    _mock_fetch(monkeypatch, "<html><body>gone</body></html>", status_code=404)
    out = parser.fetch_html("https://example.com/missing")
    assert out["ok"] is True
    assert out["status_code"] == 404


def test_fetch_html_reports_transport_failure(monkeypatch):
    def _raise(*_a, **_kw):
        raise OSError("connection refused")

    monkeypatch.setattr(parser, "http_client", _raise)
    out = parser.fetch_html("https://example.com/")
    assert out["ok"] is False
    assert "error" in out


def test_parse_url_ok_still_requires_2xx(monkeypatch):
    """parse_url's own success contract is unchanged by the fetch_html refactor."""
    _mock_fetch(monkeypatch, "<html><body>gone</body></html>", status_code=404)
    out = parser.parse_url("https://example.com/missing")
    assert out["ok"] is False
    assert out["status_code"] == 404
    assert "gone" in out["text"]  # parsing still ran; only "ok" reflects the status


# ── handlers.markdown_extract ─────────────────────────────────────────────────


def test_markdown_extract_from_html_is_pure_and_scoped():
    out = handlers.markdown_extract(html=NAV_HTML)
    assert out["ok"] is True
    assert "Caching guide" in out["content_markdown"]
    assert "Sign up now" not in out["content_markdown"]  # nav excluded
    assert "newsletter" not in out["content_markdown"]  # footer excluded
    assert "Sign up now" in out["full_markdown"]  # full rendering keeps it
    assert out["content_area_strategy"] == "auto_main"


def test_markdown_extract_from_url_fetches_then_renders(monkeypatch):
    _mock_fetch(monkeypatch, NAV_HTML)
    out = handlers.markdown_extract(url="https://example.com/")
    assert out["ok"] is True
    assert out["status_code"] == 200
    assert "Caching guide" in out["content_markdown"]
    assert "Sign up now" not in out["content_markdown"]


def test_template_content_is_absent_from_markdown_and_url_citability(monkeypatch):
    html = (
        "<html><body><template><main><h1>Unreleased draft</h1>"
        "<p>Draft evidence that must not reach a reader.</p></main></template>"
        "<main><h1>Published guide</h1><p>Visible evidence for readers.</p></main>"
        "</body></html>"
    )
    _mock_fetch(monkeypatch, html)

    markdown = handlers.markdown_extract(url="https://example.com/")
    citability = handlers.citability_check(url="https://example.com/")

    assert "Published guide" in markdown["content_markdown"]
    assert "Unreleased draft" not in markdown["content_markdown"]
    from seohead.tools import citability as cit_core

    assert citability["score"] == cit_core.score_citability(markdown["content_markdown"])["score"]


def test_markdown_extract_url_transport_failure_is_not_ok(monkeypatch):
    def _raise(*_a, **_kw):
        raise OSError("timeout")

    monkeypatch.setattr(parser, "http_client", _raise)
    out = handlers.markdown_extract(url="https://example.com/")
    assert out["ok"] is False
    assert out["url"] == "https://example.com/"


def test_markdown_extract_requires_url_or_html():
    try:
        handlers.markdown_extract()
    except ValueError:
        pass
    else:
        raise AssertionError("expected a ValueError")


def test_markdown_extract_honors_content_area_config():
    # Scoped to the body rather than the auto-detected <main>, so the disabled exclusions are
    # what decides whether the nav survives.
    scoped = handlers.markdown_extract(
        html=NAV_HTML, content_area={"root_selector": "body", "exclude_tags": []}
    )
    assert "Sign up now" in scoped["content_markdown"]  # exclusions disabled


# ── handlers.boilerplate_report ───────────────────────────────────────────────


def test_boilerplate_report_flags_the_minority_template():
    common_nav = "<nav><a href='/a'>A</a><a href='/b'>B</a></nav>"
    truncated_nav = "<nav><a href='/a'>A</a></nav>"
    pages = [
        {"url": "https://example.com/1", "html": f"<html><body>{common_nav}</body></html>"},
        {"url": "https://example.com/2", "html": f"<html><body>{common_nav}</body></html>"},
        {"url": "https://example.com/3", "html": f"<html><body>{common_nav}</body></html>"},
        {"url": "https://example.com/legacy", "html": f"<html><body>{truncated_nav}</body></html>"},
    ]
    out = handlers.boilerplate_report(pages=pages)
    assert out["ok"] is True
    assert out["count"] == 4
    minority = out["minority_groups"]
    assert len(minority) == 1
    assert minority[0]["urls"] == ["https://example.com/legacy"]


def test_boilerplate_report_requires_pages():
    try:
        handlers.boilerplate_report(pages=[])
    except ValueError:
        pass
    else:
        raise AssertionError("expected a ValueError")


# ── documented handoff: raw HTML/hash into boilerplate_report (issue #340) ────

_TEMPLATE_A = "<html><body><nav><a href='/a'>A</a><a href='/b'>B</a></nav></body></html>"
_TEMPLATE_B = "<html><body><nav><a href='/x'>X</a></nav></body></html>"


def test_boilerplate_handoff_accepts_raw_html_from_two_templates():
    """The documented handoff is raw html (or a hash of it), never Markdown --
    two differently-templated pages must land in two different groups."""
    pages = [
        {"url": "https://example.com/1", "html": _TEMPLATE_A},
        {"url": "https://example.com/2", "html": _TEMPLATE_B},
    ]
    out = handlers.boilerplate_report(pages=pages)
    assert out["ok"] is True
    # Two distinct templates must never collapse into one group.
    hashes = set()
    from seohead.tools import boilerplate_report as bp_core

    for page in pages:
        hashes.add(bp_core.boilerplate_hash(page["html"]))
    assert len(hashes) == 2
    assert out["count"] == 2
    assert len(out["minority_groups"]) == 1  # one of the two is the minority


def test_boilerplate_handoff_accepts_precomputed_hash_key():
    """A hash computed upstream with the documented ``hash`` key must group
    identically to handing over the raw html it was computed from."""
    from seohead.tools import boilerplate_report as bp_core

    hash_a = bp_core.boilerplate_hash(_TEMPLATE_A)
    hash_b = bp_core.boilerplate_hash(_TEMPLATE_B)
    assert hash_a != hash_b

    via_html = handlers.boilerplate_report(
        pages=[
            {"url": "https://example.com/1", "html": _TEMPLATE_A},
            {"url": "https://example.com/2", "html": _TEMPLATE_B},
        ]
    )
    via_hash = handlers.boilerplate_report(
        pages=[
            {"url": "https://example.com/1", "hash": hash_a},
            {"url": "https://example.com/2", "hash": hash_b},
        ]
    )
    assert via_hash["dominant_hash"] in {hash_a, hash_b}
    assert via_html["count"] == via_hash["count"]
    assert len(via_html["minority_groups"]) == len(via_hash["minority_groups"]) == 1


def test_boilerplate_handoff_rejects_the_misleading_boilerplate_hash_key():
    """``boilerplate_hash`` is not the accepted key -- only ``hash`` is. Two
    genuinely different templates supplied under the wrong key must NOT be
    told apart; they silently collapse into the same (empty-basis) group,
    which is exactly the failure issue #340 warns callers away from."""
    pages = [
        {"url": "https://example.com/1", "boilerplate_hash": "aaa"},
        {"url": "https://example.com/2", "boilerplate_hash": "bbb"},
    ]
    out = handlers.boilerplate_report(pages=pages)
    assert out["ok"] is True
    assert out["count"] == 2
    assert len(out["minority_groups"]) == 0  # both pages fell into one group


def test_full_markdown_is_not_the_boilerplate_report_handoff():
    """full_markdown has already lost the tag structure the hasher needs --
    feeding it in place of html must not reproduce the raw-html grouping."""
    from seohead.tools import boilerplate_report as bp_core
    from seohead.tools import markdown_extract as md_core

    full_markdown_a = md_core.extract_markdown(_TEMPLATE_A)["full_markdown"]
    full_markdown_b = md_core.extract_markdown(_TEMPLATE_B)["full_markdown"]

    raw_hash_a = bp_core.boilerplate_hash(_TEMPLATE_A)
    markdown_hash_a = bp_core.boilerplate_hash(full_markdown_a)
    # Hashing the Markdown instead of the html the hasher expects gives a
    # different (structure-free) digest, not the one the documented handoff
    # (raw html or a hash computed from it) would produce.
    assert markdown_hash_a != raw_hash_a
    assert full_markdown_a != full_markdown_b  # sanity: templates still differ as text


# ── handlers.citability_check: content-area rescoping (issue #19, part 2) ────


def test_citability_check_url_scores_content_area_not_whole_document(monkeypatch):
    """The nav/footer boilerplate must not reach the scorer, and structure
    (headings/paragraphs), which the flat whole-document text field cannot
    carry at all, must reach it."""
    _mock_fetch(monkeypatch, NAV_HTML)
    from seohead.tools import citability as cit_core
    from seohead.tools import markdown_extract as md_core

    out = handlers.citability_check(url="https://example.com/")
    assert out["ok"] is True

    expected_markdown = md_core.extract_markdown(NAV_HTML)["content_markdown"]
    expected = cit_core.score_citability(expected_markdown)
    assert out["score"] == expected["score"]
    # The heading survives in the scored text (structure now reachable)...
    assert out["dimensions"]["structure_quality"] == expected["dimensions"]["structure_quality"]
    assert out["dimensions"]["structure_quality"] > 0


def test_citability_check_url_keeps_structure_for_nested_article_markup(monkeypatch):
    """A heading/list nested inside a semantic wrapper (<article>) used to be
    flattened by markdown_extract before it ever reached the scorer, zeroing
    structure_quality for a page that has plenty of it (issue #230)."""
    wrapped_html = (
        "<html><body>"
        '<main id="content"><article><h1>Widget guide</h1>'
        "<p>A durable widget for everyone, tested since 2019.</p>"
        "<ul><li>First step</li><li>Second step</li></ul></article></main>"
        "</body></html>"
    )
    _mock_fetch(monkeypatch, wrapped_html)
    from seohead.tools import citability as cit_core
    from seohead.tools import markdown_extract as md_core

    out = handlers.citability_check(url="https://example.com/")
    assert out["ok"] is True

    content_markdown = md_core.extract_markdown(wrapped_html)["content_markdown"]
    assert "# Widget guide" in content_markdown
    assert "- First step" in content_markdown
    expected = cit_core.score_citability(content_markdown)
    assert out["score"] == expected["score"]
    assert out["dimensions"]["structure_quality"] > 0


def test_citability_check_text_argument_is_untouched():
    """Passing text directly bypasses content-area scoping entirely -- the
    caller supplied exactly what should be scored."""
    text = "A plain excerpt with no structure at all, forty-some words long here."
    out = handlers.citability_check(text=text)
    from seohead.tools import citability as cit_core

    assert out == cit_core.score_citability(text)


def test_citability_check_url_fetch_failure_is_not_ok(monkeypatch):
    def _raise(*_a, **_kw):
        raise OSError("dns failure")

    monkeypatch.setattr(parser, "http_client", _raise)
    out = handlers.citability_check(url="https://example.com/")
    assert out["ok"] is False


# ── CLI registration ──────────────────────────────────────────────────────────


def test_cli_markdown_extract_url_flag_maps_to_handler(monkeypatch, capsys):
    monkeypatch.setitem(
        handlers.HANDLERS, "markdown_extract", lambda **kw: {"ok": True, "echo": kw}
    )
    rc = cli.main(["markdown-extract", "--url", "https://example.com/"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["echo"]["url"] == "https://example.com/"


def test_cli_boilerplate_report_input_maps_to_handler(monkeypatch, capsys):
    monkeypatch.setitem(
        handlers.HANDLERS, "boilerplate_report", lambda **kw: {"ok": True, "echo": kw}
    )
    payload = {"pages": [{"url": "https://example.com/a", "html": "<html></html>"}]}
    rc = cli.main(["boilerplate-report", "--input", json.dumps(payload)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["echo"]["pages"] == payload["pages"]


# ── MCP registration ───────────────────────────────────────────────────────────


def test_mcp_duplicate_check_exposes_only_indexable():
    import asyncio

    from seohead.servers.mcp_server import build_server

    tools = asyncio.run(build_server().list_tools())
    by_name = {t.name: t for t in tools}
    props = by_name["seo_duplicate_check"].inputSchema["properties"]
    assert "only_indexable" in props
    assert {"seo_markdown_extract", "seo_boilerplate_report"} <= set(by_name)


# ── #340: full_markdown is not an input to boilerplate_report ─────────────────

_WITH_FOOTER = (
    "<html><body><header><nav><a href='/'>Home</a><a href='/a'>A</a></nav></header>"
    "<main><h1>Alpha</h1><p>one</p></main>"
    "<footer><p>(c) Example</p></footer></body></html>"
)
# The same template with its footer block gone -- a real, reportable difference.
_WITHOUT_FOOTER = (
    "<html><body><header><nav><a href='/'>Home</a><a href='/a'>A</a></nav></header>"
    "<main><h1>Beta</h1><p>two</p></main></body></html>"
)


def test_boilerplate_report_separates_the_two_templates_from_raw_html():
    """The positive control: given what the hasher is built for, it answers."""
    from seohead.tools.boilerplate_report import boilerplate_consistency_report

    report = boilerplate_consistency_report(
        [{"url": "/a", "html": _WITH_FOOTER}, {"url": "/b", "html": _WITHOUT_FOOTER}]
    )
    assert len(report["groups"]) == 2


def test_the_same_pages_as_markdown_collapse_into_one_false_group():
    """Why the old documentation was worse than merely wrong.

    ``boilerplate_hash`` walks the tag structure of a template. Markdown has
    already discarded it, so two pages whose templates genuinely differ hash
    identically and the report says one group -- a real difference rendered as
    no difference, which is the one outcome this toolkit exists to prevent.
    Pinned here so nobody re-documents the handoff as workable on the grounds
    that it "runs without error".
    """
    from seohead.tools.boilerplate_report import boilerplate_consistency_report
    from seohead.tools.markdown_extract import extract_markdown

    as_markdown = [
        {"url": "/a", "html": extract_markdown(_WITH_FOOTER)["full_markdown"]},
        {"url": "/b", "html": extract_markdown(_WITHOUT_FOOTER)["full_markdown"]},
    ]
    report = boilerplate_consistency_report(as_markdown)
    assert len(report["groups"]) == 1  # the false clean


def test_the_module_docstring_does_not_promise_the_markdown_handoff():
    """The claim lived in prose for long enough to be acted on twice. A reader
    reaches for the module docstring before the MCP description, so it is pinned
    too rather than left to be corrected again later."""
    from seohead.tools import markdown_extract as module

    doc = module.__doc__ or ""
    assert "full_markdown" in doc, "the docstring should still describe both renderings"
    # The exact sentence that misdirected two callers.
    assert "it is the input to ``boilerplate_report.py``'s hashing" not in doc
    assert "*not* an input to ``boilerplate_report.py``" in doc
