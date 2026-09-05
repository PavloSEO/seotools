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
