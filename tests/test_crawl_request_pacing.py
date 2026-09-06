"""Every follow-up request uses the current shared dispatch budget."""

import httpx

from seohead.crawl.collect import collect_urls
from seohead.crawl.settings import load
from seohead.crawl.spider import _DispatchGate
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.crawl.throttle import Throttle


def test_dispatch_gate_applies_a_new_timeout_penalty_to_the_next_turn():
    now = [0.0]
    throttle = Throttle(min_delay=1)
    gate = _DispatchGate(
        throttle, lambda seconds: now.__setitem__(0, now[0] + seconds), lambda: now[0]
    )
    gate.wait_turn()
    throttle.record_timeout()
    gate.wait_turn()
    assert now[0] == throttle.delay == 4


def test_list_retry_waits_for_the_increased_timeout_delay():
    now = [0.0]
    attempts = []

    def fetch(url):
        attempts.append(now[0])
        if len(attempts) == 1:
            raise httpx.ReadTimeout("timeout")
        return httpx.Response(200, text="<html>ok</html>", request=httpx.Request("GET", url))

    result = collect_urls(
        ["https://example.test/"],
        min_delay=1,
        retry_on_timeout=1,
        fetcher=fetch,
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        clock=lambda: now[0],
    )
    assert result.pages[0].status_code == 200
    assert attempts == [0, 4]


def test_sqlite_redirect_resolution_claims_a_turn_for_every_hop(tmp_path):
    now = [0.0]
    attempts = []

    def fetch(url):
        path = httpx.URL(url).path
        attempts.append((now[0], path))
        destination = {"/": "/hop", "/hop": "/final"}.get(path)
        return httpx.Response(
            301 if destination else 200,
            text="" if destination else "<html>done</html>",
            headers={"location": destination} if destination else {"content-type": "text/html"},
            request=httpx.Request("GET", url),
        )

    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(tmp_path / "redirect.sqlite"),
        settings=load(
            overrides={
                "speed.min_delay_seconds": 1,
                "speed.adaptive": False,
                "robots.policy": "ignore",
                "limits.max_urls": 1,
                "discovery.resolve_redirect_destination": True,
            }
        ),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            name: "test" for name in ("python", "sqlite", "httpx", "lxml", "beautifulsoup4")
        },
        fetcher=fetch,
        sleeper=lambda seconds: now.__setitem__(0, now[0] + seconds),
        clock=lambda: now[0],
    )
    assert run.pages == 1
    assert attempts == [(0, "/"), (1, "/hop"), (2, "/final")]
