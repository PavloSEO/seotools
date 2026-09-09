"""Versioned, body-free evidence derived from one captured HTML representation.

The native scan retains bodies under its own policy.  This module deliberately
stores only reproducible content facts and their source document identity: a
content-area strategy, implementation identity, hashes and a SimHash
fingerprint.  It never makes a network request and never exposes extracted
body text in a report-facing context item.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import ScanError

KIND = "content_evidence"
VERSION = "content_evidence.v1"
OUTER_VERSION = "scan_context.v1"
REPRESENTATIONS = {"static", "rendered", "legacy_fragment"}
STATES = {"complete", "empty", "unavailable"}


def _implementation() -> dict[str, Any]:
    """Identify the exact extraction implementation without serializing a body."""
    root = Path(__file__).resolve().parents[1]
    names = [
        "storage/content_evidence.py",
        "tools/content_area.py",
        "tools/markdown_extract.py",
        "tools/duplicate.py",
        "tools/boilerplate_report.py",
    ]
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update((root / name).read_bytes())
    return {"version": VERSION, "source_sha256": digest.hexdigest(), "files": names}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def capture_document(
    *,
    page_url_id: int,
    source_document_id: int,
    representation: str,
    html: str | None,
    parsed: dict[str, Any] | None,
    content_area: dict[str, Any] | None,
    unavailable_reason: str = "",
) -> dict[str, Any]:
    """Build one closed content-evidence payload for a captured document.

    ``parsed`` is the parser result from the same representation.  A missing
    body/parser is an explicit unavailable state; it is never represented as an
    empty page.  Empty extracted main content is separately measured.
    """
    if type(page_url_id) is not int or page_url_id < 1:
        raise ValueError("content evidence page_url_id must be positive")
    if type(source_document_id) is not int or source_document_id < 1:
        raise ValueError("content evidence source_document_id must be positive")
    if representation not in REPRESENTATIONS:
        raise ValueError("content evidence has unsupported representation")
    if unavailable_reason or not isinstance(html, str) or not isinstance(parsed, dict):
        return {
            "schema_version": VERSION,
            "page_url_id": page_url_id,
            "source_document_id": source_document_id,
            "representation": representation,
            "state": "unavailable",
            "reason": unavailable_reason or "captured document was not parsed as eligible HTML",
            "strategy": None,
            "implementation": _implementation(),
            "exact_hash": None,
            "normalized_hash": None,
            "boilerplate_hash": None,
            "simhash": None,
        }

    from seohead.tools.boilerplate_report import boilerplate_hash
    from seohead.tools.duplicate import simhash
    from seohead.tools.markdown_extract import extract_markdown

    extracted = extract_markdown(html, content_area)
    content = str(extracted["content_markdown"])
    normalized = _normalized(content)
    state = "empty" if not normalized else "complete"
    return {
        "schema_version": VERSION,
        "page_url_id": page_url_id,
        "source_document_id": source_document_id,
        "representation": representation,
        "state": state,
        "reason": "main content area is empty" if state == "empty" else "",
        "strategy": str(parsed.get("content_area_strategy") or extracted["content_area_strategy"]),
        "implementation": _implementation(),
        "exact_hash": _sha(content),
        "normalized_hash": _sha(normalized),
        "boilerplate_hash": boilerplate_hash(html),
        "simhash": f"{simhash(content):016x}",
    }


def context_item(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap one closed evidence payload for the existing native context writer."""
    validate_payload(payload)
    return {
        "kind": KIND,
        "item_key": (
            f"page:{payload['page_url_id']}:document:{payload['source_document_id']}:"
            f"representation:{payload['representation']}"
        ),
        "payload_version": OUTER_VERSION,
        "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        "completeness": "complete" if payload["state"] in {"complete", "empty"} else "unavailable",
        "reason": payload["reason"],
    }


def validate_payload(payload: Any) -> None:
    required = {
        "schema_version",
        "page_url_id",
        "source_document_id",
        "representation",
        "state",
        "reason",
        "strategy",
        "implementation",
        "exact_hash",
        "normalized_hash",
        "boilerplate_hash",
        "simhash",
    }
    if not isinstance(payload, dict) or set(payload) != required or payload["schema_version"] != VERSION:
        raise ScanError("content evidence payload has unsupported fields or version")
    if type(payload["page_url_id"]) is not int or payload["page_url_id"] < 1:
        raise ScanError("content evidence page identity is invalid")
    if type(payload["source_document_id"]) is not int or payload["source_document_id"] < 1:
        raise ScanError("content evidence source document identity is invalid")
    if payload["representation"] not in REPRESENTATIONS or payload["state"] not in STATES:
        raise ScanError("content evidence representation or state is invalid")
    if not isinstance(payload["reason"], str):
        raise ScanError("content evidence reason is invalid")
    if payload["state"] == "unavailable":
        if payload["reason"] == "" or any(payload[key] is not None for key in (
            "strategy", "exact_hash", "normalized_hash", "boilerplate_hash", "simhash"
        )):
            raise ScanError("unavailable content evidence must name a reason and no hashes")
    else:
        if not isinstance(payload["strategy"], str) or not payload["strategy"]:
            raise ScanError("content evidence strategy is invalid")
        if payload["reason"] and payload["state"] != "empty":
            raise ScanError("complete content evidence must not carry a reason")
        for key, length in (("exact_hash", 64), ("normalized_hash", 64), ("boilerplate_hash", 64), ("simhash", 16)):
            value = payload[key]
            if type(value) is not str or len(value) != length or any(c not in "0123456789abcdef" for c in value):
                raise ScanError(f"content evidence {key} is invalid")
    implementation = payload["implementation"]
    if (
        not isinstance(implementation, dict)
        or set(implementation) != {"version", "source_sha256", "files"}
        or implementation["version"] != VERSION
        or not isinstance(implementation["source_sha256"], str)
        or len(implementation["source_sha256"]) != 64
        or not isinstance(implementation["files"], list)
    ):
        raise ScanError("content evidence implementation identity is invalid")


def validate_context(con: Any, item: dict[str, Any], payload: Any) -> None:
    """Validate one persisted context plus its page/document representation binding."""
    validate_payload(payload)
    if item["kind"] != KIND or item["payload_version"] != OUTER_VERSION:
        raise ScanError("content evidence context kind or version is invalid")
    if item["item_key"] != (
        f"page:{payload['page_url_id']}:document:{payload['source_document_id']}:"
        f"representation:{payload['representation']}"
    ):
        raise ScanError("content evidence context key is invalid")
    expected = "complete" if payload["state"] in {"complete", "empty"} else "unavailable"
    if item["completeness"] != expected or item["reason"] != payload["reason"]:
        raise ScanError("content evidence context completeness is invalid")
    if not con.execute(
        "SELECT 1 FROM documents WHERE document_id=? AND url_id=? AND representation=?",
        (payload["source_document_id"], payload["page_url_id"], payload["representation"]),
    ).fetchone():
        raise ScanError("content evidence context references the wrong page or representation")


def read(con: Any) -> dict[str, Any]:
    """Read stored evidence without opening bodies or recalculating hashes."""
    rows = []
    for row in con.execute("SELECT * FROM context_items WHERE kind=? ORDER BY item_key", (KIND,)):
        payload = json.loads(row["payload_json"])
        rows.append(payload)
    return {
        "schema_version": VERSION,
        "items": rows,
        "coverage": (
            {"state": "complete", "observed": len(rows)}
            if rows
            else {"state": "unavailable", "reason": "content evidence was not stored in this scan"}
        ),
    }
