"""Breadth-first link discovery — the part that makes this a crawler.

Fetching a list someone else produced tests the fetcher. Following links is the
thing that closes the gap: without it the toolkit cannot obtain a URL list of
its own, which is the whole reason this module exists.

Traversal is deterministic given identical responses: the frontier is a queue,
children are enqueued in document order rather than sorted, and every exclusion
is recorded as data rather than dropped silently. That guarantee survives
concurrency too — a slice of the frontier is fetched as a batch of concurrent
requests, but results are always folded back in the order they were queued —
into ``result.pages``, the circuit breaker, redirect and link enqueueing, and
the checkpoint — before anything downstream sees them, so the recorded output
does not depend on which request happened to answer first.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from seohead.crawl import state as crawl_state
from seohead.crawl.cache import ResponseCache
from seohead.crawl.collect import (
    MAX_RESPONSE_BYTES,
    CrawlResult,
    PageRecord,
    _write,
    fetch_one,
)
from seohead.crawl.settings import checked_url_budget, resolve_credential_headers
from seohead.crawl.throttle import MAX_CONCURRENCY_CEILING, MAX_DELAY_S, Throttle
from seohead.recon.net import UA, http_client, normalize_url, registrable_domain
from seohead.tools.robots import crawl_delay, is_allowed, match_path, parse_robots

MAX_DEPTH_CEILING = 20
ROBOTS_TOKEN = "SEOHEAD-Tools"
EMPTY_ROBOTS = {"allow": [], "disallow": [], "groups": [], "crawl_delay": None}
# RFC 9309 §2.3.1.2 asks crawlers to follow "at least five consecutive redirects"
# before giving up on robots.txt — this is that number, not an arbitrary one.
MAX_ROBOTS_REDIRECTS = 5
# Matches Throttle.should_stop / host_is_failing's own default limit — kept as
# a separate constant here because the circuit breaker's *decision* is no
# longer read off Throttle's live counters (see _fold_failure_streaks below).
STOP_AFTER_CONSECUTIVE_FAILURES = 5


class _DispatchGate:
    """Spaces out request *dispatch* across every concurrent worker sharing one
    origin, so ``min_delay`` still means "at least this long between requests
    to the origin" once more than one worker is fetching for it.

    Each worker independently sleeping ``throttle.delay`` before its own
    request would honour the floor against its own clock only: with N workers
    doing that in parallel, N requests would go out every ``delay`` seconds
    instead of one, multiplying the configured rate by N. This gate hands out
    dispatch turns from a single shared clock instead, so the gap between any
    two dispatches to the origin is still at least ``delay`` — concurrency then
    buys overlap on the response *wait*, never on how densely requests are sent.
    """

    def __init__(
        self,
        throttle: Throttle,
        sleeper: Callable[[float], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._throttle = throttle
        self._sleeper = sleeper
        # The crawl's own clock, not the wall clock: the pacing decision is then testable
        # against a fake clock instead of by measuring real elapsed time, which made the
        # cross-worker pacing test fail whenever the machine was busy (#107).
        self._clock = clock
        self._lock = threading.Lock()
        self._next_at = clock()

    def wait_turn(self) -> None:
        with self._lock:
            now = self._clock()
            start_at = max(now, self._next_at)
            self._next_at = start_at + self._throttle.delay
            wait = start_at - now
        if wait > 0:
            self._sleeper(wait)


@dataclass
class LinkEdge:
    source: str
    destination: str
    anchor: str
    # Derived from rel at parse time ("nofollow" in its tokens) and kept as its own field —
    # the convenience every existing caller already reads — regardless of whether the fuller
    # rel/target/raw_href below were captured for this edge.
    nofollow: bool
    # Where the link sits in the DOM (nav/header/sidebar/footer/content/other;
    # see tools/link_position.py). Empty when the crawl did not classify links
    # (the default): storing this per link on a large crawl is a real memory
    # cost, so it is switchable, and leaving it off means the position of every
    # edge is simply unmeasured rather than a false "content" or "".
    position: str = ""
    # The full rel token set, the target attribute, and the href exactly as written before
    # resolution -- gated by link_attributes.capture (default off), the same
    # shape as position above. Measured on a synthetic 3387-page/150-link-per-page crawl
    # (~508k edges, tests/chains/chain_site.py's own fixture is far smaller): with realistic
    # attribute rates (~10-12% of links carry a target or rel, raw_href present on nearly all
    # of them) these three fields add roughly 95 bytes/edge -- about +53% over the ~179
    # bytes/edge this dataclass already costs -- ~46 MiB total for that crawl. That is
    # `raw_href` doing almost all of the damage (a second URL-shaped string per edge); `rel`
    # and `target` alone cost only a few bytes/edge each. Kept as one flag rather than three
    # because a caller wanting cross-origin/protocol-relative detection needs all three
    # together, and splitting them would not change which ones are actually expensive to have
    # both on.
    rel: tuple[str, ...] = ()
    target: str = ""
    raw_href: str = ""


@dataclass
class FormEdge:
    """One ``<form>`` recorded during a crawl (issue #125).

    Unlike ``LinkEdge``'s optional attributes above, this carries no memory-cost caveat:
    forms are rare compared to links -- a handful per page at most -- so there is no
    per-crawl total worth trading off, and it is always recorded.
    """

    page: str
    method: str
    action: str
    has_password: bool


@dataclass
class SpiderResult(CrawlResult):
    links: list[LinkEdge] = field(default_factory=list)
    # Every form found while parsing, regardless of settings -- see FormEdge's own docstring
    # on why this one is unconditional where LinkEdge's rel/target/raw_href are not.
    forms: list[FormEdge] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    max_depth_reached: int = 0
    robots_note: str = ""
    # URLs robots.txt disallows. Under "report_only" they are crawled anyway and
    # listed here, which is what an audit needs: full coverage plus an inventory
    # of what a compliant crawler would not have seen.
    robots_blocked: list[str] = field(default_factory=list)
    crawl_delay_applied: float | None = None
    effective_delay: float = 0.0
    # The adaptive concurrency level reached by the end of the crawl. 1 means
    # the crawl never ran more than one request in flight at a time, whether
    # because it was configured that way or because the origin never earned
    # more.
    effective_concurrency: int = 1
    # Why the checkpoint was or wasn't used, for the run output — see state.py.
    resume_note: str = ""
    # Seed URLs accepted into the frontier beyond the start URL itself (e.g. a
    # sitemap's declared URL set). Recorded so a sitemap-seeded run is
    # auditable: which URLs were fetched only because they were seeded, versus
    # discovered by following a link.
    seed_urls: list[str] = field(default_factory=list)
    # The start page's raw HTML and outlink counts, captured once as a
    # by-product of the ordinary fetch (never an extra request). This is what
    # lets seohead.crawl.render_escalation's pre-flight gate (#18) check for
    # an empty SPA shell or a link-less start page even in "raw" mode, which
    # has no render to fall back on. Empty when the start page was not
    # (re-)fetched in this call, e.g. a resumed run that starts past depth 0.
    start_page_evidence: dict[str, Any] = field(default_factory=dict)


def _canonical_key(url: str) -> str:
    """Identity for de-duplication: fragment dropped, nothing else rewritten."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _strip_fragment(url: str) -> str:
    """The request target for a frontier entry.

    A fragment selects nothing on the server, so keeping it on the queued and
    fetched URL made a fragment-only variant of a page its own PageRecord
    (#194) even though ``_canonical_key`` above already treats the two as one
    identity for de-duplication. Unlike that helper the path is left exactly as
    given -- this value is what actually gets requested, not merely compared --
    which is precisely what ``urldefrag`` does, so it does the work.
    """
    return urldefrag(url).url


def _same_host(url: str, host: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == host


def _fold_failure_streaks(
    record: PageRecord, consecutive_timeouts: int, consecutive_server_errors: int
) -> tuple[int, int]:
    """Advance the two failure streaks by exactly one record, in queue order.

    Mirrors Throttle.record_timeout / record_server_error / record_success —
    the same rules, applied to the *sequence of folded-back records* instead
    of Throttle's live counters. Those counters are mutated inside worker
    threads as each fetch actually completes, which is completion order, not
    queue order; reading them straight from ``after_fetch`` would make the
    circuit breaker's trip point depend on real thread scheduling instead of
    on the deterministic order the rest of the fold-back already uses. An
    exception that carries no origin-health signal at all (``status_code``
    never set, ``error_kind`` empty — a bug in caller code, say) leaves both
    streaks untouched, matching ``fetch_one``: it calls none of Throttle's
    mutators in that case either. ``error_kind`` — not a fresh string check on
    ``record.error`` — is what fetch_one itself used to decide whether to call
    ``record_timeout()``, so reading it back here is reading the same decision,
    not re-deriving a second, possibly divergent one (#132).
    """
    if record.status_code is None:
        return (
            (consecutive_timeouts + 1, consecutive_server_errors)
            if record.error_kind  # "timeout" or "connection" — see PageRecord.error_kind
            else (consecutive_timeouts, consecutive_server_errors)
        )
    if record.status_code == 429 or 500 <= record.status_code < 600:
        return consecutive_timeouts, consecutive_server_errors + 1
    return 0, 0


_PAGE_RECORD_FIELDS = {f.name for f in dataclasses.fields(PageRecord)}


def _read_pages_jsonl(path: str) -> list[PageRecord]:
    """Reconstruct previously fetched pages from a prior run's output.

    Unknown keys are dropped rather than rejected, so a state file written by
    an older build with fewer fields still resumes.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    pages = []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue  # a truncated final line must not discard the rest
        if isinstance(raw, dict):
            pages.append(PageRecord(**{k: v for k, v in raw.items() if k in _PAGE_RECORD_FIELDS}))
    return pages


_LINK_EDGE_FIELDS = {f.name for f in dataclasses.fields(LinkEdge)}


def _write_link(handle, edge: LinkEdge) -> None:
    if handle is None:
        return
    handle.write(json.dumps(dataclasses.asdict(edge), ensure_ascii=False) + "\n")
    handle.flush()


def _write_decision(handle, entry: dict[str, Any]) -> None:
    """Append one structured decision record (issue #134).

    Redaction is reused from ``runlog`` rather than duplicated: a decision log
    is the same kind of artifact — an append-only record of what a run did —
    and must be held to the same "nothing secret in it" rule.
    """
    if handle is None:
        return
    from seohead import runlog

    handle.write(json.dumps(runlog.safe_arguments(entry), ensure_ascii=False) + "\n")
    handle.flush()


def _read_links_jsonl(path: str) -> list[LinkEdge]:
    """Reconstruct the link graph recorded before a checkpoint.

    Edges are appended to this sidecar as they are found (see ``_write_link``)
    rather than embedded in ``crawl_state.json``, the same reasoning that keeps
    pages out of it: ``links`` is the largest structure a long crawl builds up,
    so a resume must not pay to reserialise the whole thing on every checkpoint
    save — only ever appending what is new. Unknown keys are dropped rather
    than rejected, so a file written by an older build still resumes.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return []
    edges = []
    for line in lines:
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue  # a truncated final line must not discard the rest
        if isinstance(raw, dict):
            fields = {k: v for k, v in raw.items() if k in _LINK_EDGE_FIELDS}
            # rel is a tuple in memory but a list once it has been through JSON. Without
            # this, a resumed crawl would hand callers a different type for the same field
            # than an uninterrupted one -- and comparisons against ("nofollow",) would
            # quietly stop matching.
            if "rel" in fields:
                fields["rel"] = tuple(fields["rel"] or ())
            edges.append(LinkEdge(**fields))
    return edges


@dataclass(frozen=True)
class Scope:
    """Which discovered URLs a crawl may fetch.

    The seed is always fetched: a crawl whose own start URL is filtered out
    would report an empty site rather than a configuration mistake. Everything
    reached from it is tested here, and every rejection is counted in
    ``SpiderResult.excluded`` under the rule that rejected it.
    """

    internal: str = "host"
    include_patterns: tuple[re.Pattern[str], ...] = ()
    exclude_patterns: tuple[re.Pattern[str], ...] = ()
    exclude_hosts: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, scope: dict[str, Any] | None) -> Scope:
        scope = scope or {}
        return cls(
            internal=scope.get("internal", "host"),
            include_patterns=tuple(re.compile(p) for p in scope.get("include_patterns") or ()),
            exclude_patterns=tuple(re.compile(p) for p in scope.get("exclude_patterns") or ()),
            exclude_hosts=frozenset(
                host.lower().lstrip(".") for host in scope.get("exclude_hosts") or ()
            ),
        )

    def is_internal(self, url: str, start_host: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return False
        if self.internal == "registrable_domain":
            return registrable_domain(host) == registrable_domain(start_host)
        return host == start_host

    def rejection(self, url: str, start_host: str) -> str:
        """The rule that rejects this URL, or "" when it may be fetched."""
        if not self.is_internal(url, start_host):
            return "outside_host"
        host = (urlsplit(url).hostname or "").lower()
        if any(host == bad or host.endswith("." + bad) for bad in self.exclude_hosts):
            return "excluded_host"
        if any(pattern.search(url) for pattern in self.exclude_patterns):
            return "excluded_by_pattern"
        if self.include_patterns and not any(p.search(url) for p in self.include_patterns):
            return "not_included_by_pattern"
        return ""


def _fetch_robots(
    start: str, fetcher: Callable[[str], Any] | None, client: Any
) -> tuple[dict, str, bool]:
    """Read robots.txt. Returns ``(parsed_or_empty, note, unavailable)``.

    ``unavailable`` is true only when the fetch itself failed (network error),
    the server (or the final hop of a redirect, see below) answered 429/5xx, or a
    redirect could not be trusted — a host that could not say what is
    disallowed, as distinct from one that has nothing to say. A 404 or another
    non-429 4xx robots.txt means "no restrictions" per RFC 9309 and is not
    "unavailable". What
    happens when it is unavailable — stop the crawl, or continue as if
    unrestricted — is ``robots.unavailable_means_stop``, decided by the
    caller: RFC 9309 treats an unavailable robots.txt as a full disallow, and
    the practical reason for the default (stop) is sharper than the standard —
    a host answering 5xx is already failing, and crawling it harder is the
    wrong response.

    ``client`` is the crawl's own content-fetching client, built with
    ``follow_redirects=False`` so a 3xx on a *page* stays visible as a 3xx
    (see ``crawl_site``). robots.txt has no such requirement — nothing reads
    its Location header — so a 3xx here is followed by hand, hop by hop,
    rather than reusing that client's redirect behaviour as-is (which is how
    this function used to read a redirect's own near-always-empty body as the
    whole ruleset: fully permissive, and silently so). Three things bound how
    far "followed" goes, each because trusting it unconditionally reopens the
    same silent-permissive hole from a different angle:

    - hop count: ``MAX_ROBOTS_REDIRECTS`` hops, and a repeated URL is caught
      as a loop before that budget even runs out;
    - host: a redirect off the crawled site's registrable domain is not
      followed — a robots.txt fetched from somebody else's server (accidental
      misconfiguration or deliberate) must not govern this crawl, only its
      own robots.txt may; the crawl's *content* fetches make an equivalent
      call for the exact same reason (``excluded["redirect_off_host"]``);
    - content type: a redirect that resolves to a non-``text/plain`` body
      (an HTML error page, a login wall) is not parsed as robots.txt just
      because ``parse_robots`` will not choke on it — it will simply find no
      ``User-agent:`` lines and return the same empty, permissive ruleset
      this whole function exists to stop producing.

    Every one of those three outcomes is reported as ``unavailable=True``
    with a specific note, so ``unavailable_means_stop`` (and the crawl's
    ``robots_note``) reflect what actually happened rather than reading as an
    ordinary, unrestricted robots.txt.
    """
    parts = urlsplit(start)
    site_domain = registrable_domain(parts.hostname or "")
    url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    visited = {url}
    hops = 0
    redirected = False
    while True:
        try:
            response = fetcher(url) if fetcher else client.get(url)
        except Exception as exc:
            return dict(EMPTY_ROBOTS), f"robots.txt unreachable: {exc}", True
        code = getattr(response, "status_code", None)
        if code is None or not (300 <= code < 400):
            break
        location = (getattr(response, "headers", None) or {}).get("location", "")
        if not location:
            break  # a redirect status with no destination is just the final response
        hops += 1
        if hops > MAX_ROBOTS_REDIRECTS:
            return (
                dict(EMPTY_ROBOTS),
                f"robots.txt redirected more than {MAX_ROBOTS_REDIRECTS} times",
                True,
            )
        next_url = urljoin(url, location)
        if next_url in visited:
            return dict(EMPTY_ROBOTS), f"robots.txt redirect loop at {next_url}", True
        visited.add(next_url)
        next_host = urlsplit(next_url).hostname or ""
        if registrable_domain(next_host) != site_domain:
            return (
                dict(EMPTY_ROBOTS),
                f"robots.txt redirected off-site to {next_url}; not followed",
                True,
            )
        url = next_url
        redirected = True

    if code is not None and (code == 429 or 500 <= code < 600):
        return dict(EMPTY_ROBOTS), f"robots.txt returned {code}", True
    if code is not None and code >= 400:
        return dict(EMPTY_ROBOTS), "no robots.txt", False
    if redirected:
        content_type = (getattr(response, "headers", None) or {}).get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if content_type and media_type != "text/plain":
            return (
                dict(EMPTY_ROBOTS),
                f"robots.txt redirected to non-text/plain content ({media_type})",
                True,
            )
    note = f"robots.txt redirected to {url}" if redirected else ""
    return parse_robots(getattr(response, "text", "") or ""), note, False


def crawl_site(
    start_url: str,
    *,
    max_urls: int = 200,
    max_depth: int = 5,
    max_seconds: float = 0,
    min_delay: float = 0.5,
    timeout: float = 15.0,
    robots_policy: str = "respect",
    scope: dict[str, Any] | Scope | None = None,
    seed_urls: list[str] | None = None,
    out_path: str | None = None,
    links_path: str | None = None,
    decisions_path: str | None = None,
    state_path: str | None = None,
    config_fingerprint: str = "",
    fetcher: Callable[[str], Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    credential_headers: list[dict[str, Any]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    concurrency: int = 1,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
    max_url_length: int = 2000,
    max_query_variants_per_path: int = 5,
    retry_on_timeout: int = 0,
    user_agent: str = "",
    robots_token: str = ROBOTS_TOKEN,
    unavailable_means_stop: bool = True,
    stop_after_consecutive_timeouts: int = STOP_AFTER_CONSECUTIVE_FAILURES,
    max_delay_seconds: float = MAX_DELAY_S,
    follow_nofollow: bool = False,
    classify_links: bool = False,
    link_position_rules: list[dict[str, Any]] | None = None,
    content_area_config: dict[str, Any] | None = None,
    cache: ResponseCache | None = None,
    extra_request_headers: dict[str, str] | None = None,
    adaptive: bool = True,
    store_hyperlinks: bool = True,
    crawl_hyperlinks: bool = True,
    store_external_links: bool = True,
    crawl_redirects: bool = True,
    capture_link_attributes: bool = False,
) -> SpiderResult:
    """Crawl one host breadth-first from ``start_url``, within ``scope``.

    ``cache``, when given, is consulted for every fetch before any delay or dispatch-gate wait
    is applied, so a cache hit costs neither a request nor a throttle slot — see ``fetch_one``.
    ``max_seconds`` is a wall-clock budget for the whole call; 0 means none.
    ``state_path``, when given, checkpoints the frontier, the exclusion tally and
    the query-variant budget there so a later call with the same path and start
    URL resumes instead of restarting — ``config_fingerprint`` is compared too,
    so a scope or limit change since the checkpoint starts fresh rather than
    mixing frontiers built under different rules. ``links_path``, when given
    alongside ``state_path``, is a sidecar that every discovered edge is
    appended to as it is found, and is read back into ``result.links`` on
    resume the same way ``out_path`` rebuilds ``result.pages`` — kept as a
    side file rather than folded into the checkpoint because the link graph is
    the largest thing a long crawl accumulates.
    ``decisions_path``, when given, is a structured, per-URL decision log
    (issue #134): one JSON line per exclusion, naming the URL and the rule
    that rejected it, so a wrong decision is debuggable after the fact rather
    than surviving only as a count in ``result.excluded``. Not resumed or
    replayed on a resumed run — it is a diagnostic trace of one call, not
    state the crawl depends on.
    ``seed_urls``, when given, are additional entry points added to the
    frontier at depth 0 alongside ``start_url`` — a sitemap-seeded crawl mode:
    every declared URL is fetched and its own links are followed, rather than
    treating the sitemap as the final answer. Each seed still goes through
    ``scope`` like any discovered link, and a rejected seed is counted in
    ``excluded`` under the rule that rejected it. Being seeded is not being
    "found by following links": a seed with no inbound edge in ``links`` is
    still reachable only because it was declared, which is what makes orphan
    detection against ``result.links`` honest even in this mode.

    ``concurrency`` is a per-origin ceiling, not a promise: the crawl starts at
    a conservative fan-out and the adaptive throttle widens it toward this
    ceiling only on sustained success, collapsing back to one request in flight
    on the first timeout or server refusal. A dispatch gate paces requests from
    a single shared clock regardless of how many workers are running, so
    ``min_delay`` still means "at least this long between requests to the
    origin" — concurrency only overlaps the *wait* for a response, never the
    rate at which requests go out. Each slice of the frontier is fetched as one
    batch, sized to what the origin has earned, and results are folded back in
    queue order — into ``result.pages``, the circuit breaker, redirect and link
    enqueueing, and the checkpoint — before anything downstream sees them, so
    the output (and the saved frontier, on an early stop) is identical to
    ``concurrency=1`` aside from ``response_time``.

    ``max_url_length`` and ``max_query_variants_per_path`` are enforced at
    enqueue time — a rejected URL is never fetched, and is counted in
    ``excluded`` like any other scope rejection. ``follow_nofollow`` decides
    whether a ``rel=nofollow`` link is still recorded in ``links`` (it always
    is) but also enqueued (only when true).
    ``classify_links`` resolves each recorded ``LinkEdge.position`` (nav,
    header, sidebar, footer, content, other — see ``tools/link_position.py``)
    while the page is already being parsed, at zero extra requests; it is off
    by default because storing a position per link is a real memory cost on a
    large crawl, and with it off every edge's ``position`` is simply ``""``
    (unmeasured), never a guessed value. ``link_position_rules`` overrides the
    default nav/header/sidebar/footer selectors (site-specific menus are
    common); ``content_area_config`` is the same config
    ``content_area.resolve_content_area`` takes, reused here so "content"
    means the same thing it means for word counts.
    ``capture_link_attributes`` copies each recorded ``LinkEdge``'s full rel token set,
    target attribute and raw (pre-resolution) href onto the edge — off by default for the
    same reason ``classify_links`` is: measured on a synthetic multi-thousand-page crawl,
    the three together add roughly 50% to per-edge memory (see ``LinkEdge``'s own
    docstring for the number). With it off, every edge's ``rel``/``target``/``raw_href``
    are simply unmeasured (``()``/``""``/``""``), and unsafe-cross-origin-link and
    protocol-relative-link detection — the two findings that need them — report nothing
    rather than a false clean result. ``nofollow`` is unaffected either way: it is derived
    from rel at parse time regardless of this setting.
    """
    start = normalize_url(start_url)
    host = (urlsplit(start).hostname or "").lower() if start else ""
    # normalize_url is lenient — it turns "not a url" into "https://not a url" —
    # so the host is checked here rather than trusted.
    if not start or not host or " " in host or "." not in host:
        raise ValueError(f"not a crawlable URL: {start_url!r}")
    rules = scope if isinstance(scope, Scope) else Scope.from_config(scope)
    limit = checked_url_budget(max_urls)
    depth_limit = max(0, min(int(max_depth), MAX_DEPTH_CEILING))
    max_concurrency = max(1, min(int(concurrency), MAX_CONCURRENCY_CEILING))
    if state_path:
        crawl_state.ensure_safe_dir(os.path.dirname(os.path.abspath(state_path)) or ".")
    parse_options = (
        {
            "classify_links": True,
            "link_position_rules": link_position_rules,
            "content_area": content_area_config,
        }
        if classify_links
        else None
    )

    result = SpiderResult()
    throttle = Throttle(
        min_delay=min_delay,
        max_delay=max_delay_seconds,
        max_concurrency=max_concurrency,
        adaptive=adaptive,
    )
    excluded: dict[str, int] = {}
    # Distinct query strings already enqueued for a given path, so the Nth+1
    # facet/filter variant on the same path is excluded rather than fetched.
    query_budget: dict[str, set[str]] = {}
    crawl_started = clock()

    def exclude(reason: str, url: str | None = None) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1
        # ``url`` is omitted for a decision that rejects a whole page's worth of
        # links at once (``depth_limit``) rather than one URL: attributing a
        # batch decision to a single URL would be a fabricated record, and a
        # wrong log line is worse than a gap named as one (see ``excluded``,
        # which still counts it).
        if url is not None and decisions_handle is not None:
            _write_decision(
                decisions_handle,
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "type": "exclude",
                    "url": url,
                    "reason": reason,
                    "host": host,
                },
            )

    def extra_rejection(candidate: str) -> str:
        """Checks beyond scope: a URL too long, or a path's query budget spent.

        Mutates ``query_budget`` the moment it runs, reserving a slot for
        ``candidate``'s query string whether or not it is later fetched — so a
        caller must run every check that can still reject the URL for a reason
        unrelated to the budget (nofollow, discovery toggles, scope) first, or
        that slot is spent on a URL that was never going to be dispatched (#193).
        """
        if max_url_length and len(candidate) > max_url_length:
            return "url_too_long"
        if max_query_variants_per_path:
            parts = urlsplit(candidate)
            path_key = parts.path or "/"
            variants = query_budget.setdefault(path_key, set())
            if parts.query not in variants:
                if len(variants) >= max_query_variants_per_path:
                    return "query_variants_limit"
                variants.add(parts.query)
        return ""

    with contextlib.ExitStack() as stack:
        client = None
        if fetcher is None:
            # A crawler must observe redirects, not be moved by them. With
            # follow_redirects on, a 301 is recorded as a 200 carrying the
            # target's title and body, the Location is never seen, and redirect
            # auditing is impossible — the old and new URL become duplicates.
            client, _ = http_client(
                timeout, follow_redirects=False, headers={"User-Agent": user_agent or UA}
            )
            stack.callback(client.close)

        enforce = robots_policy == "respect"
        if robots_policy == "ignore":
            # Not fetched at all, so there is nothing to report either.
            robots = dict(EMPTY_ROBOTS)
            note, unavailable = "robots.txt not fetched (policy: ignore)", False
        else:
            robots, note, unavailable = _fetch_robots(start, fetcher, client)
        result.robots_note = note
        if enforce and unavailable and unavailable_means_stop:
            result.partial = True
            result.stopped_reason = note or "robots.txt unavailable"
            result.finish_reason = "robots_unavailable"
            return result

        # A site asking to be crawled slowly is asking the crawler, not the
        # operator. The configured delay is a floor, never a ceiling on politeness.
        # ``min_delay``'s setter (issue #150) raises ``max_delay`` along with it when
        # the site asks for more than the crawl's own ceiling, so every later clamp
        # into [min_delay, max_delay] keeps honouring this value instead of silently
        # sinking back to a smaller max_delay.
        asked = crawl_delay(robots, robots_token) if robots else None
        if asked and asked > throttle.min_delay:
            throttle.min_delay = asked
            throttle.delay = max(throttle.delay, asked)
            result.crawl_delay_applied = asked

        loaded_state, resume_note = (
            crawl_state.load(state_path, start, config_fingerprint) if state_path else (None, "")
        )
        result.resume_note = resume_note
        result.resumed = loaded_state is not None

        handle = None
        if out_path:
            if loaded_state:
                # Prior pages already live on disk; append rather than replace,
                # and bring them back into this run's evidence.
                result.pages.extend(_read_pages_jsonl(out_path))
            mode = "a" if loaded_state else "w"
            handle = stack.enter_context(open(out_path, mode, encoding="utf-8"))

        links_handle = None
        if links_path:
            if loaded_state:
                # Same move as pages above: prior edges live in the sidecar,
                # not the checkpoint, so bring them back here rather than
                # starting result.links at [].
                result.links.extend(_read_links_jsonl(links_path))
            mode = "a" if loaded_state else "w"
            links_handle = stack.enter_context(open(links_path, mode, encoding="utf-8"))

        # Appended across a resume like links_handle above — a decision recorded
        # before a checkpoint is still a decision this run made — but never read
        # back: nothing here needs to reconstruct prior exclusions in memory.
        decisions_handle = None
        if decisions_path:
            mode = "a" if loaded_state else "w"
            decisions_handle = stack.enter_context(open(decisions_path, mode, encoding="utf-8"))

        if loaded_state:
            queue: deque[tuple[str, int]] = deque(loaded_state.queue)
            seen: set[str] = set(loaded_state.seen)
            result.max_depth_reached = loaded_state.max_depth_reached
            excluded.update(loaded_state.excluded)
            for path_key, variants in loaded_state.query_budget.items():
                query_budget[path_key] = set(variants)
            # Both are produced only for pages fetched in this invocation, so a
            # resumed run would otherwise finish reporting fewer forms than the
            # interrupted one had already found, and would hand the rendering
            # gate an empty start page (issue #188).
            result.forms.extend(FormEdge(**entry) for entry in loaded_state.forms)
            result.start_page_evidence = dict(loaded_state.start_page_evidence)
            # Crawl-wide evidence, not per-invocation data (issue #349): a
            # completed report-only audit needs every blocked URL ever seen,
            # not only ones fetched after this checkpoint.
            result.robots_blocked.extend(loaded_state.robots_blocked)
        else:
            queue = deque([(start, 0)])
            seen = {_canonical_key(start)}

        for seed in seed_urls or []:
            seed = (seed or "").strip()
            if not seed:
                continue
            # Checked against ``seen`` before the rejection rules run, and
            # added to ``seen`` either way (issue #348): a rejected seed is a
            # decision this run made about that URL, exactly like an accepted
            # one, so a resumed retry that re-supplies the same declaration
            # must not re-evaluate and re-count it.
            key = _canonical_key(seed)
            if key in seen:
                continue
            seen.add(key)
            reason = rules.rejection(seed, host) or extra_rejection(seed)
            if reason:
                exclude(reason, seed)
                continue
            queue.append((seed, 0))
            result.seed_urls.append(seed)

        # The circuit breaker's own streaks, advanced only here as records are
        # folded back in queue order — see _fold_failure_streaks.
        consecutive_timeouts = 0
        consecutive_server_errors = 0

        def _extra_headers_for(url: str) -> dict[str, str] | None:
            # Resolved for this hop's own host, never carried over from the
            # last one — that is what keeps a credential off a cross-host
            # redirect target. Called with each URL's own host regardless of
            # concurrency, so nothing is ever carried between hops or workers.
            # http.headers applies to every request; a credential applies only to the host it
            # was bound to. Merging here rather than at client construction keeps both in one
            # place and keeps the per-host resolution the credential rule depends on.
            headers = dict(extra_request_headers or {})
            if credential_headers:
                headers.update(
                    resolve_credential_headers(credential_headers, urlsplit(url).hostname or "")
                    or {}
                )
            return headers or None

        def robots_blocks(url: str) -> bool:
            """True when this URL must not be fetched under the current policy."""
            if robots and not is_allowed(robots, match_path(url), robots_token):
                result.robots_blocked.append(url)
                if enforce:
                    exclude("blocked_by_robots", url)
                    return True
            return False

        def handle_redirect(record: PageRecord, depth: int) -> None:
            # A redirect is a discovery too, and it stays inside the budget — unless
            # discovery.redirects.crawl says a redirect target is not a discovery source,
            # in which case the redirect is still recorded on the page, just never followed.
            if not crawl_redirects:
                return
            if record.redirect_url and depth < depth_limit:
                target = _strip_fragment(record.redirect_url)
                reason = rules.rejection(target, host) or extra_rejection(target)
                if reason:
                    exclude("redirect_off_host" if reason == "outside_host" else reason, target)
                else:
                    key = _canonical_key(target)
                    if key not in seen:
                        seen.add(key)
                        queue.append((target, depth + 1))

        def handle_links(parsed: dict[str, Any] | None, url: str, depth: int) -> None:
            if parsed is None:
                return
            if depth >= depth_limit:
                exclude("depth_limit")
                return
            # Document order, not sorted: a truncated crawl must sample the page
            # as the page is written, not alphabetically.
            for link in parsed.get("links") or []:
                href = (link.get("href") or "").strip()
                if not href:
                    continue
                # The frontier and PageRecord identity never see the fragment — it
                # selects nothing on the server, so a fragment-only variant of a
                # page must not become its own queued request (#194). ``href`` on
                # the stored edge below stays exactly as written, since that is
                # what the page actually links to.
                target = _strip_fragment(href)
                nofollow = bool(link.get("nofollow"))
                # Scope alone: no side effect, unlike extra_rejection below, so it is
                # safe to compute before the nofollow/discovery gates decide whether
                # this link may reach extra_rejection at all.
                scope_reason = rules.rejection(target, host)
                is_external = scope_reason == "outside_host"
                # "store" and "crawl" are independent questions: an edge that will not be
                # enqueued below is still real evidence of what the page links to. Both are
                # on by default; turning a store off makes the report smaller, not different.
                store_this = store_external_links if is_external else store_hyperlinks
                if store_this:
                    edge = LinkEdge(
                        source=url,
                        destination=href,
                        anchor=(link.get("text") or "")[:200],
                        nofollow=nofollow,
                        position=link.get("position") or "",
                    )
                    if capture_link_attributes:
                        edge.rel = tuple((link.get("rel") or "").split())
                        edge.target = link.get("target") or ""
                        edge.raw_href = link.get("raw_href") or ""
                    result.links.append(edge)
                    _write_link(links_handle, edge)
                if not crawl_hyperlinks:
                    exclude("hyperlink_discovery_off", href)
                    continue
                if nofollow and not follow_nofollow:
                    exclude("nofollow", href)
                    continue
                # extra_rejection spends a query-variant slot the instant it runs
                # (see its own docstring above), so it must be the last check before
                # enqueueing — a link already rejected, or one that will never be
                # dispatched because it is nofollow or discovery is off, must never
                # have paid for that slot (#193).
                reason = scope_reason or extra_rejection(target)
                if reason:
                    exclude(reason, href)
                    continue
                key = _canonical_key(target)
                if key in seen:
                    continue
                seen.add(key)
                queue.append((target, depth + 1))

        def handle_forms(parsed: dict[str, Any] | None, url: str) -> None:
            for form in (parsed or {}).get("forms") or []:
                result.forms.append(
                    FormEdge(
                        page=url,
                        method=form.get("method") or "get",
                        action=form.get("action") or "",
                        has_password=bool(form.get("has_password")),
                    )
                )

        def after_fetch(
            url: str, depth: int, record: PageRecord, parsed: dict[str, Any] | None
        ) -> bool:
            """Bookkeeping shared by every fetched page. Returns True to stop the crawl."""
            nonlocal consecutive_timeouts, consecutive_server_errors
            record.crawl_depth = depth
            result.pages.append(record)
            _write(handle, record)
            handle_forms(parsed, url)

            if (
                depth == 0
                and url == start
                and not result.start_page_evidence
                and parsed is not None
            ):
                # Captured once, straight from the ordinary fetch -- no extra
                # request. Used only by the pre-flight rendering gate (#18):
                # an empty SPA shell or a link-less start page must withhold
                # the health score, and both checks are static, so they must
                # not wait for a render that a raw-mode run will never perform.
                result.start_page_evidence = {
                    "html": parsed.get("_raw_html", ""),
                    "outlinks": record.outlinks,
                    "external_outlinks": record.external_outlinks,
                }

            consecutive_timeouts, consecutive_server_errors = _fold_failure_streaks(
                record, consecutive_timeouts, consecutive_server_errors
            )
            if consecutive_timeouts >= stop_after_consecutive_timeouts:
                result.partial = True
                result.stopped_reason = "origin stopped responding (repeated timeouts)"
                result.finish_reason = "errors"
                return True
            if consecutive_server_errors >= STOP_AFTER_CONSECUTIVE_FAILURES:
                # The host has refused repeatedly. Continuing would measure the
                # crawler rather than the site.
                result.partial = True
                result.stopped_reason = "origin refused repeatedly (429/5xx) — crawl stopped"
                result.finish_reason = "errors"
                return True

            handle_redirect(record, depth)
            handle_links(parsed, url, depth)
            return False

        stopped = False
        if max_concurrency <= 1:
            # The plain sequential path: one request, wait for the response,
            # then the next. Kept byte-for-byte separate from the batched path
            # below so the common case (the default) carries zero concurrency
            # overhead and zero risk of it changing behaviour.
            while queue and not stopped:
                if len(result.pages) >= limit:
                    result.partial = True
                    result.stopped_reason = f"url limit reached ({limit})"
                    result.finish_reason = "url_limit"
                    break
                if max_seconds and (clock() - crawl_started) >= max_seconds:
                    result.partial = True
                    result.stopped_reason = f"duration limit reached ({max_seconds:.0f}s)"
                    result.finish_reason = "duration_limit"
                    break
                url, depth = queue.popleft()
                result.max_depth_reached = max(result.max_depth_reached, depth)

                if robots_blocks(url):
                    continue

                try:
                    record, parsed = fetch_one(
                        url,
                        client=client,
                        fetcher=fetcher,
                        throttle=throttle,
                        extra_headers=_extra_headers_for(url),
                        user_agent=user_agent,
                        max_response_bytes=max_response_bytes,
                        retry_on_timeout=retry_on_timeout,
                        parse_options=parse_options,
                        cache=cache,
                        wait=(lambda: sleeper(throttle.delay)) if throttle.delay else None,
                    )
                except KeyboardInterrupt:
                    # Not processed: put it back so a resume retries it rather
                    # than silently dropping it from the frontier.
                    queue.appendleft((url, depth))
                    result.partial = True
                    result.stopped_reason = "interrupted"
                    result.finish_reason = "interrupted"
                    break
                stopped = after_fetch(url, depth, record, parsed)
        else:
            gate = _DispatchGate(throttle, sleeper, clock)

            def dispatch(item: tuple[str, int]) -> tuple[str, int, PageRecord, dict | None]:
                url, depth = item
                # gate.wait_turn is passed as ``wait`` rather than called here directly, so a
                # cache hit — decided inside fetch_one — never claims a dispatch turn it did not
                # need. A hit costs no request, so it must not cost a pacing slot either.
                record, parsed = fetch_one(
                    url,
                    client=client,
                    fetcher=fetcher,
                    throttle=throttle,
                    extra_headers=_extra_headers_for(url),
                    user_agent=user_agent,
                    max_response_bytes=max_response_bytes,
                    retry_on_timeout=retry_on_timeout,
                    parse_options=parse_options,
                    cache=cache,
                    wait=gate.wait_turn,
                )
                return url, depth, record, parsed

            with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
                while queue and not stopped:
                    if len(result.pages) >= limit:
                        result.partial = True
                        result.stopped_reason = f"url limit reached ({limit})"
                        result.finish_reason = "url_limit"
                        break
                    if max_seconds and (clock() - crawl_started) >= max_seconds:
                        result.partial = True
                        result.stopped_reason = f"duration limit reached ({max_seconds:.0f}s)"
                        result.finish_reason = "duration_limit"
                        break

                    # One batch is one slice of the frontier, sized to what the
                    # origin has earned so far — never more than the pool has
                    # workers for, since that is the largest unit that stays
                    # sound to re-sort by discovery order in one pass.
                    batch = [queue.popleft() for _ in range(min(throttle.concurrency, len(queue)))]

                    blocked_depths = []
                    to_fetch = []
                    for u, d in batch:
                        if robots_blocks(u):
                            blocked_depths.append(d)
                        else:
                            to_fetch.append((u, d))

                    # A URL budget ends the crawl at an exact page count,
                    # concurrency or not: anything past the remaining budget
                    # goes back to the front of the queue rather than being
                    # dispatched.
                    budget = limit - len(result.pages)
                    if len(to_fetch) > budget:
                        overflow, to_fetch = to_fetch[budget:], to_fetch[:budget]
                        for item in reversed(overflow):
                            queue.appendleft(item)

                    # Depth bookkeeping matches the sequential walk: it covers
                    # every item popped for good (fetched or robots-blocked),
                    # never an item pushed back to the queue as overflow.
                    for d in blocked_depths:
                        result.max_depth_reached = max(result.max_depth_reached, d)
                    for _, d in to_fetch:
                        result.max_depth_reached = max(result.max_depth_reached, d)

                    if not to_fetch:
                        continue

                    # Futures are submitted up front and then consumed in the
                    # order of ``to_fetch`` regardless of which request
                    # actually finished first, so every downstream step —
                    # recording, the circuit breaker, link and redirect
                    # enqueueing — sees the same order the sequential crawler
                    # would have used. Keeping the futures themselves (rather
                    # than ``pool.map``'s generator) lets a breaker firing
                    # partway through the batch still recover whichever later
                    # requests already finished, instead of discarding them.
                    futures = [pool.submit(dispatch, item) for item in to_fetch]
                    processed = 0
                    interrupted = False
                    try:
                        for future in futures:
                            url, depth, record, parsed = future.result()
                            processed += 1
                            if after_fetch(url, depth, record, parsed):
                                stopped = True
                                break
                    except KeyboardInterrupt:
                        # Some workers in this batch may still be running; the
                        # ones whose result was never consumed here are not
                        # known to be processed, so they (and anything not yet
                        # dispatched) go back to the front of the queue rather
                        # than being dropped from the frontier. A future's
                        # completion is not established here, so it is always
                        # requeued rather than risked on a `.done()` check
                        # that could race the interrupt itself.
                        interrupted = True

                    if stopped and not interrupted:
                        # The breaker fired on an earlier, ordered result, but
                        # later requests in this same batch ran concurrently
                        # and may have already completed. Merge each one that
                        # has — its PageRecord and sidecar write, its forms,
                        # its discovered links — before requeueing anything,
                        # so the checkpoint holds only work that never
                        # resolved rather than repeating requests that already
                        # reached the origin.
                        requeue: list[tuple[str, int]] = []
                        for item, future in zip(
                            to_fetch[processed:], futures[processed:], strict=True
                        ):
                            if future.done():
                                url, depth, record, parsed = future.result()
                                after_fetch(url, depth, record, parsed)
                            else:
                                requeue.append(item)
                        for item in reversed(requeue):
                            queue.appendleft(item)
                    elif interrupted:
                        for item in reversed(to_fetch[processed:]):
                            queue.appendleft(item)
                    if interrupted:
                        result.partial = True
                        result.stopped_reason = "interrupted"
                        result.finish_reason = "interrupted"
                        stopped = True

        if state_path:
            if result.finish_reason == "finished":
                # Nothing left to resume: a later call with the same path
                # should crawl fresh, not "resume" into an empty frontier.
                crawl_state.clear(state_path)
            else:
                # Saved only once the loop above has finished folding the last
                # batch back in, so the frontier on disk is always a complete
                # BFS state, never a snapshot taken mid-batch.
                crawl_state.save(
                    state_path,
                    crawl_state.CrawlState(
                        start_url=start,
                        queue=list(queue),
                        seen=sorted(seen),
                        max_depth_reached=result.max_depth_reached,
                        config_fingerprint=config_fingerprint,
                        excluded=dict(excluded),
                        query_budget={
                            path_key: sorted(variants)
                            for path_key, variants in query_budget.items()
                        },
                        forms=[dataclasses.asdict(form) for form in result.forms],
                        start_page_evidence=dict(result.start_page_evidence),
                        robots_blocked=list(result.robots_blocked),
                    ),
                )

    result.excluded = excluded
    result.effective_delay = throttle.delay
    result.effective_concurrency = throttle.concurrency
    if cache is not None:
        result.cache_stats = dict(cache.stats)
        result.cache_replay = cache.mode == "replay"
    result.limitations = [
        f"scope {rules.internal}: links outside it are recorded, never fetched",
        "static HTML only: no JavaScript rendering",
    ]
    if not seed_urls:
        # The spider itself never fetches a sitemap; a caller expands one and
        # passes the URL set in via seed_urls. When it did, this crawl was
        # sitemap-seeded, so the blanket limitation would be false.
        result.limitations.append("no sitemap expansion")
    return result
