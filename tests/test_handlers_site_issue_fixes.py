"""Regression tests for #442, #444, #487, #489, #582.

Each covers one handler/site-audit contract violation: a documented option silently
dropped, a billed task_id lost on a recoverable failure, a failed tool double-counted
as both completed and unavailable, or page-level evidence lost in aggregation.
"""

from __future__ import annotations

from unittest import mock

import httpx

from seohead.audit.site import audit_site
from seohead.data_sources.arsenkin import ArsenkinError
from seohead.reports import checks_completed_display
from seohead.servers import handlers

# --- #442: google_keywords(seed=..., difficulty=True) must not silently drop difficulty ---


def test_google_keywords_seed_and_difficulty_scores_the_expanded_ideas():
    calls = []

    def fake_keyword_ideas(seed, **kw):
        calls.append(("keyword_ideas", seed, kw))
        return {
            "ok": True,
            "seed": seed,
            "keywords": [{"phrase": "running shoes uk", "volume": 1000, "difficulty": None}],
        }

    def fake_keyword_difficulty(keywords, **kw):
        calls.append(("keyword_difficulty", keywords, kw))
        return {"ok": True, "keywords": [{"phrase": keywords[0], "difficulty": 42}]}

    with (
        mock.patch("seohead.data_sources.dataforseo.keyword_ideas", fake_keyword_ideas),
        mock.patch("seohead.data_sources.dataforseo.keyword_difficulty", fake_keyword_difficulty),
    ):
        result = handlers.google_keywords(seed="running shoes", difficulty=True)

    assert result["ok"] is True
    assert result["difficulty"]["ok"] is True
    assert result["difficulty"]["keywords"][0]["difficulty"] == 42
    assert [c[0] for c in calls] == ["keyword_ideas", "keyword_difficulty"]
    assert calls[1][1] == ["running shoes uk"]


def test_google_keywords_plain_keywords_and_difficulty_is_unaffected():
    """Negative control: the existing keywords-list + difficulty path must not regress."""
    calls = []

    def fake_keyword_difficulty(keywords, **kw):
        calls.append(("keyword_difficulty", keywords, kw))
        return {"ok": True, "keywords": [{"phrase": k, "difficulty": 1} for k in keywords]}

    with mock.patch("seohead.data_sources.dataforseo.keyword_difficulty", fake_keyword_difficulty):
        result = handlers.google_keywords(keywords=["x"], difficulty=True)

    assert result == {"ok": True, "keywords": [{"phrase": "x", "difficulty": 1}]}
    assert calls == [("keyword_difficulty", ["x"], mock.ANY)]


def test_google_keywords_seed_difficulty_failure_is_visible_at_the_top_level():
    """The successful ideas remain usable, but requested scoring cannot read as full success."""

    def fake_keyword_ideas(seed, **_kw):
        return {"ok": True, "seed": seed, "keywords": [{"phrase": "running shoes"}]}

    def fake_keyword_difficulty(_keywords, **_kw):
        return {"ok": False, "error": "all tasks failed", "status": 40501}

    with (
        mock.patch("seohead.data_sources.dataforseo.keyword_ideas", fake_keyword_ideas),
        mock.patch("seohead.data_sources.dataforseo.keyword_difficulty", fake_keyword_difficulty),
    ):
        result = handlers.google_keywords(seed="running shoes", difficulty=True)

    assert result["ok"] is False
    assert result["keywords"] == [{"phrase": "running shoes"}]
    assert result["difficulty"] == {"ok": False, "error": "all tasks failed", "status": 40501}
    assert handlers.handler_failed(result) is True


# --- #444: keywords_exact must keep task_id/cost when wait() fails ---


class _FakeArsenkinClient:
    def __init__(self, *_a, **_k):
        pass

    def set_task(self, _tool, _data):
        return {"task_id": 555111, "cost": 12}

    def wait(self, task_id):
        raise ArsenkinError("TIMEOUT", f"task {task_id} did not finish within 900s")


class _FakeArsenkinClientBillingFails:
    def __init__(self, *_a, **_k):
        pass

    def set_task(self, _tool, _data):
        raise ArsenkinError("BAD_REQUEST", "invalid keywords")


def test_keywords_exact_keeps_task_id_and_cost_when_wait_fails():
    with mock.patch("seohead.data_sources.arsenkin.ArsenkinClient", _FakeArsenkinClient):
        result = handlers.keywords_exact(keywords=["some keyword"], wait=True)

    assert result["ok"] is False
    assert result["code"] == "TIMEOUT"
    assert result["task_id"] == 555111
    assert result["cost"] == 12


def test_keywords_exact_omits_task_id_and_cost_when_set_task_itself_fails():
    """Negative control: nothing was billed, so no task_id/cost may be fabricated."""
    with mock.patch(
        "seohead.data_sources.arsenkin.ArsenkinClient", _FakeArsenkinClientBillingFails
    ):
        result = handlers.keywords_exact(keywords=["x"], wait=True)

    assert result["ok"] is False
    assert result["code"] == "BAD_REQUEST"
    assert "task_id" not in result
    assert "cost" not in result


# --- #487: a failed site tool must not appear in both tools_run and tools_failed ---


def _ok(**_kwargs):
    return {"ok": True, "findings": []}


def _boom(**_kwargs):
    raise RuntimeError("boom")


def _robots_check(url):
    return {"ok": True, "sitemaps": [], "findings": []}


def _sitemap_crawl(url):
    return {"ok": True, "urls": [{"loc": f"https://example.com/p{i}"} for i in range(5)]}


def _parse(url):
    return {"ok": True, "results": [{}]}


def _schema_check(url):
    return {"ok": True, "types": [], "findings": []}


def _social_meta_check(url):
    return {"ok": True, "missing": [], "findings": []}


def _site_tools(**overrides):
    tools = {
        "robots_check": _robots_check,
        "sitemap_crawl": _sitemap_crawl,
        "parse": _parse,
        "schema_check": _schema_check,
        "social_meta_check": _social_meta_check,
        "domain_profile": _ok,
        "cdn_check": _ok,
        "tech_detect": _ok,
        "security_check": _ok,
        "ai_bots_check": _ok,
        "llms_txt_check": _ok,
        "regions_check": _ok,
        "render_check": _ok,
    }
    tools.update(overrides)
    return tools


def test_failed_site_tool_is_not_double_counted_as_run_and_failed():
    tools = _site_tools(domain_profile=_boom, cdn_check=_boom, tech_detect=_boom)
    result = audit_site("https://example.com", limit=1, tools=tools)
    summary = result["summary"]

    assert set(summary["tools_run"]) & {f["tool"] for f in summary["tools_failed"]} == set()
    assert len(summary["tools_run"]) + len(summary["tools_failed"]) == 10  # total SITE_TOOLS
    assert checks_completed_display(summary) == 7


def test_all_clean_site_audit_keeps_tools_run_unchanged():
    """Negative control: an all-success audit must not lose any tool from tools_run."""
    tools = _site_tools()
    result = audit_site("https://example.com", limit=1, tools=tools)
    summary = result["summary"]

    assert summary["tools_failed"] == []
    assert set(summary["tools_run"]) == {
        "robots_check",
        "sitemap_crawl",
        "domain_profile",
        "cdn_check",
        "tech_detect",
        "security_check",
        "ai_bots_check",
        "llms_txt_check",
        "regions_check",
        "render_check",
    }


# --- #489: a page tool that fails for every page must surface beyond a "notice" ---


def _parse_always_fails(url):
    raise RuntimeError("connection reset by peer")


def _parse_ok(url):
    return {"ok": True, "results": [{"title": "t", "status_code": 200}]}


def test_page_tool_failing_on_every_page_is_flagged_and_escalated():
    tools = _site_tools(parse=_parse_always_fails)
    result = audit_site("https://example.com", limit=5, tools=tools)
    summary = result["summary"]

    assert summary["page_tools_failed"] == [
        {"tool": "parse", "failed_pages": 5, "pages_checked": 5}
    ]
    assert summary["findings_by_severity"]["critical"] == 5
    assert summary["findings_by_severity"]["notice"] == 0
    assert all(f["severity"] == "critical" for f in result["findings"] if f["source"] == "page")


def test_page_tool_failing_on_zero_pages_produces_no_signal():
    """Negative control: a page tool that always succeeds changes nothing."""
    tools = _site_tools(parse=_parse_ok)
    result = audit_site("https://example.com", limit=5, tools=tools)
    summary = result["summary"]

    assert summary["page_tools_failed"] == []
    assert summary["tools_failed"] == []


# --- #582: parse's real batch envelope must surface transport failure ---


def test_parse_batch_transport_failure_is_unavailable_and_critical(monkeypatch):
    """Use the real handler envelope: parser failures do not raise from ``parse``."""

    def failed_parse_url(url, _options):
        return {"url": url, "ok": False, "error": "connection refused"}

    monkeypatch.setattr("seohead.tools.parser.parse_url", failed_parse_url)
    result = audit_site("https://example.com", limit=5, tools=_site_tools(parse=handlers.parse))

    assert result["summary"]["page_tools_failed"] == [
        {"tool": "parse", "failed_pages": 5, "pages_checked": 5}
    ]
    assert all(page["issues"] == ["parse: connection refused"] for page in result["pages"])
    assert result["summary"]["findings_by_severity"]["critical"] == 5


def test_parse_batch_empty_transport_error_is_unavailable_and_critical(monkeypatch):
    """An empty exception string is still the real ``ParseFailed`` shape."""

    class TimeoutClient:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def get(self, _url):
            raise httpx.ReadTimeout("")

    monkeypatch.setattr(
        "seohead.tools.parser.http_client", lambda *_args, **_kwargs: (TimeoutClient(), False)
    )
    result = audit_site("https://example.com", limit=1, tools=_site_tools(parse=handlers.parse))

    assert result["summary"]["page_tools_failed"] == [
        {"tool": "parse", "failed_pages": 1, "pages_checked": 1}
    ]
    assert result["pages"][0]["issues"] == ["parse: no response received"]
    assert result["summary"]["findings_by_severity"]["critical"] == 1


def test_parse_batch_completed_error_response_remains_measured(monkeypatch):
    """A fetched 404 has evidence and must not be recast as a transport failure."""

    def fetched_not_found(url, _options):
        return {
            "url": url,
            "final_url": url,
            "status_code": 404,
            "ok": False,
            "title": "Gone",
            "meta_description": "",
            "headings": {},
            "word_count": 1,
        }

    monkeypatch.setattr("seohead.tools.parser.parse_url", fetched_not_found)
    result = audit_site("https://example.com", limit=1, tools=_site_tools(parse=handlers.parse))

    assert result["summary"]["page_tools_failed"] == []
    assert result["pages"][0]["status"] == 404
    assert result["pages"][0]["issues"] == []


def test_parse_batch_success_remains_available(monkeypatch):
    """A successful real parse envelope stays out of unavailable-check reporting."""

    def fetched_page(url, _options):
        return {
            "url": url,
            "final_url": url,
            "status_code": 200,
            "ok": True,
            "title": "Available",
            "meta_description": "",
            "headings": {},
            "word_count": 1,
        }

    monkeypatch.setattr("seohead.tools.parser.parse_url", fetched_page)
    result = audit_site("https://example.com", limit=1, tools=_site_tools(parse=handlers.parse))

    assert result["summary"]["page_tools_failed"] == []
    assert result["pages"][0]["status"] == 200
