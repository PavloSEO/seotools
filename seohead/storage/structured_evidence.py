"""Versioned structured-data and language-declaration evidence from one document."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import ScanError

OUTER_VERSION = "scan_context.v1"
STRUCTURED_KIND = "structured_evidence"
LANGUAGE_KIND = "language_evidence"
STRUCTURED_VERSION = "structured_evidence.v1"
LANGUAGE_VERSION = "language_evidence.v1"
REPRESENTATIONS = {"static", "rendered", "legacy_fragment"}


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _nodes(value: Any) -> list[dict[str, Any]]:
    stack, result = [value], []
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            result.append(current)
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))
    return result


def structured_payload(
    *, page_url_id: int, source_document_id: int, representation: str, parsed: dict[str, Any] | None
) -> dict[str, Any]:
    """Capture block hashes, syntax state, graph nodes and @id edges without excerpts."""
    if type(page_url_id) is not int or type(source_document_id) is not int or representation not in REPRESENTATIONS:
        raise ValueError("structured evidence identity is invalid")
    if not isinstance(parsed, dict):
        return {
            "schema_version": STRUCTURED_VERSION, "page_url_id": page_url_id,
            "source_document_id": source_document_id, "representation": representation,
            "state": "unavailable", "reason": "captured document was not parsed", "blocks": [],
            "nodes": [], "edges": [],
        }
    valid = list(parsed.get("jsonld") or [])
    invalid = list(parsed.get("jsonld_invalid") or [])
    blocks, nodes, edges = [], [], []
    for ordinal, block in enumerate(valid):
        blocks.append({"ordinal": ordinal, "state": "valid", "hash": _hash(block)})
        for node_ordinal, node in enumerate(_nodes(block)):
            node_id = node.get("@id") if isinstance(node.get("@id"), str) else None
            types = node.get("@type")
            type_values = [types] if isinstance(types, str) else sorted(t for t in types if isinstance(t, str)) if isinstance(types, list) else []
            nodes.append({"block_ordinal": ordinal, "ordinal": node_ordinal, "id": node_id, "types": type_values})
            for property_name, value in node.items():
                values = value if isinstance(value, list) else [value]
                for target in values:
                    if isinstance(target, dict) and isinstance(target.get("@id"), str):
                        edges.append({"block_ordinal": ordinal, "source_id": node_id, "property": property_name, "target_id": target["@id"]})
    for offset, item in enumerate(invalid, start=len(valid)):
        blocks.append({"ordinal": offset, "state": "malformed", "hash": _hash(item)})
    state = "malformed" if invalid else "eligible" if valid else "absent"
    return {
        "schema_version": STRUCTURED_VERSION, "page_url_id": page_url_id,
        "source_document_id": source_document_id, "representation": representation,
        "state": state, "reason": "JSON-LD syntax failures captured" if invalid else "",
        "blocks": blocks, "nodes": nodes, "edges": edges,
    }


def language_payload(
    *, page_url_id: int, source_document_id: int, representation: str, parsed: dict[str, Any] | None
) -> dict[str, Any]:
    """Capture raw/resolved hreflang declarations as directed source evidence."""
    if type(page_url_id) is not int or type(source_document_id) is not int or representation not in REPRESENTATIONS:
        raise ValueError("language evidence identity is invalid")
    if not isinstance(parsed, dict):
        return {
            "schema_version": LANGUAGE_VERSION, "page_url_id": page_url_id,
            "source_document_id": source_document_id, "representation": representation,
            "state": "unavailable", "reason": "captured document was not parsed", "html_lang": None,
            "declarations": [],
        }
    declarations = []
    for ordinal, alternate in enumerate(parsed.get("hreflang") or []):
        if not isinstance(alternate, dict):
            continue
        declarations.append({
            "ordinal": ordinal, "lang": str(alternate.get("lang") or ""),
            "raw_href": str(alternate.get("raw_href") or ""), "target": str(alternate.get("url") or ""),
            "state": "declared" if alternate.get("url") else "malformed",
        })
    return {
        "schema_version": LANGUAGE_VERSION, "page_url_id": page_url_id,
        "source_document_id": source_document_id, "representation": representation,
        "state": "declared" if declarations else "absent", "reason": "",
        "html_lang": parsed.get("html_lang"), "declarations": declarations,
    }


def context_items(structured: dict[str, Any], language: dict[str, Any]) -> list[dict[str, Any]]:
    """Return closed context envelopes for the existing atomic writer hook."""
    return [_item(STRUCTURED_KIND, structured), _item(LANGUAGE_KIND, language)]


def _item(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "item_key": f"page:{payload['page_url_id']}:document:{payload['source_document_id']}:representation:{payload['representation']}",
        "payload_version": OUTER_VERSION,
        "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        "completeness": "unavailable" if payload["state"] == "unavailable" else "complete",
        "reason": payload["reason"],
    }


def validate_context(con: Any, item: dict[str, Any], payload: Any) -> None:
    """Validate closed payload shape and exact document/page/representation binding."""
    kind = item.get("kind")
    version = STRUCTURED_VERSION if kind == STRUCTURED_KIND else LANGUAGE_VERSION if kind == LANGUAGE_KIND else None
    if version is None or item.get("payload_version") != OUTER_VERSION or not isinstance(payload, dict):
        raise ScanError("structured or language evidence context is invalid")
    required = {"schema_version", "page_url_id", "source_document_id", "representation", "state", "reason"}
    required |= {"blocks", "nodes", "edges"} if kind == STRUCTURED_KIND else {"html_lang", "declarations"}
    if set(payload) != required or payload["schema_version"] != version:
        raise ScanError("structured or language evidence payload has unsupported fields")
    if type(payload["page_url_id"]) is not int or type(payload["source_document_id"]) is not int or payload["representation"] not in REPRESENTATIONS:
        raise ScanError("structured or language evidence identity is invalid")
    if payload["state"] not in {"eligible", "malformed", "absent", "declared", "unavailable"} or not isinstance(payload["reason"], str):
        raise ScanError("structured or language evidence state is invalid")
    if item["item_key"] != f"page:{payload['page_url_id']}:document:{payload['source_document_id']}:representation:{payload['representation']}":
        raise ScanError("structured or language evidence key is invalid")
    if not con.execute("SELECT 1 FROM documents WHERE document_id=? AND url_id=? AND representation=?", (payload["source_document_id"], payload["page_url_id"], payload["representation"])).fetchone():
        raise ScanError("structured or language evidence binds the wrong document")


def read(con: Any) -> dict[str, Any]:
    """Read typed saved evidence; target observations remain explicitly unmeasured here."""
    output = {"structured": [], "language": []}
    for row in con.execute("SELECT * FROM context_items WHERE kind IN (?,?) ORDER BY kind,item_key", (STRUCTURED_KIND, LANGUAGE_KIND)):
        output["structured" if row["kind"] == STRUCTURED_KIND else "language"].append(json.loads(row["payload_json"]))
    return output
