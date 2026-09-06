"""Level-batched concurrent fetching (#25): faster, still deterministic, still polite.

Every test here is network-free. Latency is simulated with real (small) sleeps
in an injected fetcher, never with an actual socket, so these are regression
tests for the scheduling and throttling logic itself.

Concurrency was reimplemented on top of resumable crawls, the wall-clock
budget, and KeyboardInterrupt handling — all added to ``crawl_site`` after the
original concurrency branch was cut — so a second group of tests below checks
that those features still hold with more than one worker in flight: per-hop
credentials, a checkpoint saved from a complete frontier rather than mid-batch
state, interrupt-safe requeuing, and the duration budget.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from itertools import pairwise

import pytest

from seohead.crawl.spider import crawl_site
from seohead.crawl.throttle import Throttle
from tests.test_crawl_spider import SITE, FakeResponse, _fetcher, page


def _pages_without_timing(pages) -> list[dict]:
    """Every recorded field except response_time, which legitimately varies."""
    out = []
    for record in pages:
        data = asdict(record)
        data.pop("response_time", None)
        out.append(data)
    return out


def _fanned_out_site(n: int, target_factory=page) -> tuple[dict, list[str]]:
    """A root page linking to ``n`` distinct leaves, one BFS level deep."""
    leaves = [f"/leaf{i}" for i in range(n)]
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": target_factory(*leaves),
    }
    for leaf in leaves:
        site[f"https://example.com{leaf}"] = page()
    return site, leaves


# --- determinism: concurrency changes speed, never the recorded output -----


def test_output_is_byte_identical_at_any_concurrency():
    """The acceptance test from #25: same pages.jsonl at concurrency 1 or 8."""
    sequential = crawl_site(
        "https://example.com/", fetcher=_fetcher(SITE), sleeper=lambda _s: None, min_delay=0
    )
    concurrent = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(SITE),
        sleeper=lambda _s: None,
        min_delay=0,
        concurrency=8,
    )
    assert [p.url for p in concurrent.pages] == [p.url for p in sequential.pages]
    assert _pages_without_timing(concurrent.pages) == _pages_without_timing(sequential.pages)
    assert concurrent.links == sequential.links
    assert concurrent.excluded == sequential.excluded
    assert concurrent.max_depth_reached == sequential.max_depth_reached


def test_a_wide_fan_out_is_still_recorded_in_queue_order_under_concurrency():
    """pool.map hands results back in submission order, not completion order —
    this is what makes a concurrent crawl's output reproducible at all."""
    site, leaves = _fanned_out_site(20)
    result = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(site),
        sleeper=lambda _s: None,
        min_delay=0,
        concurrency=8,
        max_urls=50,
    )
    expected = ["https://example.com/"] + [f"https://example.com{leaf}" for leaf in leaves]
    assert [p.url for p in result.pages] == expected


def test_traversal_is_deterministic_across_concurrent_runs():
    site, _ = _fanned_out_site(15)
    first = [
        p.url
        for p in crawl_site(
            "https://example.com/",
            fetcher=_fetcher(site),
            sleeper=lambda _s: None,
            min_delay=0,
            concurrency=6,
        ).pages
    ]
    second = [
        p.url
        for p in crawl_site(
            "https://example.com/",
            fetcher=_fetcher(site),
            sleeper=lambda _s: None,
            min_delay=0,
            concurrency=6,
        ).pages
    ]
    assert first == second


# --- throughput: overlapping wait time is the whole point ------------------


def _slow_fetcher(mapping: dict, latency: float):
    def fetch(url: str):
        time.sleep(latency)
        value = mapping.get(url)
        if value is None:
            return FakeResponse("", status_code=404)
        return value

    return fetch


def test_wall_clock_drops_with_concurrency_up_to_the_cap():
    """A fixture with simulated per-request latency should crawl noticeably
    faster at concurrency 4 than sequentially — the throughput half of #25."""
    site, _ = _fanned_out_site(16)
    latency = 0.03

    started = time.monotonic()
    crawl_site(
        "https://example.com/",
        fetcher=_slow_fetcher(site, latency),
        min_delay=0,
        max_urls=50,
        concurrency=1,
    )
    sequential_elapsed = time.monotonic() - started

    started = time.monotonic()
    crawl_site(
        "https://example.com/",
        fetcher=_slow_fetcher(site, latency),
        min_delay=0,
        max_urls=50,
        concurrency=4,
    )
    concurrent_elapsed = time.monotonic() - started

    # Generous margin against CI scheduling noise: a purely sequential
    # implementation would show no improvement at all (ratio ~= 1.0). Unlike an absolute
    # timing assertion, this one is a ratio between two runs on the same machine moments
    # apart, so machine load moves both sides together rather than only the numerator —
    # which is why this stays a real-time measurement while the pacing test below does not
    # (#107). Overlapping real wait time is the property, and it has no virtual equivalent.
    assert concurrent_elapsed < sequential_elapsed * 0.7


# --- politeness: the floor is shared, not multiplied by the worker count ---


class _VirtualClock:
    """A clock that only moves when something sleeps.

    The property under test is a decision — "the gate spaced these dispatches at least
    ``delay`` apart" — not a duration. Measuring real elapsed time made the assertion a
    statement about how busy the machine was, and it failed under load on unchanged code
    (#107). Here every sleep advances the clock by exactly what was asked for, so the
    dispatch instants are the crawler's own arithmetic, reproducible to the microsecond.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.slept.append(seconds)
            self.now += seconds


def test_min_delay_paces_dispatch_across_workers_not_per_worker():
    """N concurrent workers must not turn one floor into N times the rate.

    Asserted against the crawler's own clock, never the wall clock: a per-worker sleep
    would let several of these dispatch instants collapse onto each other.
    """
    site, leaves = _fanned_out_site(6)
    delay = 0.03
    clock = _VirtualClock()
    dispatch_times: list[float] = []
    leaf_urls = {f"https://example.com{leaf}" for leaf in leaves}
    order_lock = threading.Lock()

    def fetch(url: str):
        if url in leaf_urls:
            with order_lock:
                dispatch_times.append(clock())
        value = site.get(url)
        return value if value is not None else FakeResponse("", status_code=404)

    crawl_site(
        "https://example.com/",
        fetcher=fetch,
        min_delay=delay,
        max_urls=50,
        concurrency=4,
        clock=clock,
        sleeper=clock.sleep,
    )

    assert len(dispatch_times) == len(leaves)
    # Workers finish in whatever order the pool schedules them; what the gate promises is
    # about the instants it handed out, not about which worker got which.
    gaps = [b - a for a, b in pairwise(sorted(dispatch_times))]
    assert all(gap >= delay for gap in gaps), gaps


def test_the_gate_hands_out_one_turn_per_delay_no_matter_how_many_ask():
    """The unit underneath the crawl-level test above: four workers, one shared clock."""
    from seohead.crawl.spider import _DispatchGate

    clock = _VirtualClock()
    throttle = Throttle(min_delay=0.25, max_delay=60.0, max_concurrency=4, adaptive=False)
    gate = _DispatchGate(throttle, clock.sleep, clock)

    instants = []
    for _ in range(4):
        gate.wait_turn()
        instants.append(clock())

    assert instants == [0.0, 0.25, 0.5, 0.75]


# --- retry pacing: a retried timeout is a real dispatch too (#196) ---------


def test_a_timeout_retry_is_paced_like_any_other_dispatch_sequential():
    """#196: fetch_one's retry loop used to call ``wait`` once before every attempt
    it would ever make, then loop straight back to the network on a timeout — so
    retry_on_timeout=2 sent three requests back-to-back regardless of min_delay.
    Every attempt, including a retry, must now wait its own turn."""
    clock = _VirtualClock()
    attempt_times: list[float] = []

    def fetch(url: str):
        if url.endswith("/robots.txt"):
            return FakeResponse("User-agent: *\n", headers={"content-type": "text/plain"})
        attempt_times.append(clock())
        raise TimeoutError("read timed out")

    result = crawl_site(
        "https://example.com/",
        fetcher=fetch,
        min_delay=1.0,
        retry_on_timeout=2,
        sleeper=clock.sleep,
        clock=clock,
    )

    assert len(attempt_times) == 3
    gaps = [b - a for a, b in pairwise(attempt_times)]
    assert all(gap >= 1.0 for gap in gaps), gaps
    assert result.pages[0].error_kind == "timeout"


def test_a_timeout_retry_is_paced_like_any_other_dispatch_concurrent():
    """Same defect, concurrent mode: every worker's retry shares the one dispatch
    gate, so no two attempts across the whole crawl -- first tries and retries
    alike -- may land closer together than min_delay."""
    clock = _VirtualClock()
    attempt_times: list[float] = []
    lock = threading.Lock()

    def fetch(url: str):
        if url.endswith("/robots.txt"):
            return FakeResponse("User-agent: *\n", headers={"content-type": "text/plain"})
        if url == "https://example.com/":
            return page("/leaf0", "/leaf1")
        with lock:
            attempt_times.append(clock())
        raise TimeoutError("read timed out")

    result = crawl_site(
        "https://example.com/",
        fetcher=fetch,
        min_delay=1.0,
        retry_on_timeout=1,
        concurrency=2,
        max_urls=50,
        sleeper=clock.sleep,
        clock=clock,
    )

    assert len(attempt_times) == 4  # two leaves, one retry each
    gaps = [b - a for a, b in pairwise(sorted(attempt_times))]
    assert all(gap >= 1.0 for gap in gaps), gaps
    assert {p.error_kind for p in result.pages if p.url != "https://example.com/"} == {"timeout"}


# --- circuit breaker: the shared signal still stops the crawl --------------


def test_repeated_timeouts_stop_a_concurrent_crawl_before_the_queue_drains():
    targets = [f"/p{i}" for i in range(12)]
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page(*targets),
    }
    for path in targets:
        site[f"https://example.com{path}"] = TimeoutError("read timed out")

    result = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(site),
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=50,
        concurrency=4,
    )
    assert result.partial is True
    assert "timeouts" in result.stopped_reason
    assert len(result.pages) < len(targets) + 1, "must stop before exhausting the queue"


def test_repeated_server_refusals_stop_a_concurrent_crawl():
    targets = [f"/p{i}" for i in range(12)]
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page(*targets),
    }
    for path in targets:
        site[f"https://example.com{path}"] = FakeResponse("", status_code=503)

    result = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(site),
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=50,
        concurrency=4,
    )
    assert result.partial is True
    assert "refused repeatedly" in result.stopped_reason
    assert len(result.pages) < len(targets) + 1, "must stop before exhausting the queue"


def test_circuit_breaker_trip_point_is_stable_regardless_of_thread_scheduling():
    """A batch large enough that several workers can race record_timeout()
    simultaneously must still trip the breaker at the same record the
    sequential crawler would have stopped at — not wherever OS scheduling
    happened to land, and not a moving target across repeated runs.

    Real threads are used deliberately, over many repeats: the bug this
    guards against (reading Throttle's live, worker-mutated streak instead of
    a queue-ordered replay) is invisible with a small batch, since 2 or 3
    racing failures still stay under the trip threshold either way.
    """
    good = [f"/good{i}" for i in range(20)]
    bad = [f"/bad{i}" for i in range(8)]
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page(*(good + bad)),
    }
    for g in good:
        site[f"https://example.com{g}"] = page()
    for b in bad:
        site[f"https://example.com{b}"] = TimeoutError("read timed out")

    baseline = None
    for _ in range(10):
        result = crawl_site(
            "https://example.com/",
            fetcher=_fetcher(site),
            sleeper=lambda _s: None,
            min_delay=0,
            max_urls=100,
            concurrency=8,
        )
        assert result.finish_reason == "errors"
        bad_fetched = [p.url for p in result.pages if "/bad" in p.url]
        # Exactly the streak length the sequential crawler would record: the
        # first 5 bad targets in queue order, never more, never fewer.
        assert bad_fetched == [f"https://example.com/bad{i}" for i in range(5)]
        if baseline is None:
            baseline = len(result.pages)
        else:
            assert len(result.pages) == baseline


def test_concurrent_breaker_never_records_more_failures_than_sequential():
    """#475: the ordered consumer correctly stops at the URL that trips the
    breaker, but the merge of already-completed later requests in the same
    batch must not fold in more of the same failure than the caller
    configured — the concurrent crawl must record exactly what a sequential
    crawl with the same ``stop_after_consecutive_timeouts`` would have.
    """
    good = [f"/good{i}" for i in range(40)]
    bad = [f"/bad{i}" for i in range(8)]
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page(*(good + bad)),
    }
    for g in good:
        site[f"https://example.com{g}"] = page()

    base_fetcher = _fetcher(site)

    def counting_fetcher(url):
        if url.rsplit("/", 1)[-1].startswith("bad"):
            raise TimeoutError("read timed out")
        return base_fetcher(url)

    kwargs = dict(
        fetcher=counting_fetcher,
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=200,
        stop_after_consecutive_timeouts=3,
    )
    seq = crawl_site("https://example.com/", concurrency=1, **kwargs)
    conc = crawl_site("https://example.com/", concurrency=8, **kwargs)

    bad_urls = {f"https://example.com{b}" for b in bad}
    seq_bad = [p for p in seq.pages if p.url in bad_urls]
    conc_bad = [p for p in conc.pages if p.url in bad_urls]
    assert len(conc_bad) == len(seq_bad) == 3

    # Negative control: an all-success crawl is unaffected by this change —
    # same pages, same exclusions, same finish reason, at any concurrency.
    ok_kwargs = dict(fetcher=base_fetcher, sleeper=lambda _s: None, min_delay=0, max_urls=200)
    seq_ok = crawl_site("https://example.com/", concurrency=1, **ok_kwargs)
    conc_ok = crawl_site("https://example.com/", concurrency=8, **ok_kwargs)
    assert {p.url for p in conc_ok.pages} == {p.url for p in seq_ok.pages}
    assert conc_ok.excluded == seq_ok.excluded
    assert conc_ok.finish_reason == seq_ok.finish_reason == "finished"


def test_breaker_trip_merges_later_completions_from_the_same_batch(tmp_path):
    """#304: ordered consumption stops at the URL that trips the breaker, but
    later URLs in the same batch run concurrently and may already have a
    response by then. Those completions must be recorded and dropped from the
    checkpoint, not requeued as if they were never dispatched — a genuinely
    unresolved URL in the same batch must still be requeued as before.

    p1-p4 fail first to build the streak to one short of the trip threshold.
    p5-p8 land in the next batch: p5 waits for p6 and p7 to be dispatched,
    releases them, waits for both to finish, then raises the fifth timeout
    that trips the breaker. p8 sleeps past the point where the breaker result
    is consumed, so it is still running (never completed) when this batch's
    remainder is sorted into "keep" versus "requeue".
    """
    targets = [f"/p{i}" for i in range(1, 9)]
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page(*targets),
    }

    p6_started = threading.Event()
    p7_started = threading.Event()
    release_successes = threading.Event()
    p6_finished = threading.Event()
    p7_finished = threading.Event()

    def fetch(url: str):
        if url == "https://example.com/p6":
            p6_started.set()
            assert release_successes.wait(2), "p5 never released p6"
            p6_finished.set()
            return page()
        if url == "https://example.com/p7":
            p7_started.set()
            assert release_successes.wait(2), "p5 never released p7"
            p7_finished.set()
            return page()
        if url == "https://example.com/p5":
            assert p6_started.wait(2), "p6 was not dispatched alongside p5"
            assert p7_started.wait(2), "p7 was not dispatched alongside p5"
            release_successes.set()
            assert p6_finished.wait(2), "p6 did not finish before p5"
            assert p7_finished.wait(2), "p7 did not finish before p5"
            # The events above only mark that p6/p7's own function bodies are
            # done; the executor still has to record each future's result on
            # its own thread. A short pause closes that window so both
            # futures are reliably `.done()` by the time this raise lets the
            # main thread consume p5's own (ordered) result.
            time.sleep(0.05)
            raise TimeoutError("synthetic fifth timeout")
        if url == "https://example.com/p8":
            time.sleep(0.5)
            return page()
        # p1-p4: fail immediately, no synchronization needed.
        return site.get(url, TimeoutError("read timed out"))

    def wrapped(url: str):
        value = fetch(url)
        if isinstance(value, Exception):
            raise value
        return value

    state_path = str(tmp_path / "state.json")
    result = crawl_site(
        "https://example.com/",
        fetcher=wrapped,
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=50,
        concurrency=4,
        adaptive=False,
        state_path=state_path,
    )

    assert result.finish_reason == "errors"
    recorded = {p.url for p in result.pages}
    assert p6_finished.is_set() and p7_finished.is_set()

    # The positive control: p6 and p7 finished before the breaker's own
    # result was consumed, so they must be recorded, not requeued.
    assert "https://example.com/p6" in recorded
    assert "https://example.com/p7" in recorded

    with open(state_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    queued = {u for u, _d in saved["queue"]}

    assert "https://example.com/p6" not in queued
    assert "https://example.com/p7" not in queued

    # The negative control: p8 never finished by the time the batch's
    # remainder was sorted, so it must stay on the checkpointed frontier
    # rather than being silently treated as recorded.
    assert "https://example.com/p8" not in recorded
    assert "https://example.com/p8" in queued

    # Resuming must not repeat the two requests that already completed.
    hits: list[str] = []

    def resumed_fetch(url: str):
        hits.append(url)
        return page()

    resumed = crawl_site(
        "https://example.com/",
        fetcher=resumed_fetch,
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=50,
        concurrency=4,
        adaptive=False,
        state_path=state_path,
    )
    assert resumed.resumed is True
    assert resumed.finish_reason == "finished"
    assert "https://example.com/p6" not in hits
    assert "https://example.com/p7" not in hits
    assert "https://example.com/p8" in hits


def test_the_url_budget_is_exact_under_concurrency():
    site, _ = _fanned_out_site(20)
    result = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(site),
        sleeper=lambda _s: None,
        min_delay=0,
        concurrency=8,
        max_urls=5,
    )
    assert len(result.pages) == 5
    assert result.partial is True
    assert "url limit" in result.stopped_reason


# --- adaptive concurrency lives on Throttle ---------------------------------


def test_concurrency_starts_conservative_not_at_the_ceiling():
    t = Throttle(max_concurrency=4)
    assert 1 < t.concurrency < 4


def test_concurrency_widens_only_after_sustained_success():
    t = Throttle(max_concurrency=4)
    start = t.concurrency
    t.record_response(0.01, ok=True)
    assert t.concurrency == start, "one good response must not be enough on its own"
    for _ in range(20):
        t.record_response(0.01, ok=True)
    assert t.concurrency == 4, "sustained success should reach the configured ceiling"


def test_a_timeout_collapses_concurrency_back_to_one():
    t = Throttle(max_concurrency=4)
    for _ in range(20):
        t.record_response(0.01, ok=True)
    assert t.concurrency > 1
    t.record_timeout()
    assert t.concurrency == 1


def test_a_server_error_collapses_concurrency_back_to_one():
    t = Throttle(max_concurrency=4)
    for _ in range(20):
        t.record_response(0.01, ok=True)
    assert t.concurrency > 1
    t.record_server_error(503)
    assert t.concurrency == 1


def test_a_single_worker_ceiling_behaves_exactly_like_today():
    """max_concurrency=1 (the default) must never widen, matching the crawler
    that existed before #25."""
    t = Throttle()
    for _ in range(50):
        t.record_response(0.01, ok=True)
    assert t.concurrency == 1


@pytest.mark.parametrize("bad_concurrency", [0, -1])
def test_concurrency_is_never_configured_below_one(bad_concurrency):
    t = Throttle(max_concurrency=bad_concurrency)
    assert t.max_concurrency == 1
    assert t.concurrency == 1


# --- min_delay/max_delay stay consistent after construction (#150) ---------


def test_raising_min_delay_above_max_delay_raises_max_delay_too():
    t = Throttle(min_delay=0.5, max_delay=5.0)
    t.min_delay = 20.0  # a robots.txt Crawl-delay wider than the crawl's own ceiling
    assert t.max_delay >= 20.0


def test_delay_never_sinks_below_min_delay_once_inverted_by_a_late_mutation():
    """Not just __init__: min_delay can be raised well after construction (a
    robots.txt Crawl-delay is only known once robots.txt is fetched), the way
    ``spider.py`` raises it once the delay itself has already been pushed to
    match (mirroring the site's own request). The invariant must hold through
    every mutator from that point on, including the ones (record_timeout,
    record_server_error) that are supposed to widen the delay further, not
    let it collapse back under a stale ceiling."""
    t = Throttle(min_delay=0.5, max_delay=5.0, adaptive=True)
    t.min_delay = 100.0
    t.delay = max(t.delay, t.min_delay)
    assert t.delay >= t.min_delay

    t.record_response(0.01, ok=True)
    assert t.delay >= t.min_delay

    t.record_timeout()
    assert t.delay >= t.min_delay

    t.record_server_error(503)
    assert t.delay >= t.min_delay


# --- composing with everything main gained after this branch was cut -------
#
# The concurrency branch predates resumable crawls, the wall-clock budget, and
# KeyboardInterrupt handling. Each of those must still hold with more than one
# request in flight — these tests are the acceptance criteria for that.


def test_credentials_are_still_resolved_per_hop_under_concurrency(monkeypatch):
    """A stale host from another worker's hop must never decide this URL's
    headers — the per-hop guarantee must survive concurrent dispatch."""
    import seohead.crawl.spider as spider_mod

    seen_hosts: list[str] = []
    monkeypatch.setattr(
        spider_mod,
        "resolve_credential_headers",
        lambda entries, host: seen_hosts.append(host) or {},
    )
    site, leaves = _fanned_out_site(10)
    crawl_site(
        "https://example.com/",
        fetcher=_fetcher(site),
        sleeper=lambda _s: None,
        min_delay=0,
        concurrency=4,
        credential_headers=[{"host": "example.com", "headers": {}}],
    )
    # One resolution per fetch (root + every leaf), every one for this host —
    # order is not guaranteed once several workers resolve concurrently, but
    # every single resolution must still be for the URL's own host.
    assert len(seen_hosts) == 1 + len(leaves)
    assert set(seen_hosts) == {"example.com"}


def test_a_circuit_breaker_trip_mid_batch_checkpoints_the_untouched_tail(tmp_path):
    """When the breaker trips partway through a batch, the URLs after the
    tripping one in queue order must still be on the saved frontier — the
    checkpoint must reflect a consistent frontier, not mid-batch state."""
    targets = [f"/p{i}" for i in range(8)]
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page(*targets),
    }
    for path in targets:
        site[f"https://example.com{path}"] = TimeoutError("read timed out")

    state_path = str(tmp_path / "state.json")
    result = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(site),
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=50,
        concurrency=4,
        state_path=state_path,
    )
    assert result.finish_reason == "errors"

    with open(state_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    queued = {u for u, _d in saved["queue"]}
    fetched = {p.url for p in result.pages}
    all_targets = {f"https://example.com{p}" for p in targets}
    # Every target URL is either recorded as fetched or still on the
    # checkpointed frontier — none may have vanished from both.
    assert fetched | queued >= all_targets
    assert not (fetched & queued), "a URL cannot be both fetched and still queued"


def test_a_keyboard_interrupt_mid_batch_requeues_the_unprocessed_tail(tmp_path):
    """The interrupted URL, and anything after it in the same batch that this
    run never confirmed as processed, must survive to resume."""
    targets = [f"/p{i}" for i in range(6)]
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page(*targets),
    }
    for path in targets:
        site[f"https://example.com{path}"] = page()

    def fetch(url):
        if url == "https://example.com/p1":
            raise KeyboardInterrupt
        return site.get(url, FakeResponse("", status_code=404))

    state_path = str(tmp_path / "state.json")
    result = crawl_site(
        "https://example.com/",
        fetcher=fetch,
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=50,
        concurrency=4,
        state_path=state_path,
    )
    assert result.finish_reason == "interrupted"
    assert result.partial is True

    with open(state_path, encoding="utf-8") as fh:
        saved = json.load(fh)
    queued = {u for u, _d in saved["queue"]}
    assert "https://example.com/p1" in queued, "the interrupted URL must be retried on resume"


def test_max_seconds_still_ends_a_concurrent_crawl_with_a_duration_finish_reason():
    site, _ = _fanned_out_site(30)
    ticking = {"t": 0.0}

    def fake_clock():
        ticking["t"] += 2  # each check sees two seconds pass
        return ticking["t"]

    result = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(site),
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=100,
        max_seconds=5,
        clock=fake_clock,
        concurrency=4,
    )
    assert result.finish_reason == "duration_limit"
    assert result.partial is True
    assert len(result.pages) < 31, "must stop well before exhausting the site"


def test_resuming_a_concurrent_crawl_does_not_refetch_completed_urls(tmp_path):
    site, leaves = _fanned_out_site(6)
    state_path = str(tmp_path / "state.json")
    out_path = str(tmp_path / "pages.jsonl")

    first = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(site),
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=1,
        state_path=state_path,
        out_path=out_path,
        concurrency=4,
    )
    assert {p.url for p in first.pages} == {"https://example.com/"}

    hits: list[str] = []

    def fetch(url):
        hits.append(url)
        return site.get(url, FakeResponse("", status_code=404))

    second = crawl_site(
        "https://example.com/",
        fetcher=fetch,
        sleeper=lambda _s: None,
        min_delay=0,
        max_urls=50,
        state_path=state_path,
        out_path=out_path,
        concurrency=4,
    )
    assert second.resumed is True
    assert "https://example.com/" not in hits
    expected_leaves = {f"https://example.com{leaf}" for leaf in leaves}
    assert set(hits) == {"https://example.com/robots.txt"} | expected_leaves
    assert {p.url for p in second.pages} == {"https://example.com/"} | expected_leaves
    assert second.finish_reason == "finished"
