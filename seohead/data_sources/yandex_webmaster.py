"""Read-only Yandex Webmaster REST adapter with injected transport support."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

HOST = "https://api.webmaster.yandex.net/v4"
TIMEOUT = 30
Transport = Callable[[str, str, dict[str, Any] | None, str], str]


def _default_transport(method: str, url: str, payload: dict[str, Any] | None, token: str) -> str:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload else None,
        method=method,
        headers={"Authorization": f"OAuth {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
        return response.read().decode("utf-8")


def collect(
    operation: str, *, user_id: str, host_id: str | None = None, token: str | None = None,
    transport: Transport | None = None
) -> dict[str, Any]:
    """Collect verified-host, diagnostics, sitemap, or search-performance REST evidence."""
    from seohead.data_sources.credentials import MissingCredential, yandex_webmaster_token

    if operation not in {"hosts", "indexing", "crawl", "sitemaps", "search_performance"} or not user_id:
        raise ValueError("a supported operation and user_id are required")
    if operation != "hosts" and not host_id:
        raise ValueError("host_id is required for this Yandex Webmaster operation")
    try:
        bearer = token or yandex_webmaster_token()
    except MissingCredential as exc:
        return {"ok": False, "state": "not_configured", "verified": False, "error": str(exc)}
    paths = {
        "hosts": f"/user/{user_id}/hosts",
        "indexing": f"/user/{user_id}/hosts/{host_id}/search-urls",
        "crawl": f"/user/{user_id}/hosts/{host_id}/search-urls/events/samples",
        "sitemaps": f"/user/{user_id}/hosts/{host_id}/sitemaps",
        "search_performance": f"/user/{user_id}/hosts/{host_id}/search-queries/popular?order_by=TOTAL_SHOWS",
    }
    try:
        body = json.loads((transport or _default_transport)("GET", HOST + paths[operation], None, bearer))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"ok": False, "state": "failed", "error": str(exc)}
    if not isinstance(body, dict):
        return {"ok": False, "state": "failed", "error": "malformed Yandex Webmaster response"}
    return {"ok": True, "state": "complete", "operation": operation, "data": body, "read_only": True}
