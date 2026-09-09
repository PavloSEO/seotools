"""scan.v2 declared-resource graph, independent of the frozen v1 script/style lane."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from . import ScanError

KIND = "resource_graph_coverage"
_REPRESENTATIONS = {"static", "rendered", "legacy_fragment"}
_STATES = {
    "complete",
    "partial",
    "unavailable",
    "disabled",
    "empty",
    "excluded",
    "failed",
    "budget",
}
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE)
_CSS_IMPORT = re.compile(r"@import\s+(?:url\()?\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the optional v2 resource extension only in an explicit v2 writer."""
    if con.execute("PRAGMA user_version").fetchone()[0] != 2:
        return
    con.execute(
        "CREATE TABLE IF NOT EXISTS resource_graph_occurrences (occurrence_id INTEGER PRIMARY KEY,"
        "page_url_id INTEGER NOT NULL,source_document_id INTEGER NOT NULL,representation TEXT NOT NULL,"
        "ordinal INTEGER NOT NULL,kind TEXT NOT NULL,carrier TEXT NOT NULL,raw_url TEXT NOT NULL,"
        "resolved_url TEXT NOT NULL,nesting_depth INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,"
        "UNIQUE(page_url_id,source_document_id,representation,ordinal))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS resource_graph_fetches (resolved_url TEXT PRIMARY KEY,state TEXT NOT NULL,"
        "reason TEXT NOT NULL,status_code INTEGER,content_type TEXT NOT NULL,bytes_received INTEGER NOT NULL,"
        "elapsed_seconds REAL,origin_host TEXT NOT NULL,redirects INTEGER NOT NULL,nesting_depth INTEGER NOT NULL)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS resource_graph_occurrences_url ON resource_graph_occurrences(resolved_url)"
    )


def _append(values: list[dict[str, Any]], *, kind: str, carrier: str, raw: str, base_url: str) -> None:
    value = raw.strip()
    resolved = urljoin(base_url, value)
    parts = urlsplit(resolved)
    if not value or parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return
    values.append({"kind": kind, "carrier": carrier, "raw_url": value, "resolved_url": resolved})


def _srcset(values: list[dict[str, Any]], raw: str, base_url: str) -> None:
    for candidate in raw.split(","):
        value = candidate.strip().split(maxsplit=1)[0] if candidate.strip() else ""
        _append(values, kind="image", carrier="srcset", raw=value, base_url=base_url)


def extract(html: str | None, base_url: str, *, max_occurrences: int = 20_000) -> tuple[list[dict[str, Any]], int]:
    """Extract declared HTTP(S) resources and inline CSS references in document order."""
    if not isinstance(html, str):
        return [], 0
    soup = BeautifulSoup(html, features="lxml")
    values: list[dict[str, Any]] = []
    attributes = {
        "img": (("src", "image"), ("srcset", "image")),
        "source": (("src", "media"), ("srcset", "image")),
        "video": (("src", "media"), ("poster", "image")),
        "audio": (("src", "media"),),
        "track": (("src", "media"),),
        "iframe": (("src", "document"),),
        "embed": (("src", "document"),),
        "object": (("data", "document"),),
        "script": (("src", "module"),),
    }
    for tag in soup.find_all(True):
        name = tag.name.lower()
        if name == "link":
            rel = {str(value).lower() for value in tag.get("rel") or ()}
            kind = (
                "stylesheet"
                if "stylesheet" in rel
                else "manifest"
                if "manifest" in rel
                else "module"
                if "modulepreload" in rel
                else None
            )
            if kind and isinstance(tag.get("href"), str):
                _append(values, kind=kind, carrier="link[href]", raw=tag["href"], base_url=base_url)
        for attribute, kind in attributes.get(name, ()):
            raw = tag.get(attribute)
            if not isinstance(raw, str):
                continue
            if attribute == "srcset":
                _srcset(values, raw, base_url)
            else:
                if name == "script" and str(tag.get("type") or "").lower() != "module":
                    kind = "script"
                _append(values, kind=kind, carrier=f"{name}[{attribute}]", raw=raw, base_url=base_url)
        style = tag.get("style")
        if isinstance(style, str):
            _css(values, style, base_url, carrier=f"{name}[style]")
        if name == "style":
            _css(values, tag.get_text(), base_url, carrier="style")
    omitted = max(0, len(values) - max_occurrences)
    return values[:max_occurrences], omitted


def _css(values: list[dict[str, Any]], text: str, base_url: str, *, carrier: str) -> None:
    for raw in _CSS_IMPORT.findall(text):
        _append(values, kind="css_import", carrier=carrier, raw=raw, base_url=base_url)
    for _quote, raw in _CSS_URL.findall(text):
        kind = "font" if urlsplit(raw).path.lower().endswith((".woff", ".woff2", ".ttf", ".otf")) else "css_url"
        _append(values, kind=kind, carrier=carrier, raw=raw, base_url=base_url)


def store_document(
    con: sqlite3.Connection,
    *,
    page_url_id: int,
    source_document_id: int,
    representation: str,
    html: str | None,
) -> dict[str, Any] | None:
    """Persist one representation's declaration population within its capture transaction."""
    if con.execute("PRAGMA user_version").fetchone()[0] != 2:
        return None
    ensure_schema(con)
    if representation not in _REPRESENTATIONS:
        raise ScanError("resource graph representation is invalid")
    document = con.execute(
        "SELECT url_id,representation FROM documents WHERE document_id=?", (source_document_id,)
    ).fetchone()
    if document is None or tuple(document) != (page_url_id, representation):
        raise ScanError("resource graph document binding is invalid")
    url = con.execute("SELECT url FROM urls WHERE url_id=?", (page_url_id,)).fetchone()[0]
    values, omitted = extract(html, url)
    con.execute(
        "DELETE FROM resource_graph_occurrences WHERE page_url_id=? AND source_document_id=? AND representation=?",
        (page_url_id, source_document_id, representation),
    )
    state = "unavailable" if html is None else "partial" if omitted else "empty" if not values else "complete"
    reason = (
        "document body was not available"
        if html is None
        else "resource declaration cap omitted occurrences"
        if omitted
        else ""
    )
    for ordinal, value in enumerate(values):
        con.execute(
            "INSERT INTO resource_graph_occurrences(page_url_id,source_document_id,representation,ordinal,kind,carrier,raw_url,resolved_url,nesting_depth,state,reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                page_url_id,
                source_document_id,
                representation,
                ordinal,
                value["kind"],
                value["carrier"],
                value["raw_url"],
                value["resolved_url"],
                0,
                "disabled",
                "resource fetch is disabled",
            ),
        )
    payload = {
        "schema_version": "resource_graph.v1",
        "page_url_id": page_url_id,
        "source_document_id": source_document_id,
        "representation": representation,
        "observed": len(values),
        "omitted": omitted,
        "state": state,
        "reason": reason,
    }
    con.execute(
        "INSERT OR REPLACE INTO context_items(kind,item_key,payload_version,payload_json,completeness,reason) VALUES(?,?,?,?,?,?)",
        (
            KIND,
            f"document:{source_document_id}",
            "scan_context.v1",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "unavailable" if state == "unavailable" else "partial" if state == "partial" else "complete",
            reason,
        ),
    )
    return payload


def read(con: sqlite3.Connection) -> dict[str, Any]:
    """Read typed resource declarations/fetches without re-extracting or fetching."""
    if con.execute("PRAGMA user_version").fetchone()[0] != 2:
        return {"state": "unavailable", "reason": "resource graph was not stored in this scan"}
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "resource_graph_occurrences" not in names:
        return {"state": "unavailable", "reason": "resource graph extension is absent"}
    occurrences = [
        dict(row)
        for row in con.execute(
            "SELECT * FROM resource_graph_occurrences ORDER BY page_url_id,source_document_id,ordinal"
        )
    ]
    fetches = [dict(row) for row in con.execute("SELECT * FROM resource_graph_fetches ORDER BY resolved_url")]
    return {"state": "complete", "occurrences": occurrences, "fetches": fetches}


def validate(con: sqlite3.Connection) -> None:
    """Validate the optional v2 graph without imposing it on earlier v2 artifacts."""
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    present = {"resource_graph_occurrences", "resource_graph_fetches"} & names
    if not present:
        return
    if present != {"resource_graph_occurrences", "resource_graph_fetches"}:
        raise ScanError("resource graph schema is incomplete")
    for row in con.execute("SELECT * FROM resource_graph_occurrences"):
        if (
            row["representation"] not in _REPRESENTATIONS
            or row["state"] not in _STATES
            or row["ordinal"] < 0
            or row["nesting_depth"] < 0
            or not row["kind"]
            or not row["carrier"]
            or not row["raw_url"]
            or not row["resolved_url"]
            or not con.execute(
                "SELECT 1 FROM documents WHERE document_id=? AND url_id=? AND representation=?",
                (row["source_document_id"], row["page_url_id"], row["representation"]),
            ).fetchone()
        ):
            raise ScanError("resource graph occurrence is invalid")
    if con.execute(
        "SELECT 1 FROM resource_graph_occurrences GROUP BY page_url_id,source_document_id,representation "
        "HAVING MIN(ordinal)!=0 OR MAX(ordinal)!=COUNT(*)-1 LIMIT 1"
    ).fetchone():
        raise ScanError("resource graph occurrence ordinals are not contiguous")
    for row in con.execute("SELECT * FROM resource_graph_fetches"):
        if (
            row["state"] not in {"complete", "failed", "excluded", "budget"}
            or row["bytes_received"] < 0
            or row["redirects"] < 0
            or row["nesting_depth"] < 0
        ):
            raise ScanError("resource graph fetch is invalid")


def capture(
    scan: Any,
    settings: dict[str, Any],
    *,
    client: Any = None,
    fetcher: Any = None,
    wait: Any = None,
    clock: Any = None,
) -> dict[str, int]:
    """Fetch deduplicated v2 declarations under resource-graph-specific budgets."""
    if scan.con.execute("PRAGMA user_version").fetchone()[0] != 2:
        return {"stored": 0, "fetched": 0, "excluded": 0, "failed": 0, "budget": 0}
    ensure_schema(scan.con)
    graph = settings["resources"]["graph"]
    if not settings["resources"]["fetch"]:
        return {"stored": 0, "fetched": 0, "excluded": 0, "failed": 0, "budget": 0}
    from seohead.crawl.collect import fetch_one
    from seohead.crawl.sqlite_adapter import _headers
    from seohead.crawl.spider import Scope
    from seohead.crawl.throttle import RequestBudgetExhausted

    start_url = scan.con.execute("SELECT start_url FROM scan WHERE singleton=1").fetchone()[0]
    start_host = (urlsplit(start_url or "").hostname or "").lower()
    scope = Scope.from_config(settings["scope"])
    started = clock() if clock is not None else 0.0
    seen_origins: set[str] = set()
    used_count = used_bytes = 0
    totals = {"stored": 0, "fetched": 0, "excluded": 0, "failed": 0, "budget": 0}
    while True:
        row = scan.con.execute(
            "SELECT o.* FROM resource_graph_occurrences o WHERE o.state='disabled' "
            "ORDER BY o.occurrence_id LIMIT 1"
        ).fetchone()
        if row is None:
            break
        url = row["resolved_url"]
        host = (urlsplit(url).hostname or "").lower()
        reason = scope.rejection(url, start_host)
        if reason:
            _set_occurrence_state(scan.con, row["resolved_url"], "excluded", reason)
            totals["excluded"] += 1
            continue
        elapsed = (clock() - started) if clock is not None else 0.0
        if (
            used_count >= graph["max_requests"]
            or used_bytes >= graph["max_bytes"]
            or (graph["max_seconds"] and elapsed >= graph["max_seconds"])
            or (host not in seen_origins and len(seen_origins) >= graph["max_origins"])
            or row["nesting_depth"] > graph["max_nesting"]
        ):
            _set_occurrence_state(scan.con, url, "budget", "resource graph budget exhausted")
            totals["budget"] += 1
            continue
        existing = scan.con.execute(
            "SELECT state,reason FROM resource_graph_fetches WHERE resolved_url=?", (url,)
        ).fetchone()
        if existing is not None:
            _set_occurrence_state(scan.con, url, existing["state"], existing["reason"])
            totals["stored"] += 1
            continue
        events = []
        before = clock() if clock is not None else 0.0
        try:
            record, _parsed = fetch_one(
                url,
                client=client,
                fetcher=fetcher,
                extra_headers=_headers(settings, url),
                user_agent=settings["http"]["user_agent"],
                max_response_bytes=graph["max_bytes_per_resource"],
                retry_on_timeout=0,
                wait=wait,
                capture_observer=events.append,
                capture_max_bytes=graph["max_bytes_per_resource"],
            )
            event = events[-1] if events else None
            body = event.entity_bytes if event is not None else None
            content_type = record.content_type or ""
            status = record.status_code
            redirects = 1 if record.redirect_url else 0
            if redirects > graph["max_redirects"]:
                state, reason = "failed", "resource redirect budget exhausted"
            elif status is None or not 200 <= status < 300:
                state, reason = "failed", record.error or "resource response was not successful"
            else:
                state, reason = "complete", ""
            received = len(body or b"")
            used_count += 1
            used_bytes += received
            seen_origins.add(host)
            scan.con.execute(
                "INSERT INTO resource_graph_fetches VALUES(?,?,?,?,?,?,?,?,?,?)",
                (url, state, reason, status, content_type, received, (clock() - before) if clock else None, host, redirects, row["nesting_depth"]),
            )
            _set_occurrence_state(scan.con, url, state, reason)
            totals["fetched" if state == "complete" else "failed"] += 1
            if state == "complete" and content_type.partition(";")[0].strip().lower() == "text/css":
                _store_css_children(scan.con, row, (body or b"").decode("utf-8", "replace"), graph["max_nesting"])
        except RequestBudgetExhausted:
            _set_occurrence_state(scan.con, url, "budget", "total HTTP request budget exhausted")
            totals["budget"] += 1
            break
        except Exception as exc:
            used_count += 1
            scan.con.execute(
                "INSERT INTO resource_graph_fetches VALUES(?,?,?,?,?,?,?,?,?,?)",
                (url, "failed", str(exc)[:500], None, "", 0, None, host, 0, row["nesting_depth"]),
            )
            _set_occurrence_state(scan.con, url, "failed", "resource fetch failed")
            totals["failed"] += 1
    scan.con.commit()
    return totals


def _set_occurrence_state(con: sqlite3.Connection, url: str, state: str, reason: str) -> None:
    con.execute(
        "UPDATE resource_graph_occurrences SET state=?,reason=? WHERE resolved_url=? AND state='disabled'",
        (state, reason, url),
    )


def _store_css_children(con: sqlite3.Connection, parent: Any, text: str, max_nesting: int) -> None:
    depth = parent["nesting_depth"] + 1
    if depth > max_nesting:
        return
    values: list[dict[str, Any]] = []
    _css(values, text, parent["resolved_url"], carrier="css")
    ordinal = con.execute(
        "SELECT COALESCE(MAX(ordinal)+1,0) FROM resource_graph_occurrences "
        "WHERE page_url_id=? AND source_document_id=? AND representation=?",
        (parent["page_url_id"], parent["source_document_id"], parent["representation"]),
    ).fetchone()[0]
    for value in values:
        con.execute(
            "INSERT OR IGNORE INTO resource_graph_occurrences(page_url_id,source_document_id,representation,ordinal,kind,carrier,raw_url,resolved_url,nesting_depth,state,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                parent["page_url_id"], parent["source_document_id"], parent["representation"], ordinal,
                value["kind"], value["carrier"], value["raw_url"], value["resolved_url"], depth,
                "disabled", "resource fetch is pending",
            ),
        )
        ordinal += 1
