"""Adversarial same-origin transport checks for declared resources."""

from __future__ import annotations

import httpx

from seohead.crawl import collect, resource_fetch
from seohead.crawl.resource_fetch import fetch_resource
from seohead.crawl.settings import load


def test_explicit_https_default_port_is_same_origin_and_is_measured(monkeypatch):
    monkeypatch.setattr(resource_fetch, "validate_url", lambda url: url)
    monkeypatch.setattr(collect, "validate_url", lambda url: url)
    monkeypatch.setattr(collect, "pinned_target", lambda url: (url, {}, {}))
    settings = load(overrides={"speed.min_delay_seconds": 0})

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/javascript"},
            stream=httpx.ByteStream(b"window.ok = true"),
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
        result = fetch_resource(
            "https://example.test:443/app.js",
            "script",
            settings=settings,
            client=client,
            throttle=None,
            origin_url="https://example.test/",
            robots_allowed=lambda _url: True,
            remaining_requests=1,
        )

    assert result.capture_state == "measured"
    assert result.requests_used == 1
    assert result.captures[0].entity_bytes == b"window.ok = true"


def test_blocked_redirect_prefix_has_no_unfetched_next_url(monkeypatch):
    monkeypatch.setattr(resource_fetch, "validate_url", lambda url: url)
    monkeypatch.setattr(collect, "validate_url", lambda url: url)
    monkeypatch.setattr(collect, "pinned_target", lambda url: (url, {}, {}))
    settings = load(overrides={"speed.min_delay_seconds": 0})

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                302,
                headers={"location": "https://outside.test/app.js"},
                stream=httpx.ByteStream(b""),
                request=request,
            )
        ),
        follow_redirects=False,
    ) as client:
        result = fetch_resource(
            "https://example.test/app.js",
            "script",
            settings=settings,
            client=client,
            throttle=None,
            origin_url="https://example.test/",
            robots_allowed=lambda _url: True,
            remaining_requests=1,
        )

    assert result.capture_state == "excluded_scope"
    assert result.captures[0].redirect_history[-1]["blocked"] is True
    assert result.captures[0].redirect_history[-1]["next_url"] is None
