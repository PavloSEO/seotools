"""Read-only Bing Webmaster JSON REST adapter.

Microsoft's current Bing Webmaster documentation says SOAP and POX retire on 2026-08-31, while
its current primary reference still publishes JSON request examples for the fixed HTTPS
``/webmaster/api.svc/json`` resource. This adapter uses only that JSON form, never SOAP or POX.
Every operation is a bounded GET and accepts an injected transport for offline fixtures.
"""

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
_METHODS = {
    "sites": "GetUserSites",
    "crawl": "GetCrawlIssues",
    "links": "GetLinkCounts",
    "keywords": "GetQueryStats",
    "search_performance": "GetQueryStats",
}


def _default_transport(url: str) -> str:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # nosec B310
        return response.read().decode("utf-8")


def collect(
    operation: str,
    *,
    site_url: str,
    page: int = 0,
    api_key: str | None = None,
    transport: Transport | None = None,
) -> dict[str, Any]:
    """Read site, crawl, link, keyword, or search-performance JSON REST evidence."""
    from seohead.data_sources.credentials import MissingCredential, bing_webmaster_key

    if operation not in _METHODS:
        raise ValueError("unsupported Bing Webmaster JSON operation")
    if operation != "sites" and not site_url:
        raise ValueError("site_url is required for this Bing Webmaster operation")
    if type(page) is not int or page < 0:
        raise ValueError("page must be a non-negative integer")
    try:
        key = api_key or bing_webmaster_key()
    except MissingCredential as exc:
        return {"ok": False, "state": "not_configured", "verified": False, "error": str(exc)}
    params = {"apikey": key}
    if site_url:
        params["siteUrl"] = site_url
    if operation == "links":
        params["page"] = str(page)
    url = f"{HOST}/{_METHODS[operation]}?{urllib.parse.urlencode(params)}"
    try:
        body = json.loads((transport or _default_transport)(url))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "state": "failed", "error": str(exc)}
    if not isinstance(body, dict) or "d" not in body:
        return {"ok": False, "state": "failed", "error": "malformed Bing Webmaster JSON response"}
    return {"ok": True, "state": "complete", "operation": operation, "data": body["d"], "read_only": True}
