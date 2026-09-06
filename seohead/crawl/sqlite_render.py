"""Bounded rendering escalation for a native SQLite scan.

The regular directory crawl keeps rendered HTML in ``EscalationResult`` long
enough for ``apply_rendered_evidence`` to fold it back into its page list.  A
scan artifact must not do that: a large crawl would retain every serialized
DOM twice.  This adapter consumes each DOM immediately, commits it in the
same transaction as its page and graph observations, and leaves only the
small escalation summary in memory.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from seohead.crawl import render_escalation


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _policy_facts(settings: dict[str, Any]) -> dict[str, bool]:
    """Return only facts the renderer may record about retention policy."""
    browser = settings["rendering"]["browser"]
    return {
        "credentials_used": bool(settings["http"]["credential_headers"])
        or bool(browser.get("persistent_profile")),
        # A browser DOM is not an HTTP response.  It cannot honestly inherit a
        # cache-control header from the static response.
        "cache_control_no_store": False,
    }


def _unknown_renderer(target_url: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Record an unsuccessful renderer attempt without inventing its identity."""
    browser = settings["rendering"]["browser"]
    viewport_name = browser["viewport"]
    from seohead.tools.render import VIEWPORT_PRESETS

    return {
        "engine": "unknown",
        "engine_version": "unknown",
        "navigation": {
            "requested_url": target_url,
            "final_url": target_url,
            "wait_until": browser["wait_until"],
            "timeout_seconds": float(settings["http"]["timeout_seconds"]),
        },
        "settings": {
            "viewport": dict(VIEWPORT_PRESETS[viewport_name]),
            "device_pixel_ratio": float(browser["device_pixel_ratio"]),
            "mobile_emulation": bool(browser["mobile_emulation"]),
            "touch_emulation": bool(browser["touch_emulation"]),
            "script_timeout_seconds": float(browser["script_timeout_seconds"]),
            "resize_to_content": bool(browser["resize_to_content"]),
            "resize_to_content_max_height_px": int(browser["resize_to_content_max_height_px"]),
            "persistent_profile": bool(browser["persistent_profile"]),
        },
        "transforms": {
            "flatten_shadow_dom_requested": bool(browser["flatten_shadow_dom"]),
            "flatten_shadow_dom_applied": 0,
            "flatten_iframes_requested": bool(browser["flatten_iframes"]),
            "flatten_iframes_applied": 0,
        },
        "policy": _policy_facts(settings),
    }


def _rendered_batch(
    parsed: dict[str, Any] | None,
    *,
    target_url: str,
    depth: int,
    settings: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Use the native collector's existing parser-to-observation contract.

    Rendered evidence never discovers or queues a URL.  ``_document_batch``
    is still the right conversion because it applies the same storage and
    attribute gates as static evidence; its candidates and decisions are
    intentionally discarded.
    """
    from seohead.crawl.spider import Scope
    from seohead.crawl.sqlite_adapter import _document_batch

    batch = _document_batch(
        parsed,
        source_url=target_url,
        depth=depth,
        scope=Scope.from_config(settings["scope"]),
        start_host=(urlsplit(target_url).hostname or "").lower(),
        settings=settings,
    )
    return batch.links, batch.forms, batch.partial_reasons


def _candidate(
    record: Any,
    target_url: str,
    fetched: dict[str, Any],
    representation: str,
    current_links: list[Any],
    settings: dict[str, Any],
    max_parse_bytes: int,
) -> tuple[Any | None, dict[str, Any] | None, bool]:
    """Derive one rendered page with the established raw/rendered union rule."""
    from dataclasses import replace

    html = fetched.get("html")
    if not isinstance(html, str) or not html:
        return None, None, False
    from seohead.crawl.sqlite_adapter import _parse_options

    candidate = replace(record)
    parsed, degenerate = render_escalation.fold_rendered_evidence(
        candidate,
        current_links,
        target_url,
        fetched,
        representation,
        parse_options=_parse_options(settings, None),
        max_response_bytes=max_parse_bytes,
    )
    if degenerate:
        return None, parsed, True
    return candidate, parsed, False


def _source_links(scan: Any, target_url: str, fallback: list[Any]) -> list[Any]:
    """Read one static source's observations without materializing the graph."""
    from seohead.crawl.spider import LinkEdge

    if not hasattr(scan, "con"):
        return [edge for edge in fallback if edge.source == target_url]
    rows = scan.con.execute(
        "SELECT destination.url AS destination,l.anchor,l.nofollow,l.position,l.rel_json,l.target,"
        "l.raw_href FROM links l JOIN urls source ON source.url_id=l.source_url_id "
        "JOIN urls destination ON destination.url_id=l.destination_url_id "
        "WHERE source.url=? AND l.evidence_representation='static' ORDER BY l.ordinal",
        (target_url,),
    )
    return [
        LinkEdge(
            source=target_url,
            destination=row["destination"],
            anchor=row["anchor"],
            nofollow=bool(row["nofollow"]),
            position=row["position"],
            rel=tuple(json.loads(row["rel_json"])),
            target=row["target"],
            raw_href=row["raw_href"],
        )
        for row in rows
    ] or [edge for edge in fallback if edge.source == target_url]


def _apply_committed_result(record: Any, candidate: Any) -> None:
    """Update the transient page only after its SQL transaction committed."""
    for field in dataclasses.fields(record):
        setattr(record, field.name, getattr(candidate, field.name))


def _document_state(scan: Any, document_id: int) -> tuple[str, str]:
    """Report the stored retention state without treating omission as failure."""
    if not hasattr(scan, "con"):
        return "complete", ""
    row = scan.con.execute(
        "SELECT body_state,body_reason FROM documents WHERE document_id=?", (document_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("render transaction returned an unknown document")
    return str(row[0]), "" if row[0] == "complete" else str(row[1])


def _static_html(scan: Any, target_url: str, max_bytes: int) -> str:
    """Read one retained static document for legacy-fragment probing.

    This is deliberately a database read, not a fallback request.  A resumed
    scan therefore cannot turn a missing raw observation into fresh evidence.
    """
    from seohead.storage.bodies import read_document

    row = scan.con.execute(
        "SELECT d.document_id FROM documents d JOIN urls u ON u.url_id=d.url_id "
        "WHERE u.url=? AND d.representation='static' AND d.body_state='complete' "
        "ORDER BY d.document_id DESC LIMIT 1",
        (target_url,),
    ).fetchone()
    if row is None:
        return ""
    return read_document(scan.con, int(row[0]), max_decoded_bytes=max_bytes)


def _legacy_fetch(
    target_url: str,
    raw_html: str,
    settings: dict[str, Any],
    scan: Any,
    max_parse_bytes: int,
) -> dict[str, Any]:
    """Fetch an opted-in escaped fragment and retain its actual response."""
    from seohead.crawl.capture import now_utc
    from seohead.crawl.collect import fetch_one
    from seohead.crawl.settings import resolve_credential_headers
    from seohead.recon.net import http_client
    from seohead.tools import render as render_tool

    escaped = render_tool.legacy_fragment_target(target_url, raw_html)
    if not escaped:
        return {"ok": False, "url": target_url, "error": "no legacy fragment target"}
    captures: list[Any] = []
    host = urlsplit(escaped).hostname or ""
    extra_headers = dict(settings["http"]["headers"])
    extra_headers.update(
        resolve_credential_headers(settings["http"]["credential_headers"], host) or {}
    )
    client, _http2 = http_client(
        settings["http"]["timeout_seconds"],
        follow_redirects=False,
        headers={"User-Agent": settings["http"]["user_agent"]},
    )
    try:
        record, parsed = fetch_one(
            escaped,
            client=client,
            extra_headers=extra_headers or None,
            user_agent=settings["http"]["user_agent"],
            max_response_bytes=settings["limits"]["max_response_bytes"],
            retry_on_timeout=settings["http"]["retry_on_timeout"],
            capture_observer=captures.append,
            capture_max_bytes=max_parse_bytes,
        )
    finally:
        client.close()
    html = parsed.get("_raw_html", "") if parsed is not None else ""
    return {
        "ok": bool(parsed is not None and html),
        "url": target_url,
        "final_url": record.final_url or escaped,
        "html": html,
        "record": record,
        "parsed": parsed,
        "captures": tuple(captures),
        "captured_at": captures[-1].received_at if captures else now_utc(),
        "renderer": {
            **_unknown_renderer(target_url, settings),
            "engine": "legacy-escaped-fragment",
            "engine_version": "scan.v1",
            "navigation_transform": "legacy_escaped_fragment",
            "navigation": {
                "requested_url": escaped,
                "final_url": record.final_url or escaped,
                "wait_until": settings["rendering"]["browser"]["wait_until"],
                "timeout_seconds": float(settings["http"]["timeout_seconds"]),
            },
        },
    }


def run_render_escalation(scan: Any, result: Any, settings: dict[str, Any]) -> Any:
    """Run selective rendering and store each attempted representation promptly.

    The return value remains ``EscalationResult`` so existing audit summaries
    retain their contract.  Its ``rendered`` entries contain only metadata;
    serialized HTML is released as soon as its transaction has committed.
    """
    mode = settings["rendering"]["mode"]
    if mode == "raw" or not result.pages:
        return render_escalation.EscalationResult(mode=mode)

    from seohead.tools import render as render_tool

    rendering_config = settings["rendering"]
    start_url = (
        scan.con.execute("SELECT start_url FROM scan").fetchone()[0]
        if getattr(scan, "con", None) is not None
        else result.pages[0].url
    )
    from seohead.storage import MAX_RECORD_BYTES

    # Parsing is independently bounded from retention.  Body-off scans still
    # need a bounded DOM to produce findings, while a larger parse limit must
    # never exceed the atomic writer's 64 MiB input ceiling.
    max_parse_bytes = min(settings["limits"]["max_response_bytes"], 8 * MAX_RECORD_BYTES)
    max_retained_bytes = settings["storage"]["max_body_bytes"]

    if mode == "js":

        def probe(target: str) -> dict[str, Any]:
            scan.preflight_capture()
            try:
                raw_html = _static_html(scan, target, max_retained_bytes)
            except Exception:
                raw_html = ""
            if not raw_html:
                scan.commit_render(
                    target,
                    None,
                    html=None,
                    renderer=_unknown_renderer(target, settings),
                    captured_at=_now(),
                    body_state="unavailable",
                    body_reason="not_in_corpus",
                )
                return {
                    "ok": False,
                    "needs_escalation": False,
                    "reason": "retained static body is unavailable",
                }
            fetched = render_tool.render_document(
                target,
                rendering_config,
                user_agent=settings["http"]["user_agent"],
                max_html_bytes=max_parse_bytes,
                policy_facts=_policy_facts(settings),
            )
            renderer = fetched.get("renderer")
            if not isinstance(renderer, dict):
                renderer = _unknown_renderer(target, settings)
            if not fetched.get("ok"):
                scan.commit_render(
                    target,
                    None,
                    html=None,
                    renderer=renderer,
                    captured_at=_now(),
                    body_state="truncated"
                    if fetched.get("dom_state") == "truncated"
                    else "unavailable",
                    body_reason="truncated"
                    if fetched.get("dom_state") == "truncated"
                    else "fetch_failed",
                )
                return {"ok": False, "needs_escalation": False}
            # A probe DOM is an observation in its own right.  Store it before
            # comparing, then free it; a later accepted full render gets its
            # own document and page/graph update.
            scan.commit_render(
                target,
                None,
                html=fetched.get("html"),
                renderer=renderer,
                captured_at=_now(),
            )
            raw = render_tool._snapshot(raw_html, target)
            rendered_html = str(fetched.get("html") or "")
            final_url = str(fetched.get("final_url") or target)
            rendered = render_tool._snapshot(rendered_html, final_url)
            shell = render_tool.detect_empty_shell(raw_html)
            findings = render_tool.compare(raw, rendered, raw_html, shell)
            return {
                "ok": True,
                "needs_escalation": findings != [render_tool.ALL_CLEAR],
                "empty_shell": shell,
            }

        def render_fetch(target: str) -> dict[str, Any]:
            scan.preflight_capture()
            return render_tool.render_document(
                target,
                rendering_config,
                user_agent=settings["http"]["user_agent"],
                max_html_bytes=max_parse_bytes,
                policy_facts=_policy_facts(settings),
            )

        representation = "rendered"
    else:

        def probe(target: str) -> dict[str, Any]:
            scan.preflight_capture()
            try:
                html = _static_html(scan, target, max_retained_bytes)
            except Exception:
                html = ""
            return {
                "ok": bool(html),
                "needs_escalation": render_tool.legacy_fragment_target(target, html) is not None,
                "empty_shell": render_tool.detect_empty_shell(html),
            }

        def render_fetch(target: str) -> dict[str, Any]:
            scan.preflight_capture()
            try:
                html = _static_html(scan, target, max_retained_bytes)
            except Exception:
                return {"ok": False, "url": target, "error": "raw document unavailable"}
            return _legacy_fetch(target, html, settings, scan, max_parse_bytes)

        representation = "legacy_fragment"

    def consume(target: str, fetched: dict[str, Any], label: str) -> dict[str, Any]:
        # A browser failure has no trustworthy renderer provenance to put in
        # the artifact.  The escalation summary still records the attempted
        # URL; static evidence remains selected.
        if not fetched.get("ok"):
            renderer = fetched.get("renderer")
            scan.commit_render(
                target,
                None,
                html=None,
                renderer=renderer
                if isinstance(renderer, dict)
                else _unknown_renderer(target, settings),
                captured_at=str(fetched.get("captured_at") or _now()),
                representation=label,
                body_state="truncated"
                if fetched.get("dom_state") == "truncated"
                else "unavailable",
                body_reason="truncated"
                if fetched.get("dom_state") == "truncated"
                else "fetch_failed",
                captures=fetched.get("captures", ()) if label == "legacy_fragment" else (),
            )
            return {
                "accepted": False,
                "state": "unavailable",
                "reason": str(fetched.get("error") or "render failed"),
            }

        record = next((page for page in result.pages if page.url == target), None)
        if record is None:
            return {"accepted": False, "state": "unavailable", "reason": "static page is absent"}
        raw_source_links = _source_links(scan, target, result.links)
        candidate, parsed, degenerate = _candidate(
            record,
            target,
            fetched,
            label,
            raw_source_links,
            settings,
            max_parse_bytes,
        )
        if candidate is None or parsed is None:
            scan.commit_render(
                target,
                None,
                html=fetched.get("html") if label == "rendered" else None,
                renderer=fetched.get("renderer") or _unknown_renderer(target, settings),
                captured_at=str(fetched.get("captured_at") or _now()),
                representation=label,
                captures=fetched.get("captures", ()) if label == "legacy_fragment" else (),
            )
            return {
                "accepted": False,
                "state": "unavailable",
                "reason": "rendered body is degenerate"
                if degenerate
                else "rendered body is not parseable",
            }
        links, forms, partial_reasons = _rendered_batch(
            parsed,
            target_url=target,
            depth=candidate.crawl_depth,
            settings=settings,
        )
        captures = fetched.get("captures", ()) if label == "legacy_fragment" else ()
        document_id = scan.commit_render(
            target,
            dataclasses.asdict(candidate),
            html=fetched.get("html") if label == "rendered" else None,
            renderer=fetched.get("renderer") or {},
            captured_at=str(fetched.get("captured_at") or _now()),
            links=links,
            forms=forms,
            representation=label,
            captures=captures,
            partial_reasons=partial_reasons,
        )
        state, reason = _document_state(scan, document_id)
        _apply_committed_result(record, candidate)
        if target == start_url:
            # Keep at most this one transient DOM for the current audit gate,
            # including when retention omitted its bytes by policy.
            result._rendered_start_html = fetched.get("html")
        return {"accepted": True, "state": state, "reason": reason}

    return render_escalation.escalate(
        result.pages,
        rendering_config,
        probe=probe,
        render_fetch=render_fetch,
        representation_label=representation,
        render_consumer=consume,
    )


__all__ = ["run_render_escalation"]
