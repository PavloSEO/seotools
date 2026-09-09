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
    "gsc": {
        "credential_components": ["oauth_bearer"], "access": "read_only",
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
}


def provider_registry() -> dict[str, Any]:
    """Return immutable-by-convention metadata; callers receive a JSON-safe copy."""
    return {"format": "seohead.provider-registry.v1", "providers": json.loads(json.dumps(_REGISTRY))}


def _credential_components(provider: str) -> dict[str, bool]:
    paths = {
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
    }
    if provider not in paths:
        raise ValueError("unknown provider")
    return {name: credentials.available(*source) for name, source in paths[provider].items()}


def sources_doctor() -> dict[str, Any]:
    """Configuration inspection without secret values or a false verification claim."""
    providers = {}
    for name in _REGISTRY:
        components = _credential_components(name)
        providers[name] = {
            "state": "credential_present" if all(components.values()) else "not_configured",
            "credential_components": components,
            "verified": False,
            "note": "run explicit provider-verify; configured credentials are not verified access",
        }
    return {"format": "seohead.provider-doctor.v1", "providers": providers}


def _reference(value: Any) -> str | None:
    if value is None:
        return None
    return "sha256:" + hashlib.sha256(str(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _save_local_artifact(directory: str | Path, value: dict[str, Any]) -> str:
    root = Path(directory)
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
        "filters": request.get("filters") or {},
        "target_reference": _reference(request.get("site_url") or request.get("url") or request.get("property_id") or request.get("host_id")),
        "pagination": {"returned": result.get("returned", len(rows)), "truncated": bool(result.get("truncated"))},
        "row_counts": {"returned": result.get("returned", len(rows))},
        "sampling": result.get("sampling_or_thresholding") or result.get("sampling") or False,
        "privacy_thresholds": result.get("privacy_thresholds") or "not_reported",
        "quota_state": result.get("quota_mode") or _REGISTRY[provider]["quota_mode"],
        "status": state,
        "complete": state == "complete",
        "artifact_reference": artifact,
        "redaction": "raw provider rows and identifiers are restricted local artifacts by default",
        "error": result.get("error"),
    }


def provider_verify(provider: str, request: dict[str, Any] | None = None, *, transport: Any = None) -> dict[str, Any]:
    """Perform one declared bounded read when supported; credentials alone stay unverified."""
    request = request or {}
    if provider not in _REGISTRY:
        raise ValueError("unknown provider")
    components = _credential_components(provider)
    if not all(components.values()):
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
    return {
        "ok": bool(result.get("ok")), "provider": provider,
        "state": "verified" if result.get("ok") else result.get("state", "verification_failed"),
        "verified": bool(result.get("ok")), "credential_components": components,
        "selected_reference": _reference(request.get("site_url") or request.get("host_id")),
        "scopes": result.get("scopes", []), "quota_mode": _REGISTRY[provider]["quota_mode"],
        "contract_compatible": bool(result.get("ok")), "error": result.get("error"),
    }


def provider_collect(
    provider: str, operation: str, request: dict[str, Any], *, transport: Any = None,
    artifact_dir: str | Path | None = None
) -> dict[str, Any]:
    """Explicit provider collection; each dispatch is read-only and may return skipped evidence."""
    if provider not in _REGISTRY or operation not in _REGISTRY[provider]["operations"]:
        raise ValueError("unsupported provider operation")
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
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
    artifact = _save_local_artifact(artifact_dir, result) if artifact_dir else None
    return {"evidence": _evidence(provider, operation, request, result, artifact), "result": result if artifact else None}


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
