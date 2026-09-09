"""Read-only GA4 Data API landing-page aggregates for evidence prioritization."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

HOST = "https://analyticsdata.googleapis.com/v1beta"
TIMEOUT = 30
MAX_ROWS = 25_000
Transport = Callable[[str, dict[str, Any], str], str]


def _default_transport(url: str, payload: dict[str, Any], token: str) -> str:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
        return response.read().decode("utf-8")


def landing_pages(
    property_id: str,
    start_date: str,
    end_date: str,
    *,
    include_conversions: bool = False,
    include_revenue: bool = False,
    reporting_identity: str | None = None,
    token: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Return GA4 landing-page aggregates; sessions are not Search Console clicks."""
    from seohead.data_sources.credentials import MissingCredential, ga4_access_token

    if not property_id or not start_date or not end_date or start_date > end_date:
        raise ValueError("property_id and an ordered date range are required")
    try:
        bearer = token or ga4_access_token()
    except MissingCredential as exc:
        return {"ok": False, "state": "not_configured", "verified": False, "error": str(exc)}
    metrics = ["sessions", "engagedSessions"]
    if include_conversions:
        metrics.append("keyEvents")
    if include_revenue:
        metrics.append("totalRevenue")
    payload: dict[str, Any] = {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "landingPagePlusQueryString"}],
        "metrics": [{"name": name} for name in metrics],
        "limit": str(MAX_ROWS),
        "keepEmptyRows": False,
    }
    try:
        raw = (transport or _default_transport)(
            f"{HOST}/properties/{property_id}:runReport", payload, bearer
        )
        body = json.loads(raw)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "state": "failed", "error": str(exc)}
    rows = body.get("rows") if isinstance(body, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return {"ok": False, "state": "failed", "error": "malformed GA4 Data API response"}
    sampling = body.get("metadata", {}).get("dataLossFromOtherRow") if isinstance(body.get("metadata"), dict) else None
    return {
        "ok": True,
        "state": "partial" if len(rows) >= MAX_ROWS or sampling else "complete",
        "property_reference": "redacted-local-artifact",
        "reporting_identity": reporting_identity or "provider default",
        "period": {"start_date": start_date, "end_date": end_date},
        "dimensions": ["landingPagePlusQueryString"],
        "metrics": metrics,
        "rows": rows,
        "returned": len(rows),
        "truncated": len(rows) >= MAX_ROWS,
        "sampling_or_thresholding": bool(sampling),
        "note": "GA4 sessions are analytics visits and are not Google Search Console clicks.",
    }
