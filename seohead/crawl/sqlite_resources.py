"""Execute the declared resource lane with SQL membership and finite request units."""

from __future__ import annotations

import sqlite3
import time
from contextlib import nullcontext

from seohead.crawl.resource_fetch import ResourceFetchResult, ResourceStop, fetch_resource
from seohead.crawl.spider import _DispatchGate
from seohead.crawl.sqlite_adapter import _client_context, _storage_failure
from seohead.crawl.throttle import Throttle
from seohead.storage import ScanError
from seohead.storage.credential_context import credential_verifier
from seohead.storage.resource_capture import commit_resource, request_count
from seohead.tools.robots import is_allowed, match_path


def capture_resources(
    scan, settings, *, client=None, fetcher=None, clock=time.monotonic, sleeper=time.sleep
):
    """Resolve resource references without making them crawl frontier entries."""
    if not settings.get("resources", {}).get("fetch"):
        return
    snapshot = scan.resume_snapshot()
    start_url = snapshot["scan"]["start_url"]
    recorded_credentials = scan.read_context("credential_context")
    if recorded_credentials is not None and recorded_credentials["verifier"] != credential_verifier(
        settings, snapshot["scan"]["scan_uuid"]
    ):
        raise ScanError("credential context changed before resource collection")
    missing_session = bool(
        recorded_credentials
        and recorded_credentials["implicit_state"]
        and client is None
        and fetcher is None
    )
    robots = scan.read_context("robots_summary")
    policy = settings["robots"]["policy"]
    token = settings["robots"]["user_agent_token"]
    throttle = Throttle(
        min_delay=settings["speed"]["min_delay_seconds"],
        max_delay=settings["speed"]["max_delay_seconds"],
        max_concurrency=settings["speed"]["concurrency"],
        adaptive=settings["speed"]["adaptive"],
    )
    throttle.restore_state(snapshot["runtime"]["throttle"])
    delay = snapshot["runtime"]["crawl_delay_applied"]
    if delay:
        throttle.min_delay = max(throttle.min_delay, delay)
        throttle.delay = max(throttle.delay, delay)
    gate = _DispatchGate(throttle, sleeper, clock)
    began = clock()
    elapsed_before = snapshot["runtime"]["elapsed_seconds"]
    duration = settings["limits"]["max_crawl_seconds"]
    used = request_count(scan.con)

    def allowed(url):
        if policy != "respect":
            return True
        return bool(
            robots
            and robots["fetch_state"] in {"fetched", "not_fetched"}
            and is_allowed(robots["parsed"], match_path(url), token)
        )

    def preflight():
        if duration and elapsed_before + clock() - began >= duration:
            raise ResourceStop("total crawl duration exhausted")
        scan.preflight_capture()

    context = nullcontext(client) if client is not None else _client_context(settings, fetcher)
    with context as active_client:
        while True:
            candidate = scan.con.execute(
                "SELECT r.resource_url_id,r.kind,u.url FROM resource_refs r "
                "JOIN urls u ON u.url_id=r.resource_url_id WHERE r.capture_state='not_fetched' "
                "ORDER BY r.resource_ref_id LIMIT 1"
            ).fetchone()
            if candidate is None:
                break
            url_id, kind, url = candidate
            previous = scan.con.execute(
                "SELECT response_id,body_state,body_reason,effective_status_code FROM responses "
                "WHERE request_url_id=? AND purpose=? ORDER BY request_ordinal DESC LIMIT 1",
                (url_id, kind),
            ).fetchone()
            existing_id = None
            if previous is not None:
                existing_id = previous[0]
                if previous[3] is None or not 200 <= previous[3] < 300:
                    state, reason = "fetch_failed", "resource response was not successful"
                elif previous[1] == "complete":
                    state, reason = "measured", ""
                else:
                    state, reason = "body_unavailable", previous[2]
                outcome = ResourceFetchResult((), state, reason, 0)
            elif missing_session:
                outcome = ResourceFetchResult(
                    (), "excluded_scope", "implicit credential session is unavailable", 0
                )
            elif used >= settings["resources"]["max_requests"] or (
                duration and elapsed_before + clock() - began >= duration
            ):
                outcome = ResourceFetchResult(
                    (),
                    "resource_budget_exhausted",
                    "total crawl duration exhausted"
                    if duration and elapsed_before + clock() - began >= duration
                    else "resource request budget exhausted",
                    0,
                )
            else:
                try:
                    outcome = fetch_resource(
                        url,
                        kind,
                        settings=settings,
                        client=active_client,
                        throttle=throttle,
                        origin_url=start_url,
                        robots_allowed=allowed,
                        wait=gate.wait_turn,
                        remaining_requests=settings["resources"]["max_requests"] - used,
                        before_request=preflight,
                        fetcher=fetcher,
                    )
                except (ScanError, sqlite3.Error) as exc:
                    _storage_failure(scan, exc)
                    raise
            try:
                commit_resource(
                    scan,
                    url_id,
                    kind,
                    outcome,
                    elapsed_seconds=elapsed_before + clock() - began,
                    existing_response_id=existing_id,
                    throttle_state=throttle.snapshot_state(),
                )
            except (ScanError, sqlite3.Error) as exc:
                _storage_failure(scan, exc)
                raise
            used += outcome.requests_used
