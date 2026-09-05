"""Wayback Machine CDX API: when a URL changed, and what it looked like before.

A crawl reports the current state of a page. It cannot say when `/uslugi/fundament/` started
returning 404, or what content used to live there — the difference between a bug report and a
restoration plan (see issue #97). The CDX API answers exactly that: every snapshot the Internet
Archive holds for a URL, with its timestamp, HTTP status, and MIME type at capture time.

No key, no account, no OAuth. The public endpoint self-throttles at roughly one request per
second, so this client issues one request per call and leaves pacing a bulk caller's own concern.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

HOST = "https://web.archive.org/cdx/search/cdx"
TIMEOUT = 20
USER_AGENT = "Mozilla/5.0 (compatible; SEOHEAD-Tools/3.0; +https://seohead.tech/seotools)"
# The JSON response's first row names fields. These are the two fields this
# adapter needs to build a snapshot and its archive URL; accepting a string
# list that lacks them would turn an error-shaped array into clean zero evidence.
_REQUIRED_HEADER_FIELDS = frozenset({"timestamp", "original"})

Fetcher = Callable[[str], str]


def _default_fetcher(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # The request URL is built from the fixed HTTPS CDX endpoint plus an encoded query string.
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
        return response.read().decode("utf-8")


def history(
    url: str,
    *,
    limit: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """Return every recorded snapshot of ``url``, oldest first, as the CDX server orders them.

    ``from_date``/``to_date`` accept any CDX-compatible prefix of ``YYYYMMDDhhmmss`` (a bare year
    or month narrows the range without needing exact hours). A URL with no snapshot at all is not
    a failure: the archive simply never captured it, so this returns ``ok: true`` with an empty
    list rather than treating "nothing found" as an error.
    """
    if not url:
        raise ValueError("url required")
    params: dict[str, str] = {"url": url, "output": "json"}
    if limit:
        params["limit"] = str(int(limit))
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    query = urllib.parse.urlencode(params)
    fetch = fetcher or _default_fetcher
    try:
        raw = fetch(f"{HOST}?{query}")
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "url": url, "error": f"Wayback CDX request failed: {exc}"}

    if not raw.strip():
        # The CDX server returns a fully empty body, not even a header row, when nothing matches.
        return {"ok": True, "url": url, "count": 0, "snapshots": []}
    try:
        rows = json.loads(raw)
    except ValueError:
        return {
            "ok": False,
            "url": url,
            "error": "Wayback CDX returned a response that is not JSON",
        }
    if not isinstance(rows, list):
        return {
            "ok": False,
            "url": url,
            "error": "Wayback CDX returned a JSON body that is not an array",
        }
    if not rows:
        # The documented empty-result shape: a JSON array with nothing in it.
        return {"ok": True, "url": url, "count": 0, "snapshots": []}

    header, *data_rows = rows
    if (
        not isinstance(header, list)
        or not all(isinstance(field, str) for field in header)
        or not _REQUIRED_HEADER_FIELDS.issubset(header)
    ):
        return {
            "ok": False,
            "url": url,
            "error": "Wayback CDX response header row has an unexpected shape",
        }
    snapshots = []
    for row in data_rows:
        if not isinstance(row, list):
            return {
                "ok": False,
                "url": url,
                "error": "Wayback CDX response contains a malformed row",
            }
        record = dict(zip(header, row, strict=False))
        original = record.get("original", url)
        timestamp = record.get("timestamp", "")
        record["archived_url"] = f"https://web.archive.org/web/{timestamp}/{original}"
        snapshots.append(record)
    return {"ok": True, "url": url, "count": len(snapshots), "snapshots": snapshots}
