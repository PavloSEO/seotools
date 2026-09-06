"""Bounded native crawl adapter for an explicit SQLite scan artifact.

The adapter deliberately owns no global page, link, frontier, seen, or query
collections.  ``NativeScan`` is the sole writer and source of those facts.
The small per-document batch comes from the shared spider helpers so legacy and
SQLite link semantics cannot drift.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import sqlite3
import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from seohead.crawl.collect import _resolve_redirect_destination, fetch_one
from seohead.crawl.settings import (
    checked_url_budget,
    fingerprint,
    resolve_credential_headers,
)
from seohead.crawl.spider import (
    STOP_AFTER_CONSECUTIVE_FAILURES,
    Scope,
    _DispatchGate,
    _fetch_robots,
    _fold_failure_streaks,
    _strip_fragment,
)
from seohead.crawl.throttle import Throttle
from seohead.models import ParsedRobots
from seohead.recon.net import UA, http_client, normalize_url
from seohead.storage import MAX_RECORD_BYTES, ScanError
from seohead.storage.native_scan import NativeScan
from seohead.tools.robots import crawl_delay as robots_crawl_delay
from seohead.tools.robots import is_allowed, match_path

MAX_LINK_OBSERVATIONS = 20_000
MAX_FORM_OBSERVATIONS = 2_000


def _append_capture(captures, event):
    size = sum(len(item.entity_bytes or b"") for item in captures) + len(event.entity_bytes or b"")
    if len(captures) >= 1000 or size > 8 * MAX_RECORD_BYTES:
        raise ScanError("page response observations exceed the bounded capture unit")
    captures.append(event)


@dataclass(frozen=True)
class ScanRun:
    """Small native-scan outcome; graph evidence remains in the SQLite artifact."""

    path: str
    pages: int
    links: int
    forms: int
    lifecycle: str
    finish_reason: str
    partial: bool
    resumed: bool = False
    audit_available: bool = False
    audit_reason: str = "collection has not run the analyzer"
    limitations: tuple[str, ...] = ()
    start_page_gate: dict[str, Any] | None = None
    corpus_partial: bool = True
    capabilities: dict[str, Any] | None = None


@dataclass
class _DocumentBatch:
    links: list[dict[str, Any]] = field(default_factory=list)
    forms: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    partial_reasons: list[str] = field(default_factory=list)


def _runtime(
    throttle: Throttle,
    *,
    max_depth: int,
    elapsed: float,
    timeouts: int,
    server_errors: int,
    robots_delay: float | None,
) -> dict[str, Any]:
    """The C-owned resume shape; no adapter-side state file exists."""
    return {
        "max_depth_reached": max_depth,
        "elapsed_seconds": elapsed,
        "circuit_timeout_streak": timeouts,
        "circuit_server_error_streak": server_errors,
        "crawl_delay_applied": robots_delay,
        "throttle": throttle.snapshot_state(),
    }


def _headers(settings: dict[str, Any], url: str) -> dict[str, str] | None:
    headers = dict(settings["http"]["headers"])
    headers.update(
        resolve_credential_headers(
            settings["http"]["credential_headers"], urlsplit(url).hostname or ""
        )
        or {}
    )
    return headers or None


def _parse_options(
    settings: dict[str, Any], content_area_config: dict[str, Any] | None
) -> dict[str, Any]:
    """The current spider's parser options plus scan-only bounded observations."""
    options = {
        "classify_links": settings["link_position"]["classify"],
        "link_position_rules": settings["link_position"]["rules"],
        "content_area": content_area_config,
        "max_link_observations": MAX_LINK_OBSERVATIONS,
        "max_form_observations": MAX_FORM_OBSERVATIONS,
    }
    if "resources" in settings:
        options.update(resource_declarations=True, max_resource_declarations=MAX_LINK_OBSERVATIONS)
    return options


def _resource_observations(record, parsed, captures, settings):
    """Describe measured-empty, bounded or unavailable declaration extraction."""
    if "resources" not in settings or not record.is_html:
        return {}
    values, omitted = [], 0
    if parsed is not None and "resource_declarations" in parsed:
        values = parsed["resource_declarations"]
        omitted = parsed["resource_declarations_omitted"]
        state = "partial" if omitted else "complete"
    elif any(event.requested_url == record.url and event.entity_bytes == b"" for event in captures):
        state = "complete"
    else:
        state = "unavailable"
    return {"resources": values, "resource_inventory_state": state, "resources_omitted": omitted}


@contextmanager
def _client_context(
    settings: dict[str, Any], fetcher: Callable[[str], Any] | None
) -> Iterator[Any]:
    """The same guarded, no-follow client used by the legacy spider."""
    if fetcher is not None:
        yield None
        return
    client, _http2 = http_client(
        settings["http"]["timeout_seconds"],
        follow_redirects=False,
        headers={"User-Agent": settings["http"]["user_agent"] or UA},
    )
    try:
        yield client
    finally:
        client.close()


def _restore_runtime(
    throttle: Throttle, snapshot: dict[str, Any]
) -> tuple[int, float, int, int, float | None]:
    """Use the shared Throttle restore API; never re-create hidden counters here."""
    runtime = snapshot["runtime"]
    throttle.restore_state(runtime["throttle"])
    return (
        runtime["max_depth_reached"],
        runtime["elapsed_seconds"],
        runtime["circuit_timeout_streak"],
        runtime["circuit_server_error_streak"],
        runtime["crawl_delay_applied"],
    )


def _storage_failure(scan: NativeScan, exc: Exception) -> None:
    """Leave a resumable marker when SQLite rejects an otherwise valid unit."""
    reason = f"storage_error:{type(exc).__name__}:{str(exc)[:160]}"
    with suppress(Exception):
        scan.interrupt(reason)
    # Disk-full can prevent even the best-effort lifecycle update.  The caller
    # still gets the original error and C's last committed unit is left untouched.


def _document_batch(
    parsed: dict[str, Any] | None,
    *,
    source_url: str,
    depth: int,
    scope: Scope,
    start_host: str,
    settings: dict[str, Any],
) -> _DocumentBatch:
    """Use the shared helper; this module never reparses HTML or recreates links."""
    if parsed is None:
        return _DocumentBatch()
    try:
        from seohead.crawl.spider import apply_document_links, form_edges
    except ImportError as exc:  # integration order guard until shared helper merges
        raise RuntimeError("SQLite adapter requires shared spider document helpers") from exc

    batch = _DocumentBatch()

    def record_edge(edge: Any) -> None:
        batch.links.append(dataclasses.asdict(edge))

    def reject(reason: str, url: str | None) -> None:
        if url is not None:
            batch.decisions.append(
                {"url": url, "reason": reason, "source": source_url, "depth": depth + 1}
            )

    def discover(target: str, next_depth: int, requested_url: str) -> str | None:
        if (
            settings["limits"]["max_url_length"]
            and len(target) > settings["limits"]["max_url_length"]
        ):
            return "url_too_long"
        parts = urlsplit(target)
        batch.candidates.append(
            {
                "path_key": parts.path or "/",
                "query_key": parts.query,
                "requested_url": requested_url,
                "frontier_url": target,
                "depth": next_depth,
            }
        )
        # C resolves query reservation and known-frontier identity atomically.
        return None

    def mark_partial(omitted: int) -> None:
        if omitted:
            batch.partial_reasons.append("link_observations_omitted")

    apply_document_links(
        parsed,
        source_url,
        depth,
        depth_limit=settings["limits"]["max_depth"],
        host=start_host,
        rejection=scope.rejection,
        discover=discover,
        store_hyperlinks=settings["discovery"]["hyperlinks"]["store"],
        store_external_links=settings["discovery"]["external"]["store"],
        crawl_hyperlinks=settings["discovery"]["hyperlinks"]["crawl"],
        follow_nofollow=settings["discovery"]["follow_nofollow"],
        capture_link_attributes=settings["link_attributes"]["capture"],
        record_edge=record_edge,
        reject=reject,
        mark_link_partial=mark_partial,
    )
    forms, omitted = form_edges(parsed, source_url)
    batch.forms.extend(dataclasses.asdict(form) for form in forms)
    if omitted:
        batch.partial_reasons.append("form_observations_omitted")
    return batch


def _forms_only_batch(parsed: dict[str, Any] | None, source_url: str) -> _DocumentBatch:
    """Keep legacy's forms-before-circuit behavior without discovering links."""
    from seohead.crawl.spider import form_edges

    batch = _DocumentBatch()
    forms, omitted = form_edges(parsed, source_url)
    batch.forms.extend(dataclasses.asdict(form) for form in forms)
    if omitted:
        batch.partial_reasons.append("form_observations_omitted")
    return batch


def _redirect_discovery(
    batch: _DocumentBatch,
    record: Any,
    *,
    depth: int,
    scope: Scope,
    host: str,
    settings: dict[str, Any],
) -> None:
    """Apply the legacy redirect-discovery order before ordinary page links."""
    if (
        not settings["discovery"]["redirects"]["crawl"]
        or not record.redirect_url
        or depth >= settings["limits"]["max_depth"]
    ):
        return
    target = _strip_fragment(record.redirect_url)
    reason = scope.rejection(target, host)
    if reason:
        batch.decisions.append(
            {
                "url": target,
                "reason": "redirect_off_host" if reason == "outside_host" else reason,
                "source": record.url,
                "depth": depth + 1,
            }
        )
        return
    if settings["limits"]["max_url_length"] and len(target) > settings["limits"]["max_url_length"]:
        batch.decisions.append(
            {
                "url": target,
                "reason": "url_too_long",
                "source": record.url,
                "depth": depth + 1,
            }
        )
        return
    parts = urlsplit(target)
    batch.candidates.append(
        {
            "path_key": parts.path or "/",
            "query_key": parts.query,
            "requested_url": target,
            "frontier_url": target,
            "depth": depth + 1,
        }
    )


def crawl_to_scan(
    start_url: str,
    *,
    scan_out: str,
    settings: dict[str, Any],
    producer_version: str,
    producer_revision: str,
    runtime_versions: dict[str, str],
    seed_urls: Iterable[str] = (),
    initial_sitemaps: tuple[tuple[str, str], ...] = (),
    seed_loader: Callable[[NativeScan, Callable[[Iterable[str]], None]], None] | None = None,
    content_area_config: dict[str, Any] | None = None,
    fetcher: Callable[[str], Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ScanRun:
    """Collect a cache-off native crawl into one explicit scan artifact.

    Handler/CLI/MCP wiring is deliberately outside this module.  Callers pass
    the already loaded settings and producer provenance, so there is no second
    configuration path here.
    """
    if settings["cache"]["mode"] != "off":
        raise ValueError(
            "SQLite scan collection requires cache.mode=off; its artifact owns retained bodies"
        )
    limit = checked_url_budget(settings["limits"]["max_urls"])
    start = normalize_url(start_url)
    host = (urlsplit(start).hostname or "").lower()
    if not host:
        raise ValueError("SQLite scan collection requires a crawlable start URL")
    scope = Scope.from_config(settings["scope"])
    throttle = Throttle(
        min_delay=settings["speed"]["min_delay_seconds"],
        max_delay=settings["speed"]["max_delay_seconds"],
        max_concurrency=settings["speed"]["concurrency"],
        adaptive=settings["speed"]["adaptive"],
    )
    started = clock()
    timeouts = server_errors = max_depth = 0
    elapsed_before = 0.0
    robots_delay = None
    partial = False
    finish_reason = "finished"
    limitations = ["offline reanalysis and browser-network response capture are unavailable"]
    start_page_gate: dict[str, Any] | None = None

    existing = Path(scan_out).exists()
    scan_context = (
        NativeScan.open(
            scan_out,
            expected_start_url=start,
            expected_config=settings,
            expected_writer_revision=producer_revision,
        )
        if existing
        else NativeScan.create(
            scan_out,
            start_url=start,
            config=settings,
            config_fingerprint=fingerprint(settings),
            writer_version=producer_version,
            writer_revision=producer_revision,
            runtime_versions=runtime_versions,
            limitations=limitations,
            initial_sitemaps=initial_sitemaps,
        )
    )
    with _client_context(settings, fetcher) as client, scan_context as scan:
        scan.preflight_capture()
        snapshot = scan.resume_snapshot()
        seeded = (
            existing
            and json.loads(snapshot["scan"]["capabilities_json"])["resume"]["state"] == "complete"
        )
        selected_roots = scan.sitemap_roots() if seed_loader is not None else []
        if any(
            not (
                scan.read_context("sitemap_fetch_summary", f"url:{root['sitemap_url_id']}") or {}
            ).get("complete")
            for root in selected_roots
        ):
            seeded = False
        if existing and initial_sitemaps:
            expected = [url for url, _source in initial_sitemaps]
            selected = [root["url"] for root in selected_roots if root["source"] == "explicit"]
            if expected != selected:
                raise ScanError("selected sitemap inputs changed; refusing unsafe resume")
        discovery_needs_robots = (
            seed_loader is not None and settings["sitemaps"]["auto_discover"] and not selected_roots
        )
        robots_policy = settings["robots"]["policy"]
        robots_token = settings["robots"]["user_agent_token"]
        saved_robots = scan.read_context("robots_summary") if existing else None
        if saved_robots is not None:
            if not isinstance(saved_robots, dict):
                raise ScanError("resumable native scan is missing its robots summary")
            if (
                saved_robots.get("policy") != robots_policy
                or saved_robots.get("token") != robots_token
            ):
                raise ScanError(
                    "resumable native scan robots policy differs from its stored context"
                )
            robots = saved_robots.get("parsed")
            robots_note = saved_robots.get("note")
            robots_state = saved_robots.get("fetch_state")
            if not isinstance(robots, dict) or not isinstance(robots_note, str):
                raise ScanError("resumable native scan robots context is invalid")
            robots_unavailable = robots_state == "unavailable"
        elif seeded:
            raise ScanError("resumable native scan is missing its robots summary")
        elif robots_policy == "ignore" and not discovery_needs_robots:
            robots, robots_note, robots_unavailable = (
                {"groups": [], "sitemaps": []},
                "robots.txt not fetched (policy: ignore)",
                False,
            )
            robots_state = "not_fetched"
        else:
            robots, robots_note, robots_unavailable = _fetch_robots(start, fetcher, client)
            robots_state = "unavailable" if robots_unavailable else "fetched"
        if saved_robots is None:
            scan.write_context(
                [
                    {
                        "kind": "robots_summary",
                        "item_key": "run",
                        "payload_version": "scan_context.v1",
                        "payload_json": json.dumps(
                            {
                                "policy": robots_policy,
                                "token": robots_token,
                                "fetch_state": robots_state,
                                "final_response_id": None,
                                "note": robots_note,
                                "parsed": robots,
                            },
                            sort_keys=True,
                        ),
                        "completeness": "partial" if robots_unavailable else "complete",
                        "reason": robots_note,
                    }
                ]
            )
        if (
            robots_policy == "respect"
            and robots_unavailable
            and settings["robots"]["unavailable_means_stop"]
        ):
            scan.interrupt(robots_note or "robots.txt unavailable")
            outcome = scan.resume_snapshot(include_edges=True)
            return ScanRun(
                str(scan.path),
                outcome["counts"]["pages"],
                outcome["counts"]["links"],
                outcome["counts"]["forms"],
                outcome["scan"]["lifecycle"],
                "robots_unavailable",
                True,
                resumed=existing,
                limitations=tuple(json.loads(outcome["scan"]["limitations_json"])),
                corpus_partial=bool(outcome["scan"]["corpus_partial"]),
                capabilities=json.loads(outcome["scan"]["capabilities_json"]),
            )
        asked_delay = robots_crawl_delay(cast(ParsedRobots, robots), robots_token)
        if asked_delay and asked_delay > throttle.min_delay:
            throttle.min_delay = asked_delay
            throttle.delay = max(throttle.delay, asked_delay)
            robots_delay = asked_delay
        if existing:
            snapshot = scan.resume_snapshot()
            max_depth, elapsed_before, timeouts, server_errors, robots_delay = _restore_runtime(
                throttle, snapshot
            )
            scan.recover_inflight()
        if not seeded:
            # Initial input chunks are replayed until the durable phase is complete.
            # D depends on C's atomic seed API: supply in chunks so sitemap expansion
            # never becomes a second Python frontier.  Until that API is integrated,
            # this intentionally fails instead of falling back to a deque/set.
            scan.seed_frontier(
                [
                    {
                        "requested_url": start,
                        "frontier_url": start,
                        "depth": 0,
                        "reason": "",
                        "source": "start",
                        "reserve_query": False,
                        "seed": False,
                    }
                ]
            )

            # Existing seed handling preserves the supplied request spelling; C
            # performs canonical membership internally without altering evidence.
            def seed_entry(url: str) -> dict[str, Any]:
                requested = url.strip()
                reason = scope.rejection(requested, host)
                if (
                    not reason
                    and settings["limits"]["max_url_length"]
                    and len(requested) > settings["limits"]["max_url_length"]
                ):
                    reason = "url_too_long"
                return {
                    "requested_url": requested,
                    "frontier_url": requested,
                    "depth": 0,
                    "reason": reason,
                    "source": "sitemap",
                    "reserve_query": True,
                    "seed": True,
                }

            def emit_seeds(urls: Iterable[str]) -> None:
                seed_iter = (seed_entry(url) for url in urls if url and url.strip())
                while chunk := list(itertools.islice(seed_iter, 256)):
                    scan.seed_frontier(chunk)

            if seed_loader is None:
                emit_seeds(seed_urls)
            else:
                seed_loader(scan, emit_seeds)

        frontier_state = scan.resume_snapshot()["counts"]
        if frontier_state.get("queued", 0) or frontier_state.get("inflight", 0):
            scan.begin_collection()

        dispatch_gate = _DispatchGate(throttle, sleeper, clock)
        while True:
            snapshot = scan.resume_snapshot()
            counts = snapshot["counts"]
            if counts["pages"] >= limit:
                if counts["queued"] or counts["inflight"]:
                    partial, finish_reason = True, "url_limit"
                    scan.interrupt(f"url limit reached ({limit})")
                break
            if (
                settings["limits"]["max_crawl_seconds"]
                and elapsed_before + clock() - started >= settings["limits"]["max_crawl_seconds"]
            ):
                partial, finish_reason = True, "duration_limit"
                scan.interrupt("duration limit reached")
                break
            remaining = limit - counts["pages"]
            scan.preflight_capture()
            leases = scan.claim(min(throttle.concurrency, remaining))
            if not leases:
                # Handler owns audit/no-audit finalization after its bounded
                # compatibility bridge decides whether it may materialize.
                break

            parse_options = _parse_options(settings, content_area_config)

            def fetch(
                lease: Any,
                *,
                gate: _DispatchGate = dispatch_gate,
                options: dict[str, Any] = parse_options,
            ) -> Any:
                captures = []
                captured_bytes = 0

                def observe(observation):
                    nonlocal captured_bytes
                    captured_bytes += (
                        len(observation.entity_bytes) if observation.entity_bytes is not None else 0
                    )
                    if len(captures) >= 1000 or captured_bytes > 8 * MAX_RECORD_BYTES:
                        raise ScanError(
                            "page response observations exceed the bounded capture unit"
                        )
                    captures.append(observation)

                capture_options = {}
                if "storage" in settings:
                    capture_options = {
                        "capture_observer": observe,
                        "capture_max_bytes": min(
                            8 * MAX_RECORD_BYTES,
                            max(
                                settings["limits"]["max_response_bytes"],
                                settings["storage"]["max_body_bytes"]
                                if settings["storage"]["body_mode"] != "off"
                                else 0,
                            ),
                        ),
                    }
                result = fetch_one(
                    lease.url,
                    client=client,
                    fetcher=fetcher,
                    throttle=throttle,
                    extra_headers=_headers(settings, lease.url),
                    user_agent=settings["http"]["user_agent"],
                    max_response_bytes=settings["limits"]["max_response_bytes"],
                    retry_on_timeout=settings["http"]["retry_on_timeout"],
                    parse_options=options,
                    cache=None,
                    wait=gate.wait_turn,
                    **capture_options,
                )
                return lease, result, captures

            actions: list[tuple[Any, bool]] = []
            robots_context: dict[int, list[dict[str, str]]] = {}
            for lease in leases:
                blocked = robots_policy in {"respect", "report_only"} and not is_allowed(
                    cast(ParsedRobots, robots), match_path(lease.url), robots_token
                )
                if blocked:
                    robots_context[lease.queue_ordinal] = [
                        {
                            "kind": "robots_blocked_url",
                            "item_key": f"url:{lease.url_id}",
                            "payload_version": "scan_context.v1",
                            "payload_json": json.dumps(
                                {
                                    "url_id": lease.url_id,
                                    "token": robots_token,
                                    "policy": "report_only",
                                },
                                sort_keys=True,
                            ),
                            "completeness": "complete",
                            "reason": "robots.txt",
                        }
                    ]
                actions.append((lease, blocked and robots_policy == "respect"))

            fetchable = [lease for lease, excluded in actions if not excluded]
            with ThreadPoolExecutor(max_workers=max(1, len(fetchable))) as pool:
                # Futures are consumed in claim order: the C writer's contiguous
                # inflight-prefix rule then gives deterministic evidence order.
                futures = {lease.queue_ordinal: pool.submit(fetch, lease) for lease in fetchable}
                for lease, excluded in actions:
                    if excluded:
                        max_depth = max(max_depth, lease.depth)
                        try:
                            scan.exclude_lease(
                                lease,
                                "blocked_by_robots",
                                runtime=_runtime(
                                    throttle,
                                    max_depth=max_depth,
                                    elapsed=elapsed_before + clock() - started,
                                    timeouts=timeouts,
                                    server_errors=server_errors,
                                    robots_delay=robots_delay,
                                ),
                            )
                        except (ScanError, sqlite3.Error) as exc:
                            _storage_failure(scan, exc)
                            raise ScanError(f"native scan storage failure: {exc}") from exc
                        continue
                    try:
                        lease, (record, parsed), captures = futures[lease.queue_ordinal].result()
                    except ScanError as exc:
                        _storage_failure(scan, exc)
                        raise
                    record.crawl_depth = lease.depth
                    max_depth = max(max_depth, lease.depth)
                    if (
                        settings["discovery"]["resolve_redirect_destination"]
                        and record.redirect_url
                    ):
                        _resolve_redirect_destination(
                            record,
                            client=client,
                            fetcher=fetcher,
                            throttle=throttle,
                            extra_headers=_headers(settings, lease.url),
                            user_agent=settings["http"]["user_agent"],
                            max_response_bytes=settings["limits"]["max_response_bytes"],
                            retry_on_timeout=settings["http"]["retry_on_timeout"],
                            parse_options=parse_options,
                            cache=None,
                            sleeper=sleeper,
                            capture_observer=(
                                lambda observation, captured=captures: _append_capture(
                                    captured, observation
                                )
                            )
                            if "storage" in settings
                            else None,
                            capture_max_bytes=min(
                                8 * MAX_RECORD_BYTES,
                                max(
                                    settings["limits"]["max_response_bytes"],
                                    settings.get("storage", {}).get("max_body_bytes", 0),
                                ),
                            ),
                            headers_for_url=lambda target: _headers(settings, target),
                        )
                    if (
                        start_page_gate is None
                        and lease.depth == 0
                        and lease.url == start
                        and parsed is not None
                    ):
                        start_page_gate = {
                            "html": parsed.get("_raw_html", ""),
                            "outlinks": record.outlinks,
                            "external_outlinks": record.external_outlinks,
                        }
                    timeouts, server_errors = _fold_failure_streaks(record, timeouts, server_errors)
                    circuit_stopped = (
                        timeouts >= settings["speed"]["stop_after_consecutive_timeouts"]
                        or server_errors >= STOP_AFTER_CONSECUTIVE_FAILURES
                    )
                    if circuit_stopped:
                        batch = _forms_only_batch(parsed, lease.url)
                    else:
                        batch = _DocumentBatch()
                        _redirect_discovery(
                            batch,
                            record,
                            depth=lease.depth,
                            scope=scope,
                            host=host,
                            settings=settings,
                        )
                        links_batch = _document_batch(
                            parsed,
                            source_url=lease.url,
                            depth=lease.depth,
                            scope=scope,
                            start_host=host,
                            settings=settings,
                        )
                        batch.links.extend(links_batch.links)
                        batch.forms.extend(links_batch.forms)
                        batch.decisions.extend(links_batch.decisions)
                        batch.candidates.extend(links_batch.candidates)
                        batch.partial_reasons.extend(links_batch.partial_reasons)
                    try:
                        if (
                            record.is_html
                            and parsed is None
                            and any(
                                event.body_state in {"truncated", "unavailable"}
                                and event.body_reason in {"truncated", "fetch_failed"}
                                for event in captures
                            )
                        ):
                            batch.partial_reasons.append(
                                "response_body_unavailable: HTML could not be decoded completely"
                            )
                        resource_observations = _resource_observations(
                            record, parsed, captures, settings
                        )
                        if resource_observations.get("resources_omitted"):
                            batch.partial_reasons.append("resource_declarations_omitted")
                        scan.commit_page(
                            lease,
                            dataclasses.asdict(record),
                            links=batch.links,
                            forms=batch.forms,
                            decisions=batch.decisions,
                            candidates=batch.candidates,
                            runtime=_runtime(
                                throttle,
                                max_depth=max_depth,
                                elapsed=elapsed_before + clock() - started,
                                timeouts=timeouts,
                                server_errors=server_errors,
                                robots_delay=robots_delay,
                            ),
                            partial_reasons=tuple(batch.partial_reasons),
                            context=robots_context.get(lease.queue_ordinal, ()),
                            captures=captures,
                            **resource_observations,
                        )
                    except (ScanError, sqlite3.Error) as exc:
                        _storage_failure(scan, exc)
                        raise ScanError(f"native scan storage failure: {exc}") from exc
                    if circuit_stopped:
                        partial, finish_reason = True, "errors"
                        scan.interrupt("origin stopped responding or refused repeatedly")
                        break
                if partial:
                    break

        if finish_reason != "errors":
            from .sqlite_resources import capture_resources

            capture_resources(
                scan, settings, client=client, fetcher=fetcher, clock=clock, sleeper=sleeper
            )
        if start_page_gate is None:
            start_page_gate = retained_start_gate(scan, settings, content_area_config)
        outcome = scan.resume_snapshot(include_edges=True)
        return ScanRun(
            path=str(scan.path),
            pages=outcome["counts"]["pages"],
            links=outcome["counts"]["links"],
            forms=outcome["counts"]["forms"],
            lifecycle=outcome["scan"]["lifecycle"],
            finish_reason=finish_reason,
            partial=partial or outcome["scan"]["crawl_partial"] == 1,
            resumed=existing,
            limitations=tuple(json.loads(outcome["scan"]["limitations_json"])),
            start_page_gate=start_page_gate,
            corpus_partial=bool(outcome["scan"]["corpus_partial"]),
            capabilities=json.loads(outcome["scan"]["capabilities_json"]),
        )


def retained_start_gate(scan, settings, content_area_config=None):
    """Reconstruct the raw start-page gate from its static document, one body at a time."""
    from seohead.crawl.collect import PageRecord, _apply_body
    from seohead.storage.bodies import read_document

    start = scan.con.execute("SELECT start_url FROM scan").fetchone()[0]
    row = scan.con.execute(
        "SELECT d.document_id,r.content_type,r.effective_url_id,u.url AS final_url,b.decoded_bytes "
        "FROM documents d JOIN urls logical ON logical.url_id=d.url_id "
        "JOIN responses r ON r.response_id=d.source_response_id "
        "JOIN bodies b ON b.sha256=d.body_sha256 "
        "LEFT JOIN urls u ON u.url_id=r.effective_url_id "
        "WHERE logical.url=? AND d.representation='static' AND d.body_state='complete' "
        "ORDER BY d.document_id DESC LIMIT 1",
        (start,),
    ).fetchone()
    if row is None:
        return None
    html = read_document(scan.con, row["document_id"], max_decoded_bytes=8 * MAX_RECORD_BYTES)
    record = PageRecord(url=start, content_type=row["content_type"])
    parsed = _apply_body(
        record,
        row["final_url"] or start,
        html,
        parse_options=_parse_options(settings, content_area_config),
        max_response_bytes=settings["limits"]["max_response_bytes"],
        size_bytes=row["decoded_bytes"],
    )
    if parsed is None:
        return None
    return {
        "html": html,
        "outlinks": record.outlinks,
        "external_outlinks": record.external_outlinks,
    }
