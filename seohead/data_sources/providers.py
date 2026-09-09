"""Offline-first provider registry, verification, collection evidence, and URL joins.

The registry is descriptive.  A present credential is only configuration state; authenticated
access becomes ``verified`` only after :func:`provider_verify` completes its declared live read.
No registry lookup performs a network request or a paid call.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seohead.data_sources import credentials
from seohead.tools.external_join import join_external_data, orphan_urls

EVIDENCE_FORMAT = "seohead.provider-evidence.v1"
_STATES = {"complete", "partial", "failed", "skipped"}
_REGISTRY: dict[str, dict[str, Any]] = {
    "arsenkin": {
        "credential_components": ["api_token"], "access": "read_only_paid",
        "operations": ["keyword_frequency", "serp_clustering"], "quota_mode": "paid limit credits", "privacy_class": "aggregate",
    },
    "yandex_cloud": {
        "credential_components": ["api_key", "folder_id"], "access": "read_only_paid",
        "operations": ["wordstat", "web_search"], "quota_mode": "provider quota and recorded spend", "privacy_class": "aggregate",
    },
    "gsc": {
        "credential_components": ["oauth_bearer", "service_account"], "access": "read_only",
        "operations": ["verify", "properties", "search_analytics", "inspection", "sitemaps"],
        "quota_mode": "Google Search Console row and request limits", "privacy_class": "restricted",
    },
    "crux": {
        "credential_components": ["api_key"], "access": "read_only",
        "operations": ["current", "history"], "quota_mode": "Google Cloud API quota", "privacy_class": "aggregate",
    },
    "pagespeed": {
        "credential_components": ["api_key"], "access": "read_only",
        "operations": ["mobile_samples", "desktop_samples"], "quota_mode": "Google API quota", "privacy_class": "aggregate",
    },
    "ga4": {
        "credential_components": ["oauth_bearer"], "access": "read_only",
        "operations": ["landing_pages"], "quota_mode": "GA4 Data API quota", "privacy_class": "restricted",
    },
    "metrika": {
        "credential_components": ["oauth_bearer"], "access": "read_only",
        "operations": ["counters", "aggregate_report"], "quota_mode": "Yandex Metrika API quota", "privacy_class": "restricted",
        "excluded_operations": ["raw_logs"],
    },
    "yandex_webmaster": {
        "credential_components": ["oauth_bearer"], "access": "read_only",
        "operations": ["hosts", "indexing", "crawl", "sitemaps", "search_performance"],
        "quota_mode": "Yandex Webmaster application quota", "privacy_class": "restricted",
    },
    "bing_webmaster": {
        "credential_components": ["api_key"], "access": "read_only",
        "operations": ["sites", "crawl", "links", "keywords", "search_performance"],
        "quota_mode": "Bing Webmaster API quota", "privacy_class": "restricted",
    },
    "dataforseo_backlinks": {
        "credential_components": ["login", "password"], "access": "read_only_optional_paid",
        "operations": ["backlinks_summary"], "quota_mode": "paid per provider response", "privacy_class": "restricted",
        "default_enabled": False,
    },
    "indexnow": {
        "credential_components": ["submission_key"], "access": "confirmed_write",
        "operations": ["submit"], "quota_mode": "provider submission quota", "privacy_class": "public_url_list",
        "default_enabled": False,
    },
    "wayback": {
        "credential_components": [], "access": "read_only",
        "operations": ["history"], "quota_mode": "public service pacing", "privacy_class": "public",
    },
    "crtsh": {
        "credential_components": [], "access": "read_only",
        "operations": ["subdomains"], "quota_mode": "public service availability", "privacy_class": "public",
    },
}


def provider_registry() -> dict[str, Any]:
    """Return immutable-by-convention metadata; callers receive a JSON-safe copy."""
    return {"format": "seohead.provider-registry.v1", "providers": json.loads(json.dumps(_REGISTRY))}


def _credential_components(provider: str) -> dict[str, bool]:
    paths = {
        "arsenkin": {"api_token": ("arsenkin/token", "ARSENKIN_TOKEN")},
        "yandex_cloud": {
            "api_key": ("yandex-wordstat/api_key", "YANDEX_CLOUD_API_KEY"),
            "folder_id": ("yandex-wordstat/folder_id", "YANDEX_CLOUD_FOLDER_ID"),
        },
        "gsc": {"oauth_bearer": ("gsc/access_token", "GSC_ACCESS_TOKEN")},
        "crux": {"api_key": ("crux/api_key", "CRUX_API_KEY")},
        "pagespeed": {"api_key": ("pagespeed/api_key", "PAGESPEED_API_KEY")},
        "ga4": {"oauth_bearer": ("ga4/access_token", "GA4_ACCESS_TOKEN")},
        "metrika": {"oauth_bearer": ("yandex-metrika/token", "YANDEX_METRIKA_TOKEN")},
        "yandex_webmaster": {"oauth_bearer": ("yandex-webmaster/access_token", "YANDEX_WEBMASTER_TOKEN")},
        "bing_webmaster": {"api_key": ("bing-webmaster/api_key", "BING_WEBMASTER_API_KEY")},
        "dataforseo_backlinks": {
            "login": ("dataforseo/login", "DATAFORSEO_LOGIN"),
            "password": ("dataforseo/password", "DATAFORSEO_PASSWORD"),
        },
        "indexnow": {"submission_key": ("indexnow/key", "INDEXNOW_KEY")},
        "wayback": {},
        "crtsh": {},
    }
    if provider not in paths:
        raise ValueError("unknown provider")
    components = {name: credentials.available(*source) for name, source in paths[provider].items()}
    if provider == "gsc":
        components["service_account"] = credentials.gsc_service_account_available()
        from seohead.data_sources.oauth import grant_available
        components["durable_oauth"] = grant_available("gsc")
    return components


def sources_doctor() -> dict[str, Any]:
    """Configuration inspection without secret values or a false verification claim."""
    providers = {}
    for name in _REGISTRY:
        components = _credential_components(name)
        available = any(components.values()) if name == "gsc" else all(components.values())
        providers[name] = {
            "state": (
                "credential_present"
                if components and available
                else "not_configured"
                if components
                else "not_required"
            ),
            "credential_components": components,
            "verified": False,
            "note": "run explicit provider-verify; configured credentials are not verified access",
        }
    return {"format": "seohead.provider-doctor.v1", "providers": providers}


def _reference(value: Any) -> str | None:
    """Public envelopes never carry a reversible or guessable target identifier."""
    return None


def _redacted_filters(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {"state": "not_supplied", "keys": []}
    return {
        "state": "values_redacted",
        "keys": sorted(str(key) for key in value),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _save_local_artifact(directory: str | Path, value: dict[str, Any]) -> str:
    root = Path(directory)
    if root.is_symlink():
        raise ValueError("provider artifact directory must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
    destination = root / f"provider-{digest[:16]}.json"
    descriptor, staged = tempfile.mkstemp(prefix=".provider-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, 0o600)
        os.replace(staged, destination)
    finally:
        Path(staged).unlink(missing_ok=True)
    return f"local-artifact:{digest}"


def _evidence(provider: str, operation: str, request: dict[str, Any], result: dict[str, Any], artifact: str | None) -> dict[str, Any]:
    state = result.get("state")
    if state not in _STATES:
        state = "complete" if result.get("ok") else "failed"
    rows = result.get("rows") or result.get("samples") or result.get("summary") or []
    return {
        "format": EVIDENCE_FORMAT,
        "provider": provider,
        "operation": operation,
        "retrieved_at": _now(),
        "period": request.get("period") or result.get("period"),
        "dimensions": request.get("dimensions") or result.get("dimensions") or [],
        "filters": _redacted_filters(request.get("filters")),
        "target_reference": _reference(request.get("site_url") or request.get("url") or request.get("property_id") or request.get("host_id")),
        "pagination": {"returned": result.get("returned", len(rows)), "truncated": bool(result.get("truncated"))},
        "row_counts": {"returned": result.get("returned", len(rows))},
        "sampling": result.get("sampling_or_thresholding") or result.get("sampling") or "unknown",
        "privacy_thresholds": result.get("privacy_thresholds") or "unknown",
        "quota_state": result.get("quota_mode") or _REGISTRY[provider]["quota_mode"],
        "status": state,
        "complete": state == "complete",
        "artifact_reference": artifact,
        "redaction": "target identifiers, filter values, and raw provider rows are restricted local artifacts by default",
        "error": result.get("error"),
    }


def provider_verify(provider: str, request: dict[str, Any] | None = None, *, transport: Any = None) -> dict[str, Any]:
    """Perform one declared bounded read when supported; credentials alone stay unverified."""
    request = request or {}
    if provider not in _REGISTRY:
        raise ValueError("unknown provider")
    components = _credential_components(provider)
    if not components:
        return {
            "ok": False, "provider": provider, "state": "not_required", "verified": False,
            "credential_components": components,
            "note": "this public source has no authenticated-access contract to verify",
        }
    ready = any(components.values()) if provider == "gsc" else all(components.values())
    if not ready:
        return {"ok": False, "provider": provider, "state": "not_configured", "verified": False, "credential_components": components}
    if provider == "gsc":
        from seohead.data_sources.gsc import discover_properties
        result = discover_properties(transport=transport)
    elif provider == "yandex_webmaster":
        if not request.get("user_id"):
            return {
                "ok": False, "provider": provider, "state": "credential_present", "verified": False,
                "credential_components": components,
                "note": "user_id is required for the bounded verified-host discovery read",
            }
        from seohead.data_sources.yandex_webmaster import collect
        result = collect("hosts", user_id=request.get("user_id", ""), transport=transport)
    elif provider == "bing_webmaster":
        from seohead.data_sources.bing_webmaster import collect
        result = collect("sites", site_url=request.get("site_url", ""), transport=transport)
    else:
        return {
            "ok": False, "provider": provider, "state": "credential_present", "verified": False,
            "credential_components": components,
            "note": "this provider needs a declared collection target for a bounded live verification",
        }
    authenticated = bool(result.get("ok"))
    target = request.get("site_url") or request.get("host_id")
    target_access = "not_requested"
    if target:
        candidates: list[Any] = []
        if provider == "gsc":
            properties = result.get("properties")
            candidates = [
                entry.get("site_url") for entry in properties if isinstance(entry, dict)
            ] if isinstance(properties, list) else []
        elif provider == "yandex_webmaster":
            data = result.get("data")
            candidates = [
                entry.get("host_id")
                for entry in (data.get("hosts", []) if isinstance(data, dict) else [])
                if isinstance(entry, dict)
            ]
        elif provider == "bing_webmaster":
            data = result.get("data")
            candidates = [
                entry.get("Url") for entry in data if isinstance(entry, dict)
            ] if isinstance(data, list) else []
        target_access = "verified" if target in candidates else "not_granted" if candidates else "unknown"
    return {
        "ok": authenticated, "provider": provider,
        "state": "authenticated" if authenticated else result.get("state", "verification_failed"),
        "authenticated_account": authenticated, "target_access": target_access,
        "verified": target_access == "verified",
        "credential_components": components,
        "selected_reference": _reference(target),
        "granted_scopes": result.get("scopes") or "unknown",
        "quota_mode": _REGISTRY[provider]["quota_mode"], "quota_state": "unknown",
        "contract_compatible": authenticated and target_access in {"verified", "not_requested"},
        "error": result.get("error"),
    }


def provider_collect(
    provider: str, operation: str, request: dict[str, Any], *, transport: Any = None,
    artifact_dir: str | Path | None = None, event_sink: Any = None
) -> dict[str, Any]:
    """Explicit provider collection; each dispatch is read-only and may return skipped evidence."""
    if provider not in _REGISTRY or operation not in _REGISTRY[provider]["operations"]:
        raise ValueError("unsupported provider operation")
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    if event_sink is not None:
        event_sink.emit(
            "provider_enrichment",
            {"provider": provider, "operation": operation, "state": "started", "rows": 0},
        )
    if provider == "gsc":
        from seohead.data_sources import gsc
        if operation == "properties": result = gsc.discover_properties(transport=transport)
        elif operation == "search_analytics": result = gsc.search_analytics_pages(transport=transport, **request)
        elif operation == "inspection": result = gsc.inspect_urls(transport=transport, **request)
        elif operation == "sitemaps": result = gsc.sitemap_status(transport=transport, **request)
        else: result = provider_verify("gsc", request, transport=transport)
    elif provider == "crux":
        from seohead.data_sources import crux
        result = (crux.history if operation == "history" else crux.query)(fetcher=transport, **request)
    elif provider == "pagespeed":
        from seohead.data_sources import pagespeed
        result = pagespeed.sample(desktop=operation == "desktop_samples", fetcher=transport, **request)
    elif provider == "ga4":
        from seohead.data_sources import ga4
        result = ga4.landing_pages(transport=transport, **request)
    elif provider == "yandex_webmaster":
        from seohead.data_sources import yandex_webmaster
        result = yandex_webmaster.collect(operation, transport=transport, **request)
    elif provider == "bing_webmaster":
        from seohead.data_sources import bing_webmaster
        result = bing_webmaster.collect(operation, transport=transport, **request)
    elif provider == "dataforseo_backlinks":
        from seohead.data_sources import dataforseo
        result = dataforseo.backlinks_summary(**request)
    elif provider == "wayback":
        from seohead.data_sources import wayback
        result = wayback.history(fetcher=transport, **request)
    elif provider == "crtsh":
        from seohead.data_sources import crtsh
        result = crtsh.subdomains(fetcher=transport, **request)
    elif provider in {"arsenkin", "yandex_cloud"}:
        raise ValueError("this paid provider uses its existing dedicated operation contract")
    elif provider == "indexnow":
        raise ValueError("IndexNow is a separately confirmed write action, not provider collection")
    elif provider == "metrika":
        if operation not in {"counters", "aggregate_report"}:
            raise ValueError("Metrika raw Logs API is intentionally unreachable")
        from seohead.data_sources.credentials import MissingCredential
        from seohead.data_sources.metrika import MetrikaClient, MetrikaError, rows_to_records
        try:
            client = MetrikaClient()
            if operation == "counters":
                counters = client.counters()
                result = {"ok": True, "state": "complete", "returned": len(counters), "rows": counters}
            else:
                required = {"counter_id", "metrics", "date1", "date2"}
                if not required <= set(request):
                    raise ValueError("aggregate_report requires counter_id, metrics, date1, and date2")
                body = client.report(
                    {
                        "ids": request["counter_id"], "metrics": request["metrics"],
                        "date1": request["date1"], "date2": request["date2"],
                        **({"dimensions": request["dimensions"]} if request.get("dimensions") else {}),
                    },
                    paginate=bool(request.get("paginate")), limit=int(request.get("limit", 100)),
                )
                result = {
                    "ok": True, "state": "partial" if body.get("capped") else "complete",
                    "period": {"start_date": request["date1"], "end_date": request["date2"]},
                    "rows": rows_to_records(body), "returned": len(body.get("data") or []),
                    "truncated": bool(body.get("capped")),
                }
        except MissingCredential as exc:
            result = {"ok": False, "state": "not_configured", "error": str(exc)}
        except MetrikaError as exc:
            result = {"ok": False, "state": "failed", "error": exc.message, "status": exc.status}
    else:  # pragma: no cover - registry and dispatch stay synchronized above.
        raise ValueError("unsupported provider operation")
    evidence = _evidence(provider, operation, request, result, None)
    artifact = _save_local_artifact(artifact_dir, {"evidence": evidence, "result": result}) if artifact_dir else None
    evidence["artifact_reference"] = artifact
    if event_sink is not None:
        rows = result.get("rows") or result.get("samples") or result.get("summary") or []
        event_sink.emit(
            "provider_enrichment",
            {
                "provider": provider,
                "operation": operation,
                "state": str(result.get("state") or "unknown"),
                "rows": int(result.get("returned", len(rows))),
            },
        )
    return {"evidence": evidence, "result": None}


def provider_join(
    crawl_pages: list[dict[str, Any]], evidence_rows: list[dict[str, Any]], *,
    url_field: str = "url", url_column: str = "url", review_external_only: bool = False,
    adjustments: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Reproducibly join evidence; no external URL enters a crawl without explicit review."""
    joined = join_external_data(crawl_pages, evidence_rows, url_field=url_field, url_column=url_column)
    candidates = orphan_urls(joined, url_column=url_column) if review_external_only else []
    applied = []
    for rule in adjustments or []:
        required = {"source_fields", "period", "coverage", "adjustment"}
        if not isinstance(rule, dict) or not required <= set(rule):
            raise ValueError("priority adjustment requires source fields, period, coverage, and adjustment")
        if (
            not isinstance(rule["source_fields"], list)
            or not rule["source_fields"]
            or not all(isinstance(name, str) and name for name in rule["source_fields"])
        ):
            raise ValueError("priority adjustment requires named source fields")
        if rule["coverage"] not in {"complete", "partial", "sampled", "truncated", "unmatched", "privacy_thresholded"}:
            raise ValueError("priority adjustment has unsupported coverage")
        if rule["coverage"] != "complete":
            applied.append({"applied": False, "reason": "prioritization evidence unavailable", "rule": rule})
        else:
            matches = [
                entry for entry in joined["joined"]
                if all(field in entry["external"] and entry["external"][field] is not None for field in rule["source_fields"])
            ]
            if not matches:
                applied.append({"applied": False, "reason": "no matched source-backed rows", "rule": rule})
                continue
            applied.extend(
                {
                    "applied": True,
                    "url": entry["url"],
                    "adjustment": rule["adjustment"],
                    "source_fields": rule["source_fields"],
                    "period": rule["period"],
                    "technical_severity": entry["page"].get("severity"),
                    "technical_severity_changed": False,
                }
                for entry in matches
            )
    return {
        "format": "seohead.provider-join.v1", "join": joined,
        "list_crawl_candidates": candidates, "frontier_mutated": False,
        "priority_adjustments": applied,
    }


def provider_replay(input_path: str, evidence_file: str, out_dir: str, *, url_column: str = "url", review_external_only: bool = False) -> dict[str, Any]:
    """Join one restricted saved provider result to a saved scan, without network.

    Raw joins remain in a private local file. Returned counts identify unmatched
    populations without exposing URLs, queries, or property identifiers.
    """
    from seohead.storage import open_scan
    source = Path(evidence_file)
    if source.is_symlink() or not source.is_file() or source.stat().st_mode & 0o077 or source.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("evidence_file must be a private bounded regular JSON file")
    stored_bytes = source.read_bytes()
    stored = json.loads(stored_bytes)
    if not isinstance(stored, dict) or set(stored) != {"evidence", "result"}:
        raise ValueError("expected a saved provider collection envelope")
    evidence, result = stored["evidence"], stored["result"]
    if not isinstance(evidence, dict) or evidence.get("format") != EVIDENCE_FORMAT or not isinstance(result, dict):
        raise ValueError("invalid saved provider evidence")
    rows = result.get("rows")
    if not isinstance(rows, list) or len(rows) > 100_000 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("saved provider operation does not contain bounded joinable rows")
    dimensions = result.get("dimensions", [])
    if evidence.get("provider") == "gsc" and "page" in dimensions:
        index = dimensions.index("page")
        rows = [{**row, "url": row["keys"][index] if isinstance(row.get("keys"), list) and len(row["keys"]) > index else None} for row in rows]
        url_column = "url"
    con = open_scan(input_path, require_audit=False)
    try:
        if con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] > 100_000:
            raise ValueError("saved provider join exceeds the page bound")
        pages = [dict(row) for row in con.execute("SELECT u.url,p.status_code FROM pages p JOIN urls u USING(url_id) ORDER BY p.url_id")]
        scan_uuid = con.execute("SELECT scan_uuid FROM scan").fetchone()[0]
    finally:
        con.close()
    joined = provider_join(pages, rows, url_column=url_column, review_external_only=review_external_only)
    joined["source"] = {"scan_uuid": scan_uuid, "provider_evidence_sha256": hashlib.sha256(stored_bytes).hexdigest(), "provider": evidence.get("provider"), "period": evidence.get("period"), "status": evidence.get("status"), "sampling": evidence.get("sampling")}
    reference = _save_local_artifact(out_dir, joined)
    return {"ok": True, "format": "seohead.provider-replay.v1", "counts": joined["join"]["summary"], "artifact_reference": reference, "provider_status": evidence.get("status"), "frontier_mutated": False, "priority_applied": False}
