"""Versioned interpretation of CrUX current-window field Core Web Vitals."""

from __future__ import annotations

import math
from datetime import date
from typing import Any

POLICY = "web-vitals-2024-03"
POLICY_URL = "https://web.dev/articles/defining-core-web-vitals-thresholds"
THRESHOLDS = {
    "largest_contentful_paint": (2500.0, 4000.0, "ms"),
    "interaction_to_next_paint": (200.0, 500.0, "ms"),
    "cumulative_layout_shift": (0.1, 0.25, "score"),
}
LABELS = {
    "largest_contentful_paint": "LCP",
    "interaction_to_next_paint": "INP",
    "cumulative_layout_shift": "CLS",
}


def _date(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    try:
        parts = [value[name] for name in ("year", "month", "day")]
        if any(isinstance(part, bool) or not isinstance(part, int) for part in parts):
            return None
        return date(*parts).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def assess(record: dict[str, Any]) -> dict[str, Any]:
    """Assess one CrUX current record; missing data never becomes a passing metric."""
    if record.get("metric_source") != "CrUX current field data":
        raise ValueError("CWV findings require CrUX current field data")
    period = record.get("collection_period")
    first = _date(period.get("firstDate")) if isinstance(period, dict) else None
    last = (
        _date(period.get("lastDate") or period.get("endDate")) if isinstance(period, dict) else None
    )
    valid_period = bool(first and last and first <= last)
    metrics = record.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    results: dict[str, dict[str, Any]] = {}
    for name, (good, poor, unit) in THRESHOLDS.items():
        entry = metrics.get(name)
        raw = entry.get("p75") if isinstance(entry, dict) else None
        value = _value(raw)
        if record.get("state") == "not_configured":
            reason = "not_configured"
        elif record.get("state") == "no_field_data":
            reason = "no_field_data"
        elif record.get("state") in {"response_too_large", "cache_too_large", "cache_invalid"}:
            reason = record["state"]
        elif record.get("ok") is False:
            reason = "provider_failed"
        elif value is None:
            reason = "missing_or_invalid_p75"
        elif not valid_period:
            reason = "missing_or_invalid_collection_period"
        else:
            reason = None
        if reason:
            state = "unavailable"
        elif value <= good:
            state = "good"
        elif value <= poor:
            state = "needs_improvement"
        else:
            state = "poor"
        results[name] = {
            "label": LABELS[name],
            "p75": value if reason is None else None,
            "unit": unit,
            "state": state,
            "reason": reason,
        }
    states = {entry["state"] for entry in results.values()}
    if states == {"unavailable"}:
        overall = "unavailable"
    elif "unavailable" in states:
        overall = "partial"
    elif "poor" in states:
        overall = "poor"
    elif "needs_improvement" in states:
        overall = "needs_improvement"
    else:
        overall = "good"
    return {
        "schema": "seohead.field-cwv/1",
        "provider": "crux",
        "metric_source": "field",
        "policy": POLICY,
        "policy_url": POLICY_URL,
        "target": record.get("record_target") or record.get("target"),
        "requested_target": record.get("target"),
        "record_target": record.get("record_target"),
        "target_kind": record.get("target_kind"),
        "form_factor": record.get("form_factor") or "ALL_FORM_FACTORS",
        "collection_period": {"first_date": first, "last_date": last},
        "retrieved_at": record.get("retrieved_at"),
        "provider_access": "read_only",
        "cost_mode": "free_within_quota",
        "quota_mode": "Google Cloud API quota",
        "overall": overall,
        "metrics": results,
        "missing_metrics": [
            name for name, entry in results.items() if entry["state"] == "unavailable"
        ],
    }
