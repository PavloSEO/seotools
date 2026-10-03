"""Offline contract for crawl-time URL suffix and response media filters."""

from __future__ import annotations

import json

import pytest

from seohead.crawl.collect import fetch_one
from seohead.crawl.settings import ConfigError, fingerprint, load
from seohead.crawl.spider import Scope, crawl_site
from tests.test_crawl_spider import FakeResponse, _fetcher, page

START = "https://example.test/"


def _crawl(mapping, *, scope=None, **kwargs):
    return crawl_site(
        START,
        fetcher=_fetcher(mapping),
        scope=scope,
        min_delay=0,
        sleeper=lambda _seconds: None,
        robots_policy="ignore",
        **kwargs,
    )


def _urls(result):
    return {record.url for record in result.pages}


def test_extension_exclusion_uses_path_suffix_and_keeps_the_link_observation():
    target = "https://example.test/files/Report.PDF?download=1"
    directory = "https://example.test/folder.with.dot/no-extension/"
    mapping = {
        START: page("/files/Report.PDF?download=1#preview", "/folder.with.dot/no-extension/"),
        directory: page(title="folder"),
    }

    result = _crawl(mapping, scope={"exclude_extensions": [".pdf"]})

    assert target not in _urls(result)
    assert directory in _urls(result)
    assert result.excluded["excluded_by_extension"] == 1
    assert [edge.destination for edge in result.links] == [
        "https://example.test/files/Report.PDF?download=1#preview",
        "https://example.test/folder.with.dot/no-extension/",
    ]


def test_extension_allowlist_excludes_extensionless_routes_but_not_the_start_seed():
    html = "https://example.test/index.HTML?lang=en"
    extensionless = "https://example.test/about/"
    mapping = {
        START: page("/index.HTML?lang=en", "/about/"),
        html: page(title="html"),
        extensionless: page(title="about"),
    }

    result = _crawl(mapping, scope={"include_extensions": [".HTML"]})

    assert START in _urls(result)  # The declared start URL keeps its existing seed exemption.
    assert html in _urls(result)
    assert extensionless not in _urls(result)
    assert result.excluded["not_included_by_extension"] == 1


def test_existing_url_scope_precedes_suffix_filters_and_suffix_exclusions_win():
    scope = Scope.from_config(
        {
            "include_patterns": [r"/blog/"],
            "include_extensions": ["pdf"],
            "exclude_extensions": [".PDF"],
        }
    )

    assert scope.rejection("https://example.test/files/a.pdf", "example.test") == (
        "not_included_by_pattern"
    )
    assert scope.rejection("https://example.test/blog/a.PDF?download=1", "example.test") == (
        "excluded_by_extension"
    )
    assert (
        Scope.from_config({"include_extensions": ["pdf"]}).rejection(
            "https://example.test/.well-known", "example.test"
        )
        == "not_included_by_extension"
    )


def test_response_media_rules_are_independent_case_insensitive_and_parameter_aware():
    scope = Scope.from_config(
        {
            "include_media_types": ["text/*", "application/pdf"],
            "exclude_media_types": ["text/xml"],
        }
    )

    assert scope.response_media_rejection("Text/HTML; Charset=UTF-8") == ""
    assert scope.response_media_rejection("application/PDF; version=1.7") == ""
    assert scope.response_media_rejection("text/xml; charset=utf-8") == "excluded_by_media_type"
    assert scope.response_media_rejection("image/png") == "not_included_by_media_type"
    assert scope.response_media_rejection("") == "media_type_unavailable"
    assert scope.response_media_rejection("not a media type") == "media_type_unavailable"


def test_exclusion_only_media_filter_does_not_reject_missing_content_type():
    scope = Scope.from_config({"exclude_media_types": ["application/pdf"]})
    assert scope.response_media_rejection("") == ""
    assert scope.response_media_rejection("text/html; charset=utf-8") == ""
    assert scope.response_media_rejection("application/pdf") == "excluded_by_media_type"


def test_redirect_destination_extension_filter_records_hop_without_requesting_target():
    destination = "https://example.test/archive.PDF?download=1"
    calls = []
    mapping = {
        START: page("/go.html"),
        "https://example.test/go.html": FakeResponse(
            "", status_code=302, headers={"location": "/archive.PDF?download=1"}
        ),
    }

    def fetcher(url):
        calls.append(url)
        return _fetcher(mapping)(url)

    result = crawl_site(
        START,
        fetcher=fetcher,
        scope={"exclude_extensions": ["pdf"]},
        min_delay=0,
        sleeper=lambda _seconds: None,
        robots_policy="ignore",
    )

    redirect = next(record for record in result.pages if record.url.endswith("/go.html"))
    assert redirect.status_code == 302
    assert redirect.redirect_url == destination
    assert destination not in calls
    assert result.excluded["excluded_by_extension"] == 1
    assert any(edge.destination == "https://example.test/go.html" for edge in result.links)


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"scope.include_extensions": [".HTML", ".pdf"]}, [".HTML", ".pdf"]),
        ({"scope.include_media_types": ["text/html", "image/*"]}, ["text/html", "image/*"]),
    ],
)
def test_file_filter_lists_load_from_dotted_overrides(override, expected):
    resolved = load(overrides=override)
    path, _value = next(iter(override.items()))
    category, setting = path.split(".", 1)
    assert resolved[category][setting] == expected


@pytest.mark.parametrize(
    "override,message",
    [
        ({"scope.include_extensions": "pdf"}, "scope.include_extensions must be a list"),
        ({"scope.exclude_extensions": [1]}, "scope.exclude_extensions entries must be strings"),
        ({"scope.include_extensions": ["..pdf"]}, "scope.include_extensions entry"),
        ({"scope.exclude_extensions": ["tar.gz"]}, "scope.exclude_extensions entry"),
        (
            {"scope.include_extensions": [".PDF", "pdf"]},
            "duplicate suffix 'pdf'",
        ),
        ({"scope.include_media_types": "text/html"}, "scope.include_media_types must be a list"),
        (
            {"scope.exclude_media_types": ["text/html; charset=utf-8"]},
            "scope.exclude_media_types entry",
        ),
        ({"scope.include_media_types": ["*/json"]}, "scope.include_media_types entry"),
        ({"scope.include_media_types": ["text/"]}, "scope.include_media_types entry"),
        (
            {"scope.exclude_media_types": ["TEXT/HTML", "text/html"]},
            "duplicate media type 'text/html'",
        ),
    ],
)
def test_file_filter_validation_names_invalid_paths(override, message):
    with pytest.raises(ConfigError, match=message):
        load(overrides=override)


def test_media_filtered_html_is_not_parsed_but_page_and_link_evidence_remain(tmp_path):
    target = "https://example.test/download.html"
    mapping = {
        START: page("/download.html"),
        # The URL suffix passes, but the response header is authoritative.
        target: FakeResponse(
            "<html><head><title>Must not parse</title></head><body><h1>Must not parse</h1></body></html>",
            headers={"content-type": "application/pdf; version=1.7"},
        ),
    }
    page_log = tmp_path / "pages.jsonl"
    link_log = tmp_path / "links.jsonl"
    decisions = tmp_path / "decisions.jsonl"

    result = _crawl(
        mapping,
        scope={"include_extensions": ["html"], "include_media_types": ["text/html"]},
        out_path=str(page_log),
        links_path=str(link_log),
        decisions_path=str(decisions),
    )

    filtered = next(record for record in result.pages if record.url == target)
    assert filtered.status_code == 200
    assert filtered.content_type == "application/pdf; version=1.7"
    assert filtered.title == ""
    assert filtered.h1 == ""
    assert filtered.body_unavailable == "not_included_by_media_type"
    assert result.excluded["not_included_by_media_type"] == 1
    assert any(edge.destination == target for edge in result.links)
    saved = [json.loads(line) for line in decisions.read_text().splitlines()]
    assert any(
        entry["url"] == target and entry["reason"] == "not_included_by_media_type"
        for entry in saved
    )
    assert any(json.loads(line)["url"] == target for line in page_log.read_text().splitlines())
    assert link_log.exists()


def test_fetch_one_media_filter_records_unavailable_body_reason():
    url = "https://example.test/file.html"
    scope = Scope.from_config({"exclude_media_types": ["application/pdf"]})
    record, parsed = fetch_one(
        url,
        fetcher=lambda _url: FakeResponse(
            "<html><title>unparsed</title></html>",
            headers={"content-type": "application/pdf"},
        ),
        response_filter=scope.response_media_rejection,
    )

    assert parsed is None
    assert record.body_unavailable == "excluded_by_media_type"
    assert record.title == ""


@pytest.mark.parametrize("headers", [{}, {"content-type": "not a media type"}])
def test_include_media_filter_records_missing_or_malformed_type(monkeypatch, headers):
    url = "https://example.test/unknown"
    scope = Scope.from_config({"include_media_types": ["text/html"]})
    response = FakeResponse("<html><title>unmeasured</title></html>")
    response.headers = headers

    record, parsed = fetch_one(
        url,
        fetcher=lambda _url: response,
        response_filter=scope.response_media_rejection,
    )

    assert parsed is None
    assert record.body_unavailable == "media_type_unavailable"


def test_media_filter_is_applied_to_a_cached_response_before_parsing(tmp_path):
    from seohead.crawl.cache import ResponseCache

    url = "https://example.test/cached.html"
    cache = ResponseCache(tmp_path)
    html = FakeResponse("<html><head><title>cached</title></head><body>x</body></html>")
    html.headers["cache-control"] = "max-age=3600"
    first, parsed = fetch_one(url, fetcher=lambda _url: html, cache=cache)
    assert parsed is not None
    assert first.cache_status == "miss"

    scope = Scope.from_config({"exclude_media_types": ["text/html"]})
    cached, cached_parsed = fetch_one(
        url,
        fetcher=lambda _url: pytest.fail("a cache hit must not refetch"),
        cache=cache,
        response_filter=scope.response_media_rejection,
    )

    assert cached_parsed is None
    assert cached.cache_status == "hit"
    assert cached.body_unavailable == "excluded_by_media_type"
    assert cached.title == ""


def test_extension_decisions_and_link_evidence_survive_legacy_resume(tmp_path):
    excluded = "https://example.test/private.pdf"
    queued = "https://example.test/next"
    resolved = load(overrides={"scope.exclude_extensions": ["pdf"]})
    config_fingerprint = fingerprint(resolved)
    state_path = tmp_path / "crawl_state.json"
    pages_path = tmp_path / "pages.jsonl"
    links_path = tmp_path / "links.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    first_calls = []

    def first_fetcher(url):
        first_calls.append(url)
        if url == START:
            return page("/private.pdf", "/next")
        if url == queued:
            raise KeyboardInterrupt
        raise AssertionError(f"unexpected request: {url}")

    partial = crawl_site(
        START,
        fetcher=first_fetcher,
        scope=resolved["scope"],
        min_delay=0,
        max_urls=2,
        sleeper=lambda _seconds: None,
        robots_policy="ignore",
        out_path=str(pages_path),
        links_path=str(links_path),
        decisions_path=str(decisions_path),
        state_path=str(state_path),
        config_fingerprint=config_fingerprint,
    )
    assert partial.finish_reason == "interrupted"
    assert excluded not in first_calls

    resumed_calls = []

    def resumed_fetcher(url):
        resumed_calls.append(url)
        if url == queued:
            return page(title="resumed")
        raise AssertionError(f"excluded or already-fetched URL was requested: {url}")

    resumed = crawl_site(
        START,
        fetcher=resumed_fetcher,
        scope=resolved["scope"],
        min_delay=0,
        max_urls=2,
        sleeper=lambda _seconds: None,
        robots_policy="ignore",
        out_path=str(pages_path),
        links_path=str(links_path),
        decisions_path=str(decisions_path),
        state_path=str(state_path),
        config_fingerprint=config_fingerprint,
    )

    assert resumed.resumed is True
    assert {record.url for record in resumed.pages} == {START, queued}
    assert resumed.excluded["excluded_by_extension"] == 1
    assert resumed_calls == [queued]
    assert any(edge.destination == excluded for edge in resumed.links)
    decisions = [json.loads(line) for line in decisions_path.read_text().splitlines()]
    assert (
        sum(
            entry["url"] == excluded and entry["reason"] == "excluded_by_extension"
            for entry in decisions
        )
        == 1
    )


def test_native_capture_media_filter_stops_before_reading_stream_body(monkeypatch):
    from seohead.crawl import collect

    url = "https://example.test/large.pdf"
    scope = Scope.from_config({"exclude_media_types": ["application/pdf"]})

    class _Request:
        def __init__(self):
            self.method = "GET"
            self.headers = {"user-agent": "SEOHEAD-Tools"}

    class _Stream:
        def __init__(self, target):
            self.status_code = 200
            self.headers = {"content-type": "application/pdf"}
            self.http_version = "HTTP/1.1"
            self.url = target
            self.request = _Request()
            self.history = ()
            self.iterated = False

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def iter_raw(self, **_kwargs):
            self.iterated = True
            yield b"oversized PDF body"

    stream = _Stream(url)

    class _Client:
        def __init__(self, response):
            self.headers = {}
            self.response = response

        def stream(self, *_args, **_kwargs):
            return self.response

    monkeypatch.setattr(collect, "validate_url", lambda _url: None)
    monkeypatch.setattr(collect, "pinned_target", lambda target: (target, {}, {}))
    events = []
    record, parsed = fetch_one(
        url,
        client=_Client(stream),
        response_filter=scope.response_media_rejection,
        capture_observer=events.append,
        capture_max_bytes=1,
    )

    assert parsed is None
    assert record.status_code == 200
    assert record.content_type == "application/pdf"
    assert record.body_unavailable == "excluded_by_media_type"
    assert stream.iterated is False
    assert len(events) == 1
    assert events[0].entity_bytes is None
    assert events[0].body_state == "omitted"
    assert events[0].body_reason == "unsupported_media"
    assert events[0].status_code == events[0].effective_status_code == 200
    assert events[0].response_headers == (("content-type", "application/pdf"),)
