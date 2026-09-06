"""Bounded same-origin transport for declared scripts and stylesheets."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from seohead.crawl.capture import CaptureEvent
from seohead.crawl.collect import fetch_one
from seohead.crawl.sqlite_adapter import _headers
from seohead.recon.net import validate_url

_MAX_ENTITY_BYTES = 64 * 1024 * 1024
_MAX_METADATA_BYTES = 8 * 1024 * 1024
_MAX_CAPTURES = 1000
_DEFAULT_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_REDIRECTS = 10
_ACCEPT = {
    "script": "application/javascript, text/javascript, application/ecmascript, "
    "text/ecmascript;q=0.9, */*;q=0.1",
    "stylesheet": "text/css, */*;q=0.1",
}


@dataclass(frozen=True)
class ResourceFetchResult:
    """One declaration transport outcome; persistence remains the caller's job."""

    captures: tuple[CaptureEvent, ...]
    capture_state: str
    reason: str
    requests_used: int


class ResourceStop(RuntimeError):
    """The executor cannot start another physical resource request."""

    def __init__(self, reason: str):
        if not isinstance(reason, str) or not reason:
            raise ValueError("resource stop reason must be a nonempty string")
        super().__init__(reason)
        self.reason = reason


def _origin(url: str) -> tuple[str, str, int] | None:
    """Return a normalized HTTP origin without raising on malformed input."""
    try:
        parsed = urlsplit(str(url or ""))
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").rstrip(".").lower()
    if scheme not in {"http", "https"} or not host:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def _mime_matches(kind: str, content_type: str) -> bool:
    from seohead.storage.resources import media_matches

    return media_matches(kind, content_type)


def _metadata_bytes(event: CaptureEvent) -> int:
    payload = dataclasses.asdict(event)
    payload["entity_bytes"] = None
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode())


def _without_entity(event: CaptureEvent, *, reason: str) -> CaptureEvent:
    return dataclasses.replace(
        event,
        entity_bytes=None,
        body_fidelity="unavailable",
        body_state="omitted",
        body_reason=reason,
    )


def _observed_response_time(hops: list[CaptureEvent]) -> float | None:
    values = [event.response_time for event in hops if event.response_time is not None]
    return round(sum(values), 3) if values else None


def _redirect_event(
    first: CaptureEvent,
    final: CaptureEvent,
    hops: list[CaptureEvent],
) -> CaptureEvent:
    chain = []
    for index, event in enumerate(hops[:-1]):
        headers = dict(event.response_headers)
        chain.append(
            {
                "request_url": event.requested_url,
                "status_code": event.status_code,
                "location_raw": headers.get("location", ""),
                "next_url": hops[index + 1].requested_url,
                "blocked": False,
            }
        )
    return dataclasses.replace(
        first,
        effective_url=final.effective_url,
        redirect_history=tuple(chain),
        received_at=final.received_at,
        effective_status_code=final.effective_status_code,
        effective_headers=final.effective_headers,
        content_type=final.content_type,
        content_encoding=final.content_encoding,
        entity_bytes=final.entity_bytes,
        body_fidelity=final.body_fidelity,
        body_state=final.body_state,
        body_reason=final.body_reason,
        error=final.error,
        error_kind=final.error_kind,
        response_time=_observed_response_time(hops),
        credentials_used=any(event.credentials_used for event in hops),
        session_changed=any(event.session_changed for event in hops),
    )


def _blocked_redirect(first: CaptureEvent, hops: list[CaptureEvent]) -> CaptureEvent:
    chain = []
    for index, event in enumerate(hops):
        headers = dict(event.response_headers)
        chain.append(
            {
                "request_url": event.requested_url,
                "status_code": event.status_code,
                "location_raw": headers.get("location", ""),
                "next_url": None if index == len(hops) - 1 else hops[index + 1].requested_url,
                "blocked": index == len(hops) - 1,
            }
        )
    last = hops[-1]
    return dataclasses.replace(
        first,
        effective_url=last.effective_url or last.requested_url,
        redirect_history=tuple(chain),
        received_at=last.received_at,
        effective_status_code=last.effective_status_code,
        effective_headers=last.effective_headers,
        content_type=last.content_type,
        content_encoding=last.content_encoding,
        entity_bytes=None,
        body_fidelity="unavailable",
        body_state="unavailable",
        body_reason="fetch_failed",
        error="resource redirect was blocked before the next request",
        error_kind="blocked_redirect",
        response_time=_observed_response_time(hops),
        credentials_used=any(event.credentials_used for event in hops),
        session_changed=any(event.session_changed for event in hops),
    )


def _pending_redirect(first: CaptureEvent, hops: list[CaptureEvent], next_url: str) -> CaptureEvent:
    """Keep an observed redirect prefix when a later request was not started."""
    event = _blocked_redirect(first, hops)
    chain = list(event.redirect_history)
    chain[-1]["blocked"] = False
    chain[-1]["next_url"] = next_url
    return dataclasses.replace(
        event,
        redirect_history=tuple(chain),
        body_state="omitted",
        body_reason="resource_budget_exhausted",
        error="resource request budget exhausted before redirect target",
        error_kind="resource_budget",
    )


def fetch_resource(
    url: str,
    kind: str,
    *,
    settings: dict[str, Any],
    client: Any,
    throttle: Any,
    origin_url: str,
    robots_allowed: Callable[[str], bool],
    fetcher: Callable[[str], Any] | None = None,
    wait: Callable[[], None] | None = None,
    remaining_requests: int,
    before_request: Callable[[], None] | None = None,
) -> ResourceFetchResult:
    """Fetch one direct same-origin resource through the native capture path.

    Each call to ``fetch_one`` follows exactly one response.  Redirects are
    admitted here before their next request so origin and robots policy apply
    to every hop.  Timeout retries are likewise explicit, which makes the
    caller's remaining request budget count physical attempts rather than
    logical URLs.
    """
    if kind not in _ACCEPT:
        raise ValueError("resource kind must be script or stylesheet")
    if type(remaining_requests) is not int or remaining_requests < 0:
        raise ValueError("remaining_requests must be a nonnegative integer")
    resource_origin = _origin(url)
    configured_origin = _origin(origin_url)
    if resource_origin is None and fetcher is None:
        # Let the shared URL guard provide its existing malformed-port,
        # unsupported-scheme, and credential diagnostics without asking DNS
        # to resolve a candidate that cannot be in scope.
        try:
            validate_url(url)
        except ValueError as exc:
            return ResourceFetchResult((), "excluded_scope", str(exc), 0)
        return ResourceFetchResult((), "excluded_scope", "resource origin is invalid", 0)
    if resource_origin is None or configured_origin is None or resource_origin != configured_origin:
        return ResourceFetchResult((), "excluded_scope", "resource origin differs from page", 0)
    if fetcher is None:
        try:
            validate_url(url)
        except ValueError as exc:
            return ResourceFetchResult((), "excluded_scope", str(exc), 0)
    if not robots_allowed(url):
        return ResourceFetchResult((), "excluded_robots", "robots policy disallows resource", 0)
    resources = settings.get("resources") or {}
    response_limit = min(
        int(resources.get("max_response_bytes", _DEFAULT_RESPONSE_BYTES)),
        int(settings["limits"]["max_response_bytes"]),
        _MAX_ENTITY_BYTES,
    )
    if response_limit < 0:
        raise ValueError("resource response byte limit must be nonnegative")
    used = 0
    entity_total = 0
    metadata_total = 0
    capture_count = 0
    failures: list[CaptureEvent] = []
    redirects: list[CaptureEvent] = []
    current = url
    retry_count = 0
    redirect_count = 0
    gate_error: Exception | None = None
    stopped: ResourceStop | None = None

    def admit(event: CaptureEvent) -> bool:
        """Keep only an atomic unit the writer can accept as a whole."""
        nonlocal entity_total, metadata_total, capture_count
        entity_size = len(event.entity_bytes or b"")
        metadata_size = _metadata_bytes(event)
        if (
            capture_count >= _MAX_CAPTURES
            or entity_total + entity_size > _MAX_ENTITY_BYTES
            or metadata_total + metadata_size > _MAX_METADATA_BYTES
        ):
            return False
        capture_count += 1
        entity_total += entity_size
        metadata_total += metadata_size
        return True

    def overflow_result() -> ResourceFetchResult:
        captures = (
            tuple([*failures, _pending_redirect(redirects[0], redirects, current)])
            if redirects
            else tuple(failures)
        )
        return ResourceFetchResult(
            captures,
            "resource_budget_exhausted",
            "resource capture atomic-unit budget exhausted",
            used,
        )

    def request_gate() -> None:
        nonlocal used, gate_error, stopped
        if used >= remaining_requests:
            stopped = ResourceStop("resource request budget exhausted")
            raise stopped
        try:
            if before_request is not None:
                before_request()
            if wait is not None:
                wait()
        except ResourceStop as exc:
            stopped = exc
            raise
        except Exception as exc:  # fetch_one catches callback errors; restore them below.
            gate_error = exc
            raise RuntimeError("resource preflight failed") from exc
        used += 1

    while True:
        if used >= remaining_requests:
            captures = (
                tuple([*failures, _pending_redirect(redirects[0], redirects, current)])
                if redirects
                else tuple(failures)
            )
            return ResourceFetchResult(
                captures,
                "resource_budget_exhausted",
                "resource request budget exhausted",
                used,
            )
        captured: list[CaptureEvent] = []
        headers = _headers(settings, current) or {}
        headers["Accept"] = _ACCEPT[kind]
        before_events = len(captured)
        try:
            record, _parsed = fetch_one(
                current,
                client=client,
                fetcher=fetcher,
                throttle=throttle,
                extra_headers=headers,
                user_agent=settings["http"]["user_agent"],
                max_response_bytes=response_limit,
                retry_on_timeout=0,
                parse_options={
                    "meta": False,
                    "canonical": False,
                    "og": False,
                    "headings": False,
                    "jsonld": False,
                    "links": False,
                    "text": False,
                },
                cache=None,
                wait=request_gate,
                capture_observer=captured.append,
                capture_max_bytes=response_limit,
            )
        except ResourceStop as exc:
            captures = (
                tuple([*failures, _pending_redirect(redirects[0], redirects, current)])
                if redirects
                else tuple(failures)
            )
            return ResourceFetchResult(captures, "resource_budget_exhausted", exc.reason, used)
        except RuntimeError as exc:
            if gate_error is not None:
                raise gate_error from exc
            raise
        if gate_error is not None:
            raise gate_error
        if stopped is not None:
            # fetch_one reports the rejected callback as a failed transport;
            # it never reached the network and therefore is not evidence.
            del captured[before_events:]
            captures = (
                tuple([*failures, _pending_redirect(redirects[0], redirects, current)])
                if redirects
                else tuple(failures)
            )
            return ResourceFetchResult(
                captures,
                "resource_budget_exhausted",
                stopped.reason,
                used,
            )
        if not captured:
            return ResourceFetchResult(
                tuple(failures), "fetch_failed", "capture observation missing", used
            )
        event = captured[-1]
        # retry observations are independent attempts, never folded into a
        # later success response.
        if record.error_kind == "timeout" and retry_count < settings["http"]["retry_on_timeout"]:
            if not admit(event):
                return overflow_result()
            failures.extend(captured)
            retry_count += 1
            continue
        retry_count = 0
        if record.redirect_url:
            redirect_event = _without_entity(event, reason="not_fetched")
            if not admit(redirect_event):
                return overflow_result()
            redirects.append(redirect_event)
            next_url = record.redirect_url
            if not robots_allowed(next_url):
                return ResourceFetchResult(
                    tuple([*failures, _blocked_redirect(redirects[0], redirects)]),
                    "excluded_robots",
                    "robots policy disallows redirected resource",
                    used,
                )
            next_origin = _origin(next_url)
            if next_origin is None:
                try:
                    validate_url(next_url)
                except ValueError as exc:
                    return ResourceFetchResult(
                        tuple([*failures, _blocked_redirect(redirects[0], redirects)]),
                        "excluded_scope",
                        str(exc),
                        used,
                    )
                return ResourceFetchResult(
                    tuple([*failures, _blocked_redirect(redirects[0], redirects)]),
                    "excluded_scope",
                    "resource redirect has an invalid origin",
                    used,
                )
            if next_origin != configured_origin:
                return ResourceFetchResult(
                    tuple([*failures, _blocked_redirect(redirects[0], redirects)]),
                    "excluded_scope",
                    "resource redirect leaves the page origin",
                    used,
                )
            if fetcher is None:
                try:
                    validate_url(next_url)
                except ValueError as exc:
                    return ResourceFetchResult(
                        tuple([*failures, _blocked_redirect(redirects[0], redirects)]),
                        "excluded_scope",
                        str(exc),
                        used,
                    )
            redirect_count += 1
            if redirect_count > _MAX_REDIRECTS:
                return ResourceFetchResult(
                    tuple([*failures, _blocked_redirect(redirects[0], redirects)]),
                    "fetch_failed",
                    "resource redirect limit exceeded",
                    used,
                )
            current = next_url
            continue
        if record.status_code is None or not 200 <= record.status_code < 300:
            if not admit(event):
                return overflow_result()
            aggregate = (
                _redirect_event(redirects[0], event, [*redirects, event]) if redirects else event
            )
            return ResourceFetchResult(
                tuple([*failures, aggregate]),
                "fetch_failed",
                record.error or f"HTTP status {record.status_code}",
                used,
            )
        final = event
        complete = _redirect_event(redirects[0], final, [*redirects, final]) if redirects else final
        if not admit(final):
            return overflow_result()
        if complete.body_state != "complete":
            return ResourceFetchResult(
                tuple([*failures, complete]),
                "body_unavailable",
                complete.body_reason,
                used,
            )
        if not _mime_matches(kind, complete.content_type):
            return ResourceFetchResult(
                tuple([*failures, complete]),
                "body_unavailable",
                "unsupported_media",
                used,
            )
        return ResourceFetchResult(tuple([*failures, complete]), "measured", "", used)


__all__ = ["ResourceFetchResult", "ResourceStop", "fetch_resource"]
