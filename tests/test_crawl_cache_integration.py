"""The HTTP cache wired into fetch_one, collect_urls and crawl_site. No network.

Two things matter beyond "the cache works": a report built partly from cache must say so
(per-URL ``cache_status`` and aggregate ``cache_stats``/``cache_replay``), and a cache hit must
not consume a throttle delay slot or a concurrent dispatch turn — proved here by counting calls
to the ``wait`` callback, which is the only thing that ever consumes one.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar

from seohead.crawl.cache import ResponseCache
from seohead.crawl.collect import collect_urls, fetch_one
from seohead.crawl.spider import crawl_site
from seohead.recon.net import UA


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}


def page(*links: str, title: str = "t") -> FakeResponse:
    body = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return FakeResponse(
        f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>",
        headers={"content-type": "text/html; charset=utf-8", "cache-control": "max-age=3600"},
    )


class CountingFetcher:
    """Counts real network calls per URL — the thing a cache hit must never touch."""

    def __init__(self, mapping: dict[str, FakeResponse]):
        self.mapping = mapping
        self.calls: list[str] = []

    def __call__(self, url: str) -> FakeResponse:
        self.calls.append(url)
        value = self.mapping.get(url)
        return value if value is not None else FakeResponse("", status_code=404)


# ── fetch_one: hit / miss / revalidate through the real client path ────────


class FakeClient:
    """Stands in for the httpx client fetch_one otherwise builds itself."""

    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def get(self, target, *, headers, extensions):
        self.requests.append({"target": target, "headers": headers})
        return self.responses.pop(0)


def _patched(monkeypatch):
    import seohead.crawl.collect as collect_mod

    monkeypatch.setattr(collect_mod, "validate_url", lambda u: u)
    monkeypatch.setattr(collect_mod, "pinned_target", lambda u: (u, {"Host": "example.com"}, {}))


def test_a_fresh_fetch_is_cached_and_the_next_call_is_a_hit_that_never_touches_the_client(
    monkeypatch, tmp_path
):
    _patched(monkeypatch)
    cache = ResponseCache(tmp_path)
    client = FakeClient(
        [
            FakeResponse(
                "<html><head><title>t</title></head><body></body></html>",
                headers={"content-type": "text/html", "cache-control": "max-age=3600"},
            )
        ]
    )
    first, _ = fetch_one("https://example.com/", client=client, cache=cache)
    assert first.cache_status == "miss"
    assert len(client.requests) == 1

    second, _ = fetch_one("https://example.com/", client=client, cache=cache)
    assert second.cache_status == "hit"
    assert len(client.requests) == 1, "a hit must never call the client again"
    assert second.title == "t"


def test_a_stale_entry_with_an_etag_revalidates_with_a_conditional_request(monkeypatch, tmp_path):
    _patched(monkeypatch)
    cache = ResponseCache(tmp_path)
    client = FakeClient(
        [
            FakeResponse(
                "<html><head><title>t</title></head><body></body></html>",
                headers={"content-type": "text/html", "cache-control": "max-age=0", "etag": '"v1"'},
            ),
            FakeResponse("", status_code=304, headers={}),
        ]
    )
    first, _ = fetch_one("https://example.com/", client=client, cache=cache)
    assert first.cache_status == "miss"

    second, _ = fetch_one("https://example.com/", client=client, cache=cache)
    assert second.cache_status == "revalidated"
    assert second.title == "t"  # body came back from the cache, not from the 304
    assert client.requests[1]["headers"]["If-None-Match"] == '"v1"'
    assert cache.stats == {
        "hits": 0,
        "revalidations": 1,
        "stores": 1,
        "bypassed": 0,
        "invalidated": 0,
    }


def test_a_304_updates_seo_metadata_but_preserves_the_cached_payload_and_variant(
    monkeypatch, tmp_path
):
    _patched(monkeypatch)
    cache = ResponseCache(tmp_path)
    client = FakeClient(
        [
            FakeResponse(
                "<html><head><title>Cached</title></head><body></body></html>",
                headers={
                    "content-type": "text/html",
                    "content-encoding": "gzip",
                    "content-length": "999",
                    "cache-control": "max-age=0",
                    "etag": '"old"',
                    "vary": "User-Agent",
                    "x-robots-tag": "noindex",
                },
            ),
            FakeResponse(
                "",
                status_code=304,
                headers={
                    "cache-control": "max-age=3600",
                    "etag": '"new"',
                    "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT",
                    "x-robots-tag": "all",
                    # A 304 has no body, so these must not replace metadata for the cached one.
                    "content-type": "text/plain",
                    "content-encoding": "br",
                    "content-length": "0",
                },
            ),
        ]
    )

    first, _ = fetch_one("https://example.com/", client=client, cache=cache)
    second, _ = fetch_one("https://example.com/", client=client, cache=cache)
    third, _ = fetch_one("https://example.com/", client=client, cache=cache)

    assert first.x_robots == "noindex"
    assert second.cache_status == "revalidated"
    assert second.x_robots == "all"
    assert second.content_type == "text/html"
    assert second.content_encoding == "gzip"
    assert second.title == "Cached"
    assert client.requests[1]["headers"]["If-None-Match"] == '"old"'
    assert third.cache_status == "hit"
    assert len(client.requests) == 2
    refreshed = cache.decide("https://example.com/", {"User-Agent": UA})
    assert refreshed.entry.etag == '"new"'
    assert refreshed.entry.last_modified == "Wed, 01 Jan 2025 00:00:00 GMT"
    assert cache.decide("https://example.com/", {"User-Agent": "other"}).status == "miss"


def test_a_second_user_agent_never_replays_the_first_ones_body(monkeypatch, tmp_path):
    """#131: a run configured with one User-Agent must never hand back the body a prior run
    fetched under a different one, even though the origin here never sends Vary at all — the
    ordinary case, not the exception. The second client has no response queued at all, so any
    "hit" here could only be a wrongly-replayed body, and popping an empty list would raise."""
    _patched(monkeypatch)
    cache = ResponseCache(tmp_path)
    desktop_client = FakeClient(
        [
            FakeResponse(
                "<html><head><title>DESKTOP</title></head><body></body></html>",
                headers={"content-type": "text/html", "cache-control": "max-age=3600"},
            )
        ]
    )
    first, _ = fetch_one(
        "https://example.com/", client=desktop_client, cache=cache, user_agent="desktop-ua"
    )
    assert first.cache_status == "miss"
    assert first.title == "DESKTOP"

    # Zero queued responses: FakeClient.get() records the request before it pops, so a
    # genuine attempt to reach it is visible even though the call then has nothing to return.
    # A wrongly-served cache hit, by contrast, would never call .get() at all — the client
    # would stay untouched and second.title would come back "DESKTOP".
    googlebot_client = FakeClient([])
    second, _ = fetch_one(
        "https://example.com/",
        client=googlebot_client,
        cache=cache,
        user_agent="Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    )
    assert len(googlebot_client.requests) == 1, "a different identity must reach the real client"
    assert second.cache_status != "hit", "must never replay the first identity's stored body"
    assert second.title != "DESKTOP"


def test_a_credentialed_request_never_touches_the_cache(monkeypatch, tmp_path):
    _patched(monkeypatch)
    cache = ResponseCache(tmp_path)
    client = FakeClient(
        [
            FakeResponse(
                "<html><head><title>a</title></head></html>",
                headers={"content-type": "text/html", "cache-control": "max-age=3600"},
            ),
            FakeResponse(
                "<html><head><title>a</title></head></html>",
                headers={"content-type": "text/html", "cache-control": "max-age=3600"},
            ),
        ]
    )
    for _ in range(2):
        record, _ = fetch_one(
            "https://example.com/",
            client=client,
            cache=cache,
            extra_headers={"Authorization": "Bearer secret-token"},
        )
        assert record.cache_status == ""
    assert len(client.requests) == 2, "credentialed fetches must always hit the network"


# ── the pacing guarantee: a hit consumes no wait() call at all ──────────────


def test_a_cache_hit_never_calls_wait_a_miss_always_does(tmp_path):
    cache = ResponseCache(tmp_path)
    site = {"https://example.com/": page()}
    fetcher = CountingFetcher(site)
    waits = []

    fetch_one("https://example.com/", fetcher=fetcher, cache=cache, wait=lambda: waits.append(1))
    assert waits == [1], "the first (miss) fetch must wait for its delay slot"

    fetch_one("https://example.com/", fetcher=fetcher, cache=cache, wait=lambda: waits.append(1))
    assert waits == [1], "a hit must not consume a second delay slot"
    assert fetcher.calls == ["https://example.com/"], "a hit must never touch the network"


def test_a_stale_fetcher_backed_entry_is_refetched_in_full_not_revalidated(tmp_path):
    """A fetcher has no way to carry conditional headers, so staleness behind one means a full
    re-fetch rather than a silent skip of the cache."""
    cache = ResponseCache(tmp_path)
    stale = FakeResponse(
        "<html><head><title>old</title></head></html>",
        headers={"content-type": "text/html", "cache-control": "max-age=0", "etag": '"v1"'},
    )
    fresh = FakeResponse(
        "<html><head><title>new</title></head></html>",
        headers={"content-type": "text/html", "cache-control": "max-age=3600"},
    )
    fetcher = CountingFetcher({"https://example.com/": stale})
    first, _ = fetch_one("https://example.com/", fetcher=fetcher, cache=cache)
    assert first.title == "old"

    fetcher.mapping["https://example.com/"] = fresh
    second, _ = fetch_one("https://example.com/", fetcher=fetcher, cache=cache)
    assert second.cache_status == "miss"
    assert second.title == "new"
    assert len(fetcher.calls) == 2


# ── collect_urls: per-URL and aggregate visibility ──────────────────────────


def test_collect_urls_reports_cache_status_per_page_and_stats_in_aggregate(tmp_path):
    cache = ResponseCache(tmp_path)
    site = {"https://example.com/a": page(), "https://example.com/b": page()}
    fetcher = CountingFetcher(site)

    first = collect_urls(list(site), fetcher=fetcher, cache=cache, sleeper=lambda _s: None)
    assert [p.cache_status for p in first.pages] == ["miss", "miss"]

    second = collect_urls(list(site), fetcher=fetcher, cache=cache, sleeper=lambda _s: None)
    assert [p.cache_status for p in second.pages] == ["hit", "hit"]
    assert second.cache_stats["hits"] == 2
    assert second.cache_replay is False
    assert len(fetcher.calls) == 2, "the second run must not re-fetch anything"


def test_a_bypassed_lookup_is_reported_distinctly_from_no_cache_configured(monkeypatch, tmp_path):
    """#462: a cache that was configured but whose own lookup for this URL is unusable must
    not be reported the same way as "no cache was configured for this run at all"."""

    class BypassingCache:
        mode = "record"
        stats: ClassVar[dict] = {}

        def decide(self, url, headers):
            from seohead.crawl.cache import CacheOutcome

            return CacheOutcome("bypass")

        def store(self, *args, **kwargs):
            raise AssertionError("store should not be called on a bypassed lookup")

    record, _ = fetch_one(
        "https://example.com/",
        fetcher=CountingFetcher({"https://example.com/": page()}),
        cache=BypassingCache(),
    )
    assert record.cache_status == "bypass"
    assert record.cache_status not in ("", "hit", "revalidated", "miss")


def test_with_no_cache_configured_cache_status_stays_empty(tmp_path):
    site = {"https://example.com/a": page()}
    result = collect_urls(list(site), fetcher=CountingFetcher(site), sleeper=lambda _s: None)
    assert result.pages[0].cache_status == ""
    assert result.cache_stats == {}


# ── crawl_site: same guarantee, plus the concurrent dispatch path ──────────


def test_crawl_site_reuses_cached_pages_across_two_runs(tmp_path):
    cache = ResponseCache(tmp_path)
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page("/a"),
        "https://example.com/a": page(),
    }
    fetcher = CountingFetcher(site)
    first = crawl_site(
        "https://example.com/", fetcher=fetcher, sleeper=lambda _s: None, min_delay=0, cache=cache
    )
    assert first.cache_stats["stores"] == 2  # "/" and "/a"; robots.txt is not cached here

    fetcher.calls.clear()
    second = crawl_site(
        "https://example.com/", fetcher=fetcher, sleeper=lambda _s: None, min_delay=0, cache=cache
    )
    assert [p.cache_status for p in second.pages] == ["hit", "hit"]
    # robots.txt is re-read every run (it is not routed through fetch_one/the cache at all);
    # the two page URLs must not be.
    assert fetcher.calls == ["https://example.com/robots.txt"]


def test_a_cache_hit_does_not_consume_a_dispatch_turn_under_concurrency(tmp_path):
    """The #16 concurrency requirement, stated directly: a hit costs no request, so the shared
    dispatch gate must never be asked to pace one."""
    cache = ResponseCache(tmp_path)
    leaves = [f"/leaf{i}" for i in range(6)]
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page(*leaves),
    }
    for leaf in leaves:
        site[f"https://example.com{leaf}"] = page()
    fetcher = CountingFetcher(site)

    crawl_site(
        "https://example.com/",
        fetcher=fetcher,
        sleeper=lambda _s: None,
        min_delay=0,
        concurrency=4,
        cache=cache,
    )
    fetcher.calls.clear()

    dispatch_slots = []
    real_sleeper = lambda s: dispatch_slots.append(s)  # noqa: E731
    result = crawl_site(
        "https://example.com/",
        fetcher=fetcher,
        sleeper=real_sleeper,
        min_delay=5.0,  # would make the test time out if a single hit had to wait for it
        concurrency=4,
        cache=cache,
    )
    assert fetcher.calls == ["https://example.com/robots.txt"], "only robots.txt may be re-read"
    assert dispatch_slots == [], "no hit may consume a paced dispatch slot"
    assert {p.cache_status for p in result.pages} == {"hit"}


def test_concurrent_crawls_sharing_one_cache_do_not_corrupt_it(tmp_path):
    """Several crawl_site calls against disjoint sites, sharing one ResponseCache, run
    concurrently without exceptions or cross-contaminated entries — the cross-process safety
    story exercised in-process."""
    cache = ResponseCache(tmp_path)

    def run(n: int) -> list[str]:
        host = f"https://example{n}.com"
        site = {
            f"{host}/robots.txt": FakeResponse(
                "User-agent: *\n", headers={"content-type": "text/plain"}
            ),
            f"{host}/": page(),
        }
        fetcher = CountingFetcher(site)
        result = crawl_site(
            f"{host}/", fetcher=fetcher, sleeper=lambda _s: None, min_delay=0, cache=cache
        )
        return [p.cache_status for p in result.pages]

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(run, range(8)))

    assert all(statuses == ["miss"] for statuses in outcomes)
    assert cache.stats["stores"] == 8
