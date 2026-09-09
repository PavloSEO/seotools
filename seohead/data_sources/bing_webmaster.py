"""Read-only Bing Webmaster REST adapter, intentionally excluding retired SOAP/POX routes.

Microsoft's current documentation announces REST migration but its account-gated reference is the
source of the concrete operation URL. The operator therefore supplies the documented REST endpoint
selected for the verified account. This client sends one real HTTPS GET through either an injected
transport (offline fixtures) or the standard library; it never falls back to retiring routes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

TIMEOUT = 30
Transport = Callable[[str, dict[str, str]], str]


def _default_transport(url: str, headers: dict[str, str]) -> str:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
        return response.read().decode("utf-8")


def collect(
    operation: str,
    *,
    site_url: str,
    rest_endpoint: str | None = None,
    api_key: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Read one documented REST resource; no legacy protocol route exists in this adapter."""
    from seohead.data_sources.credentials import MissingCredential, bing_webmaster_key

    if operation not in {"sites", "crawl", "links", "keywords", "search_performance"}:
        raise ValueError("unsupported Bing Webmaster REST operation")
    if operation != "sites" and not site_url:
        raise ValueError("site_url is required for this Bing Webmaster operation")
    if not rest_endpoint or not rest_endpoint.startswith("https://") or "api.svc" in rest_endpoint:
        return {
            "ok": False,
            "state": "skipped",
            "reason": "supply the account's documented HTTPS REST endpoint; legacy SOAP/POX/JSON routes are excluded",
        }
    try:
        key = api_key or bing_webmaster_key()
    except MissingCredential as exc:
        return {"ok": False, "state": "not_configured", "verified": False, "error": str(exc)}
    params = {"operation": operation}
    if site_url:
        params["siteUrl"] = site_url
    separator = "&" if "?" in rest_endpoint else "?"
    url = rest_endpoint + separator + urllib.parse.urlencode(params)
    try:
        raw = (transport or _default_transport)(url, {"Authorization": f"Bearer {key}"})
        body = json.loads(raw)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "state": "failed", "error": str(exc)}
    if not isinstance(body, (dict, list)):
        return {"ok": False, "state": "failed", "error": "malformed Bing Webmaster REST response"}
    return {"ok": True, "state": "complete", "operation": operation, "data": body, "read_only": True}
