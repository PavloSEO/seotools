"""Offline transport contracts for direct script and stylesheet resources."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest

from seohead.crawl import collect, resource_fetch
from seohead.crawl.resource_fetch import ResourceStop, fetch_resource
from seohead.crawl.settings import load
from seohead.storage.corpus import store_response
from seohead.storage.retention import NO_BODY_RETENTION


@pytest.fixture(autouse=True)
def _safe_transport(monkeypatch):
    monkeypatch.setattr(resource_fetch, "validate_url", lambda url: url)
    monkeypatch.setattr(collect, "validate_url", lambda url: url)
    monkeypatch.setattr(collect, "pinned_target", lambda url: (url, {}, {}))


def _settings(**overrides):
    values = {"speed.min_delay_seconds": 0, "http.retry_on_timeout": 1}
    values.update(overrides)
    return load(overrides=values)


def _response(request, status: int, *, headers=None, body: bytes = b""):
    return httpx.Response(
        status,
        headers=headers,
        stream=httpx.ByteStream(body),
        request=request,
    )


class _InjectedResponse:
    def __init__(self, status_code: int, body: bytes, headers: dict[str, str]):
        self.status_code = status_code
        self.content = body
        self.text = body.decode("utf-8", errors="replace")
        self.headers = headers


def _run(handler, url="https://example.test/a", **kwargs):
    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        return fetch_resource(
            url,
            kwargs.pop("kind", "script"),
            settings=kwargs.pop("settings", _settings()),
            client=client,
            throttle=None,
            origin_url="https://example.test/",
            robots_allowed=kwargs.pop("robots_allowed", lambda _url: True),
            **kwargs,
        )


def _policy():
    return {
        **NO_BODY_RETENTION,
        "body_mode": "captured_entity_bytes",
        "max_body_bytes": 1024,
        "max_body_store_bytes": 1024,
    }


def test_same_origin_redirect_is_one_capture_with_the_original_response_identity():
    seen = []
    preflights = []

    def handler(request):
        seen.append((str(request.url), request.headers["accept"]))
        if request.url.path == "/a":
            return _response(request, 302, headers={"location": "/b"})
        return _response(
            request,
            200,
            headers={"content-type": "application/javascript"},
            body=b"window.ok=1",
        )

    result = _run(handler, remaining_requests=3, before_request=lambda: preflights.append("run"))

    assert result.capture_state == "measured"
    assert result.requests_used == 2
    assert len(result.captures) == 1
    event = result.captures[0]
    assert event.requested_url.endswith("/a")
    assert event.status_code == 302
    assert event.effective_status_code == 200
    assert event.entity_bytes == b"window.ok=1"
    assert event.redirect_history[0]["next_url"].endswith("/b")
    assert all("application/javascript" in accept for _url, accept in seen)
    assert preflights == ["run", "run"]


def test_redirect_to_other_origin_is_blocked_without_a_second_request():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return _response(request, 302, headers={"location": "https://outside.test/x"})

    result = _run(handler, remaining_requests=3)

    assert result.capture_state == "excluded_scope"
    assert result.requests_used == 1
    assert len(calls) == 1
    assert result.captures[0].entity_bytes is None
    assert result.captures[0].redirect_history[-1]["blocked"] is True
    assert result.captures[0].effective_url == "https://example.test/a"


def test_blocked_later_redirect_names_the_last_observed_url_as_effective():
    def handler(request):
        if request.url.path == "/a":
            return _response(request, 302, headers={"location": "/b"})
        return _response(request, 302, headers={"location": "https://outside.test/app.js"})

    result = _run(handler, remaining_requests=3)

    assert result.capture_state == "excluded_scope"
    assert result.requests_used == 2
    assert result.captures[0].effective_url == "https://example.test/b"
    assert result.captures[0].redirect_history[-1] == {
        "request_url": "https://example.test/b",
        "status_code": 302,
        "location_raw": "https://outside.test/app.js",
        "next_url": None,
        "blocked": True,
    }


def test_same_origin_redirect_to_a_private_target_is_blocked_before_dispatch(monkeypatch):
    monkeypatch.setattr(
        resource_fetch,
        "validate_url",
        lambda url: (
            (_ for _ in ()).throw(ValueError("private network target blocked"))
            if url.endswith("/private")
            else url
        ),
    )
    result = _run(
        lambda request: _response(request, 302, headers={"location": "/private"}),
        remaining_requests=3,
    )

    assert result.capture_state == "excluded_scope"
    assert result.requests_used == 1
    assert "private network target blocked" in result.reason


def test_robots_blocks_before_transport():
    result = _run(
        lambda _request: pytest.fail("robots-rejected resources must not fetch"),
        robots_allowed=lambda _url: False,
        remaining_requests=3,
    )

    assert result.capture_state == "excluded_robots"
    assert result.requests_used == 0
    assert result.captures == ()


def test_external_candidate_is_rejected_before_its_network_guard(monkeypatch):
    seen = []
    monkeypatch.setattr(
        resource_fetch,
        "validate_url",
        lambda url: seen.append(url) or (_ for _ in ()).throw(AssertionError("must not validate")),
    )

    result = _run(
        lambda _request: pytest.fail("external candidate must not dispatch"),
        url="https://outside.test/app.js",
        remaining_requests=1,
    )

    assert result.capture_state == "excluded_scope"
    assert seen == []


def test_injected_fetcher_uses_fetch_one_without_dns_or_a_second_transport(monkeypatch):
    monkeypatch.setattr(
        resource_fetch,
        "validate_url",
        lambda _url: pytest.fail("injected fetchers own their transport guard"),
    )
    calls = []

    def fetcher(url):
        calls.append(url)
        return _InjectedResponse(
            200,
            b"window.ok=1",
            {"content-type": "application/javascript"},
        )

    result = fetch_resource(
        "https://example.test/app.js",
        "script",
        settings=_settings(),
        client=None,
        throttle=None,
        origin_url="https://example.test/",
        robots_allowed=lambda _url: True,
        fetcher=fetcher,
        remaining_requests=1,
    )

    assert result.capture_state == "measured"
    assert result.requests_used == 1
    assert calls == ["https://example.test/app.js"]
    assert result.captures[0].entity_bytes == b"window.ok=1"


def test_timeout_retry_is_a_separate_capture_and_counts_two_requests():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("slow")
        return _response(
            request,
            200,
            headers={"content-type": "application/javascript"},
            body=b"ok",
        )

    result = _run(handler, remaining_requests=2)

    assert result.capture_state == "measured"
    assert result.requests_used == 2
    assert len(result.captures) == 2
    assert result.captures[0].body_reason == "fetch_failed"
    assert result.captures[-1].entity_bytes == b"ok"


def test_budget_stops_before_a_timeout_retry_dispatches():
    result = _run(
        lambda _request: (_ for _ in ()).throw(TimeoutError("slow")), remaining_requests=1
    )

    assert result.capture_state == "resource_budget_exhausted"
    assert result.requests_used == 1
    assert len(result.captures) == 1


def test_budget_after_a_redirect_keeps_the_observed_prefix_without_following():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return _response(request, 302, headers={"location": "/b"})

    result = _run(handler, remaining_requests=1)

    assert result.capture_state == "resource_budget_exhausted"
    assert result.requests_used == 1
    assert calls == ["https://example.test/a"]
    assert result.captures[0].body_reason == "resource_budget_exhausted"
    assert result.captures[0].redirect_history[-1]["next_url"].endswith("/b")


def test_failure_after_redirect_keeps_original_observation_and_final_failure_fields():
    def handler(request):
        if request.url.path == "/a":
            return _response(request, 302, headers={"location": "/b"}, body=b"redirect body")
        return _response(
            request,
            500,
            headers={"content-type": "application/javascript"},
            body=b"broken",
        )

    result = _run(handler, remaining_requests=2)

    assert result.capture_state == "fetch_failed"
    assert len(result.captures) == 1
    event = result.captures[0]
    assert event.requested_url.endswith("/a")
    assert event.status_code == 302
    assert dict(event.response_headers)["location"] == "/b"
    assert event.effective_status_code == 500
    assert event.entity_bytes == b"broken"
    assert event.redirect_history[0]["next_url"].endswith("/b")


def test_redirect_cookie_marks_merged_capture_credentialed_and_omits_its_body():
    cookie_seen = []

    def handler(request):
        if request.url.path == "/a":
            return _response(
                request,
                302,
                headers={"location": "/b", "set-cookie": "session=secret; Path=/"},
            )
        cookie_seen.append(request.headers.get("cookie", ""))
        return _response(
            request,
            200,
            headers={"content-type": "application/javascript"},
            body=b"private body",
        )

    result = _run(handler, remaining_requests=2)
    event = result.captures[-1]
    con = sqlite3.connect(":memory:")
    con.executescript(Path("seohead/storage/scan_v1.sql").read_text())
    try:
        response_id, _document_id = store_response(con, event, purpose="script", policy=_policy())
        stored = con.execute(
            "SELECT body_state,body_reason,body_sha256,credentials_used,response_time "
            "FROM responses WHERE response_id=?",
            (response_id,),
        ).fetchone()
    finally:
        con.close()

    assert cookie_seen == ["session=secret"]
    assert event.credentials_used is True
    assert event.response_time is not None
    assert stored[:4] == ("omitted", "credentialed", None, 1)


def test_redirect_aggregate_sums_each_observed_response_time():
    from dataclasses import replace

    first = _run(
        lambda request: _response(
            request,
            200,
            headers={"content-type": "application/javascript"},
            body=b"ok",
        ),
        remaining_requests=1,
    ).captures[0]
    from seohead.crawl.resource_fetch import _redirect_event

    redirect = replace(
        first,
        requested_url="https://example.test/a",
        effective_url="https://example.test/a",
        status_code=302,
        effective_status_code=302,
        response_headers=(("location", "/b"),),
        effective_headers=(("location", "/b"),),
        response_time=0.125,
        credentials_used=False,
    )
    final = replace(
        first,
        requested_url="https://example.test/b",
        effective_url="https://example.test/b",
        response_time=0.375,
        credentials_used=True,
    )

    aggregate = _redirect_event(redirect, final, [redirect, final])

    assert aggregate.response_time == 0.5
    assert aggregate.credentials_used is True


def test_capture_count_overflow_stops_with_a_preserved_redirect_prefix(monkeypatch):
    monkeypatch.setattr(resource_fetch, "_MAX_CAPTURES", 1)

    def handler(request):
        if request.url.path == "/a":
            return _response(request, 302, headers={"location": "/b"})
        return _response(
            request,
            200,
            headers={"content-type": "application/javascript"},
            body=b"ok",
        )

    result = _run(handler, remaining_requests=2)

    assert result.capture_state == "resource_budget_exhausted"
    assert result.requests_used == 2
    assert result.captures[0].body_reason == "resource_budget_exhausted"


def test_mime_mismatch_keeps_the_observed_entity_for_the_writer_to_classify():
    result = _run(
        lambda request: _response(
            request,
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"<html>not javascript</html>",
        ),
        remaining_requests=1,
    )

    assert result.capture_state == "body_unavailable"
    assert result.reason == "unsupported_media"
    assert result.captures[0].entity_bytes == b"<html>not javascript</html>"


def test_credentials_are_redacted_and_accept_is_part_of_the_observed_variant():
    settings = _settings()
    settings["http"]["headers"] = {"Authorization": "Bearer secret"}
    result = _run(
        lambda request: _response(
            request,
            200,
            headers={"content-type": "application/javascript"},
            body=b"ok",
        ),
        settings=settings,
        remaining_requests=1,
    )

    event = result.captures[0]
    assert event.credentials_used is True
    assert all(name != "authorization" for name, _value in event.request_headers)
    assert ("accept", event.request_headers[0][1]) in event.request_headers or any(
        name == "accept" and "application/javascript" in value
        for name, value in event.request_headers
    )


def test_stylesheet_uses_its_own_accept_variant():
    result = _run(
        lambda request: _response(
            request, 200, headers={"content-type": "text/css"}, body=b"body{}"
        ),
        kind="stylesheet",
        remaining_requests=1,
    )

    assert result.capture_state == "measured"
    assert any(
        name == "accept" and "text/css" in value
        for name, value in result.captures[0].request_headers
    )


def test_response_limit_reports_truncation_as_body_unavailable():
    result = _run(
        lambda request: _response(
            request,
            200,
            headers={"content-type": "application/javascript"},
            body=b"x" * 11,
        ),
        settings=_settings(**{"limits.max_response_bytes": 10}),
        remaining_requests=1,
    )

    assert result.capture_state == "body_unavailable"
    assert result.reason == "truncated"
    assert result.captures[0].entity_bytes is None


def test_resource_stop_is_a_named_preflight_budget_outcome():
    result = _run(
        lambda _request: pytest.fail("resource stop must happen before transport"),
        before_request=lambda: (_ for _ in ()).throw(
            ResourceStop("resource time budget exhausted")
        ),
        remaining_requests=1,
    )

    assert result.capture_state == "resource_budget_exhausted"
    assert result.reason == "resource time budget exhausted"
    assert result.requests_used == 0
