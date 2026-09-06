"""Transactional JS/CSS declaration inventory; fetching remains outside this module."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from . import MAX_RECORD_BYTES, ScanError, _insert, _url


def media_matches(kind: str, content_type: str) -> bool:
    """Admit declared text resources by their measured media type, without sniffing."""
    media = content_type.partition(";")[0].strip().lower()
    allowed = (
        {"text/css"}
        if kind == "stylesheet"
        else {
            "text/javascript",
            "application/javascript",
            "text/ecmascript",
            "application/ecmascript",
            "application/x-javascript",
        }
        if kind == "script"
        else set()
    )
    return media in allowed


def put_declarations(
    con: sqlite3.Connection,
    *,
    page_url_id: int,
    document_id: int,
    representation: str,
    declarations: Iterable[dict[str, Any]],
    fetch_enabled: bool,
    inventory_state: str = "complete",
    omitted: int = 0,
) -> dict[str, int]:
    """Replace one document representation's resource occurrences in the caller transaction."""
    if representation not in {"static", "rendered", "legacy_fragment"}:
        raise ScanError("resource representation is invalid")
    if (
        type(page_url_id) is not int
        or type(document_id) is not int
        or type(fetch_enabled) is not bool
    ):
        raise ScanError("resource declaration identity is invalid")
    if (
        inventory_state not in {"complete", "partial", "unavailable"}
        or type(omitted) is not int
        or omitted < 0
    ):
        raise ScanError("resource inventory state is invalid")
    if (inventory_state == "complete" and omitted) or (
        inventory_state == "unavailable" and omitted
    ):
        raise ScanError("resource omission count disagrees with inventory state")
    document = con.execute(
        "SELECT url_id,representation FROM documents WHERE document_id=?", (document_id,)
    ).fetchone()
    if document is None or tuple(document) != (page_url_id, representation):
        raise ScanError("resource document URL or representation disagrees with its page")
    con.execute(
        "DELETE FROM resource_refs WHERE page_url_id=? AND representation=?",
        (page_url_id, representation),
    )
    counters = {"script": 0, "stylesheet": 0}
    state = "not_fetched" if fetch_enabled else "resources_disabled"
    size = 0
    for index, item in enumerate(declarations):
        if index >= 20_000 or inventory_state == "unavailable":
            raise ScanError("resource declarations exceed their bounded measured scope")
        if (
            not isinstance(item, dict)
            or set(item) != {"kind", "url", "raw_url"}
            or item["kind"] not in counters
            or any(type(item[key]) is not str or not item[key] for key in ("url", "raw_url"))
        ):
            raise ScanError("resource declaration must be a kind/url/raw_url object")
        size += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if size > MAX_RECORD_BYTES:
            raise ScanError("resource declaration metadata exceeds 8 MiB")
        kind = item["kind"]
        _insert(
            con,
            "resource_refs",
            {
                "page_url_id": page_url_id,
                "ordinal": counters[kind],
                "resource_url_id": _url(con, item["url"]),
                "source_document_id": document_id,
                "kind": kind,
                "representation": representation,
                "raw_url": item["raw_url"],
                "capture_state": state,
                "reason": "not fetched" if fetch_enabled else "resources.fetch disabled",
            },
        )
        counters[kind] += 1
    payload = json.dumps(
        {"document_id": document_id, "state": inventory_state, "omitted": omitted},
        sort_keys=True,
    )
    con.execute(
        "DELETE FROM context_items WHERE kind='resource_inventory' AND item_key=?",
        (f"document:{document_id}",),
    )
    _insert(
        con,
        "context_items",
        {
            "kind": "resource_inventory",
            "item_key": f"document:{document_id}",
            "payload_version": "scan_context.v1",
            "payload_json": payload,
            "completeness": inventory_state,
            "reason": ""
            if inventory_state == "complete"
            else "resource declarations omitted"
            if inventory_state == "partial"
            else "resource declarations were not measured",
        },
    )
    return {"script": counters["script"], "stylesheet": counters["stylesheet"], "omitted": omitted}


def validate_inventory_context(con, item):
    """Validate the closed zero/partial/unavailable inventory marker."""
    try:
        payload = json.loads(item["payload_json"])
    except (ValueError, TypeError) as exc:
        raise ScanError("resource inventory context is invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"document_id", "state", "omitted"}
        or type(payload["document_id"]) is not int
        or payload["document_id"] < 1
        or item["item_key"] != f"document:{payload['document_id']}"
        or item["payload_version"] != "scan_context.v1"
        or payload["state"] not in {"complete", "partial", "unavailable"}
        or item["completeness"] != payload["state"]
        or type(payload["omitted"]) is not int
        or payload["omitted"] < 0
        or (payload["state"] != "partial" and payload["omitted"] != 0)
        or (payload["state"] == "complete" and item["reason"])
        or (payload["state"] != "complete" and not item["reason"])
        or con.execute(
            "SELECT 1 FROM documents WHERE document_id=?", (payload["document_id"],)
        ).fetchone()
        is None
    ):
        raise ScanError("resource inventory context is invalid")


def _dict_rows(con, sql):
    cursor = con.execute(sql)
    names = [column[0] for column in cursor.description]
    for row in cursor:
        yield dict(zip(names, row, strict=True))


def validate_resources(con: sqlite3.Connection) -> None:
    """Check inventory, references and observed bodies without loading the graph."""
    for item in _dict_rows(con, "SELECT * FROM context_items WHERE kind='resource_inventory'"):
        validate_inventory_context(con, item)
    for item in _dict_rows(con, "SELECT * FROM resource_refs"):
        document = con.execute(
            "SELECT url_id,representation FROM documents WHERE document_id=?",
            (item["source_document_id"],),
        ).fetchone()
        if document is None or tuple(document) != (item["page_url_id"], item["representation"]):
            raise ScanError("resource reference document provenance disagrees")
        if item["representation"] not in {"static", "rendered", "legacy_fragment"}:
            raise ScanError("resource representation is invalid")
        if (
            con.execute(
                "SELECT 1 FROM context_items WHERE kind='resource_inventory' AND item_key=?",
                (f"document:{item['source_document_id']}",),
            ).fetchone()
            is None
        ):
            raise ScanError("resource reference lacks its inventory context")
        state = item["capture_state"]
        if state in {"resources_disabled", "not_fetched"} and item["response_id"] is not None:
            raise ScanError("unfetched resource reference cannot name a response")
        if state == "measured" and (item["response_id"] is None or item["reason"]):
            raise ScanError("measured resource requires a complete response and empty reason")
        if state == "body_unavailable" and item["response_id"] is None:
            raise ScanError("unavailable resource body requires an observed response")
        if state != "measured" and not item["reason"]:
            raise ScanError("unmeasured resource requires a named reason")
        if item["response_id"] is not None:
            response = con.execute(
                "SELECT request_url_id,purpose,body_state,body_reason,effective_status_code FROM responses WHERE response_id=?",
                (item["response_id"],),
            ).fetchone()
            if response is None or tuple(response)[:2] != (item["resource_url_id"], item["kind"]):
                raise ScanError("resource response URL or purpose disagrees")
            if state == "measured" and (
                response[2] != "complete" or response[4] is None or not 200 <= response[4] < 300
            ):
                raise ScanError("measured resource lacks a successful complete response body")
            if state == "body_unavailable" and (
                response[2] == "complete" or item["reason"] != response[3]
            ):
                raise ScanError("resource body unavailability disagrees with its response")
        if type(item["ordinal"]) is not int or item["ordinal"] < 0:
            raise ScanError("resource ordinal is invalid")
    if con.execute(
        "SELECT 1 FROM resource_refs GROUP BY page_url_id,representation,kind HAVING MIN(ordinal)!=0 OR MAX(ordinal)!=COUNT(*)-1 LIMIT 1"
    ).fetchone():
        raise ScanError("resource occurrence ordinals are not contiguous")


def resource_capabilities(
    con: sqlite3.Connection, *, fetch_enabled: bool
) -> dict[str, dict[str, str]]:
    """Measure current declaration coverage separately from resource body coverage."""
    # Only the most recent extraction for each logical URL/representation is
    # active; an older partial attempt must not poison its replacement.
    states = {"complete": 0, "partial": 0, "unavailable": 0}
    for (state,) in con.execute(
        "SELECT c.completeness FROM context_items c JOIN documents d "
        "ON c.item_key='document:'||d.document_id WHERE c.kind='resource_inventory' "
        "AND NOT EXISTS(SELECT 1 FROM context_items later JOIN documents nd "
        "ON later.item_key='document:'||nd.document_id WHERE later.kind='resource_inventory' "
        "AND nd.url_id=d.url_id AND nd.representation=d.representation AND nd.document_id>d.document_id)"
    ):
        states[state] += 1
    missing = con.execute(
        "SELECT COUNT(*) FROM pages p WHERE lower(p.content_type) LIKE '%html%' AND "
        "(NOT EXISTS(SELECT 1 FROM context_items c JOIN documents d ON c.item_key='document:'||d.document_id "
        "WHERE c.kind='resource_inventory' AND d.url_id=p.url_id AND d.representation='static') "
        "OR (p.representation!='static' AND NOT EXISTS(SELECT 1 FROM context_items c "
        "WHERE c.kind='resource_inventory' AND c.item_key='document:'||p.document_id)))"
    ).fetchone()[0]
    total = sum(states.values())
    if not total or (states["unavailable"] and not states["complete"] and not states["partial"]):
        refs = {"state": "unavailable", "reason": "resource declarations were not measured"}
    elif missing or states["partial"] or states["unavailable"]:
        refs = {
            "state": "partial",
            "reason": "some resource declarations were not measured or were omitted",
        }
    else:
        refs = {"state": "complete", "reason": ""}
    count, measured = con.execute(
        "SELECT COUNT(*),COALESCE(SUM(capture_state='measured'),0) FROM resource_refs"
    ).fetchone()
    if not fetch_enabled:
        bodies = {"state": "unavailable", "reason": "resources.fetch disabled"}
    elif refs["state"] == "complete" and count == measured:
        bodies = {"state": "complete", "reason": ""}
    else:
        reasons = [
            row[0]
            for row in con.execute(
                "SELECT DISTINCT capture_state FROM resource_refs WHERE capture_state!='measured' ORDER BY capture_state"
            )
        ]
        bodies = {
            "state": "partial" if measured else "unavailable",
            "reason": ", ".join(reasons) or "resource inventory is incomplete",
        }
    return {"resource_refs": refs, "resource_bodies": bodies}
