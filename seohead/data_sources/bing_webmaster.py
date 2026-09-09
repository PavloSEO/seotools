"""Read-only Bing Webmaster REST adapter; legacy SOAP/POX routes are deliberately absent."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

HOST = "https://ssl.bing.com/webmaster/api.svc/json"
TIMEOUT = 30
Transport = Callable[[str], str]


def _default_transport(url: str) -> str:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # nosec B310
        return response.read().decode("utf-8")


def collect(
    operation: str, *, site_url: str, api_key: str | None = None, transport: Transport | None = None
) -> dict[str, Any]:
    """Read REST site/crawl/link/keyword/performance evidence for a declared site."""
    from seohead.data_sources.credentials import MissingCredential, bing_webmaster_key

    methods = {
        "sites": "GetUserSites",
        "crawl": "GetCrawlIssues",
        "links": "GetLinkCounts",
        "keywords": "GetQueryStats",
        "search_performance": "GetQueryStats",
    }
    if operation not in methods or (operation != "sites" and not site_url):
        raise ValueError("a supported operation and site_url are required")
    try:
        key = api_key or bing_webmaster_key()
    except MissingCredential as exc:
        return {"ok": False, "state": "not_configured", "verified": False, "error": str(exc)}
    params = {"apikey": key}
    if site_url:
        params["siteUrl"] = site_url
    query = urllib.parse.urlencode(params)
    try:
        body = json.loads((transport or _default_transport)(f"{HOST}/{methods[operation]}?{query}"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "state": "failed", "error": str(exc)}
    return {"ok": True, "state": "complete", "operation": operation, "data": body, "read_only": True}
