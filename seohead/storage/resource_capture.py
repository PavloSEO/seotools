"""One atomic resource-response/reference update under the native scan owner."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from seohead.crawl.capture import CaptureEvent
from seohead.crawl.resource_fetch import ResourceFetchResult

from . import MAX_RECORD_BYTES, ScanError, _dump, _insert
from .corpus import store_response


def request_count(con) -> int:
    """Rebuild the bounded scalar HTTP-attempt budget from committed units."""
    return sum(
        json.loads(row[0])["requests_used"]
        for row in con.execute(
            "SELECT payload_json FROM context_items WHERE kind='resource_commit'"
        )
    )


def commit_resource(
    scan,
    url_id: int,
    kind: str,
    outcome: ResourceFetchResult,
    *,
    elapsed_seconds: float,
    existing_response_id: int | None = None,
    throttle_state: dict | None = None,
) -> bool:
    """Commit observations, all unresolved references, time and revision together."""
    from .native_scan import _digest, _json_chunks

    scan._assert_mutable()
    if (
        type(url_id) is not int
        or kind not in {"script", "stylesheet"}
        or not isinstance(outcome, ResourceFetchResult)
    ):
        raise ScanError("invalid resource capture identity or result")
    allowed = {
        "measured",
        "excluded_scope",
        "excluded_robots",
        "resource_budget_exhausted",
        "fetch_failed",
        "body_unavailable",
        "body_budget_exhausted",
    }
    if outcome.capture_state not in allowed or type(outcome.reason) is not str:
        raise ScanError("invalid resource capture state")
    if type(outcome.requests_used) is not int or outcome.requests_used < 0:
        raise ScanError("invalid resource request-attempt count")
    url = scan.con.execute("SELECT url FROM urls WHERE url_id=?", (url_id,)).fetchone()
    if url is None:
        raise ScanError("unknown declared resource URL")
    metadata, byte_count = [], 0
    for event in outcome.captures:
        if not isinstance(event, CaptureEvent) or len(metadata) >= 1000:
            raise ScanError("resource capture exceeds its bounded observation unit")
        value = asdict(event)
        body = value.pop("entity_bytes")
        byte_count += len(body or b"")
        value["entity_sha256"] = hashlib.sha256(body).hexdigest() if body is not None else None
        metadata.append(value)
    if byte_count > 8 * MAX_RECORD_BYTES:
        raise ScanError("resource response unit exceeds 64 MiB")
    for _ in _json_chunks(metadata):
        pass
    config = json.loads(scan.con.execute("SELECT config_json FROM scan").fetchone()[0])
    if not config.get("resources", {}).get("fetch"):
        raise ScanError("resource fetching is disabled in this scan")
    if not hasattr(scan, "_resource_requests_used"):
        scan._resource_requests_used = request_count(scan.con)
    scan._begin()
    try:
        pending = scan.con.execute(
            "SELECT MIN(resource_ref_id) FROM resource_refs WHERE resource_url_id=? AND kind=? AND capture_state='not_fetched'",
            (url_id, kind),
        ).fetchone()[0]
        if pending is None:
            scan.con.rollback()
            return False
        if (
            scan._resource_requests_used + outcome.requests_used
            > config["resources"]["max_requests"]
        ):
            raise ScanError("resource HTTP attempts exceed the recorded request budget")
        policy = json.loads(scan.con.execute("SELECT retention_json FROM scan").fetchone()[0])
        scan._check_capture_disk_space(policy, byte_count)
        response_id = existing_response_id
        for event in outcome.captures:
            observed_id, _ = store_response(scan.con, event, purpose=kind, policy=policy)
            if event.requested_url == url[0]:
                response_id = observed_id
        scan._record_session_change(outcome.captures)
        scan._hit("after_resource_body")
        state, reason = outcome.capture_state, outcome.reason
        if response_id is not None:
            response = scan.con.execute(
                "SELECT request_url_id,purpose,body_state,body_reason,effective_status_code FROM responses WHERE response_id=?",
                (response_id,),
            ).fetchone()
            if response is None or tuple(response)[:2] != (url_id, kind):
                raise ScanError("resource response differs from its declared URL or purpose")
            if state == "measured" and response[2] != "complete":
                state, reason = "body_unavailable", response[3]
            if state == "measured" and (response[4] is None or not 200 <= response[4] < 300):
                state, reason = "fetch_failed", "resource response was not successful"
        elif state in {"measured", "body_unavailable"}:
            raise ScanError("resource outcome is missing its response observation")
        if state != "measured" and not reason:
            reason = state
        if state == "measured":
            reason = ""
        scan.con.execute(
            "UPDATE resource_refs SET response_id=?,capture_state=?,reason=? WHERE resource_ref_id IN (SELECT resource_ref_id FROM resource_refs WHERE resource_url_id=? AND kind=? AND capture_state='not_fetched' ORDER BY resource_ref_id LIMIT 512)",
            (response_id, state, reason, url_id, kind),
        )
        scan._hit("after_resource_references")
        runtime = scan.resume_snapshot()["runtime"]
        if elapsed_seconds < runtime["elapsed_seconds"]:
            raise ScanError("resource elapsed time cannot go backwards")
        runtime["elapsed_seconds"] = elapsed_seconds
        if throttle_state is not None:
            runtime["throttle"] = throttle_state
        scan._write_runtime(runtime, runtime["max_depth_reached"])
        operation_ordinal = scan.con.execute(
            "SELECT COUNT(*) FROM context_items WHERE kind='resource_commit'"
        ).fetchone()[0]
        _insert(
            scan.con,
            "context_items",
            {
                "kind": "resource_commit",
                "item_key": f"resource:{operation_ordinal}",
                "payload_version": "scan_context.v1",
                "payload_json": _dump(
                    {
                        "digest": _digest(
                            {
                                "resource_url_id": url_id,
                                "kind": kind,
                                "captures": metadata,
                                "state": state,
                                "reason": reason,
                                "existing_response_id": existing_response_id,
                            }
                        ),
                        "requests_used": outcome.requests_used,
                    }
                ),
                "completeness": "complete",
                "reason": "",
            },
        )
        scan.con.execute("UPDATE scan SET evidence_revision=evidence_revision+1")
        scan._sync_corpus()
        scan._hit("before_resource_commit")
        scan.con.commit()
        scan._resource_requests_used += outcome.requests_used
        return True
    except BaseException:
        scan._rollback()
        raise
