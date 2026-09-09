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
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import ScanError

KIND = "content_evidence"
VERSION = "content_evidence.v2"
LEGACY_VERSION = "content_evidence.v1"
OUTER_VERSION = "scan_context.v1"
REPRESENTATIONS = {"static", "rendered", "legacy_fragment"}
STATES = {"complete", "empty", "unavailable"}
MIN_DUPLICATE_TOKENS = 3
MAX_DUPLICATE_CANDIDATES = 10_000
MAX_DUPLICATE_PAIRS = 50_000


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


def _token_count(value: str) -> int:
    return len(re.findall(r"\w{2,}", value, flags=re.UNICODE))


def _url_identity(value: str) -> tuple[str, str, str, str] | None:
    """Return the URL identity relevant to a self-canonical comparison."""
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return None
    return (
        parsed.scheme.casefold(),
        parsed.netloc.casefold(),
        parsed.path.rstrip("/") or "/",
        parsed.query,
    )


def capture_document(
    *,
    page_url_id: int,
    source_document_id: int,
    representation: str,
    html: str | None,
    parsed: dict[str, Any] | None,
    content_area: dict[str, Any] | None,
    indexable: bool | None = None,
    canonical_target: str = "",
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
            "indexable": indexable,
            "canonical_target": canonical_target,
            "strategy": None,
            "implementation": _implementation(),
            "exact_hash": None,
            "normalized_hash": None,
            "boilerplate_hash": None,
            "simhash": None,
            "content_tokens": None,
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
        "indexable": indexable,
        "canonical_target": canonical_target,
        "strategy": str(parsed.get("content_area_strategy") or extracted["content_area_strategy"]),
        "implementation": _implementation(),
        "exact_hash": _sha(content),
        "normalized_hash": _sha(normalized),
        "boilerplate_hash": boilerplate_hash(html),
        "simhash": f"{simhash(content):016x}",
        "content_tokens": _token_count(content),
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
        "indexable",
        "canonical_target",
        "strategy",
        "implementation",
        "exact_hash",
        "normalized_hash",
        "boilerplate_hash",
        "simhash",
        "content_tokens",
    }
    if not isinstance(payload, dict) or payload.get("schema_version") not in {
        VERSION,
        LEGACY_VERSION,
    }:
        raise ScanError("content evidence payload has unsupported fields or version")
    if payload["schema_version"] == LEGACY_VERSION:
        required.remove("content_tokens")
    if set(payload) != required:
        raise ScanError("content evidence payload has unsupported fields")
    if type(payload["page_url_id"]) is not int or payload["page_url_id"] < 1:
        raise ScanError("content evidence page identity is invalid")
    if type(payload["source_document_id"]) is not int or payload["source_document_id"] < 1:
        raise ScanError("content evidence source document identity is invalid")
    if payload["representation"] not in REPRESENTATIONS or payload["state"] not in STATES:
        raise ScanError("content evidence representation or state is invalid")
    if not isinstance(payload["reason"], str):
        raise ScanError("content evidence reason is invalid")
    if payload["indexable"] is not None and type(payload["indexable"]) is not bool:
        raise ScanError("content evidence indexability is invalid")
    if not isinstance(payload["canonical_target"], str):
        raise ScanError("content evidence canonical target is invalid")
    if payload["state"] == "unavailable":
        unavailable_keys = (
            "strategy",
            "exact_hash",
            "normalized_hash",
            "boilerplate_hash",
            "simhash",
        )
        if payload["schema_version"] == VERSION:
            unavailable_keys += ("content_tokens",)
        if payload["reason"] == "" or any(payload[key] is not None for key in unavailable_keys):
            raise ScanError("unavailable content evidence must name a reason and no hashes")
    else:
        if not isinstance(payload["strategy"], str) or not payload["strategy"]:
            raise ScanError("content evidence strategy is invalid")
        if payload["reason"] and payload["state"] != "empty":
            raise ScanError("complete content evidence must not carry a reason")
        for key, length in (
            ("exact_hash", 64),
            ("normalized_hash", 64),
            ("boilerplate_hash", 64),
            ("simhash", 16),
        ):
            value = payload[key]
            if (
                type(value) is not str
                or len(value) != length
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise ScanError(f"content evidence {key} is invalid")
        if payload["schema_version"] == VERSION and (
            type(payload["content_tokens"]) is not int or payload["content_tokens"] < 0
        ):
            raise ScanError("content evidence token count is invalid")
    implementation = payload["implementation"]
    if (
        not isinstance(implementation, dict)
        or set(implementation) != {"version", "source_sha256", "files"}
        or implementation["version"] != payload["schema_version"]
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


def derive_duplicates(
    items: list[dict[str, Any]],
    *,
    threshold: float = 0.92,
    include_nonindexable: bool = False,
    page_urls: dict[int, str] | None = None,
    min_tokens: int = MIN_DUPLICATE_TOKENS,
    max_candidates: int = MAX_DUPLICATE_CANDIDATES,
    max_pairs: int = MAX_DUPLICATE_PAIRS,
) -> dict[str, Any]:
    """Derive reproducible duplicate witnesses from stored hashes/fingerprints only.

    The output deliberately names filters and every excluded population.  It
    does not fetch bodies, recalculate extraction, or turn unavailable evidence
    into a clean non-duplicate result.
    """
    if not isinstance(threshold, float) or not 0.0 <= threshold <= 1.0:
        raise ValueError("duplicate threshold must be a float from 0 to 1")
    if type(min_tokens) is not int or min_tokens < 1:
        raise ValueError("duplicate minimum token count must be positive")
    if (
        type(max_candidates) is not int
        or max_candidates < 1
        or type(max_pairs) is not int
        or max_pairs < 1
    ):
        raise ValueError("duplicate analysis limits must be positive integers")
    eligible, excluded, partial_reasons = [], [], []
    for item in sorted(
        items, key=lambda row: (row.get("page_url_id") or 0, row.get("source_document_id") or 0)
    ):
        page_url_id = item.get("page_url_id")
        if item.get("state") == "empty":
            excluded.append({"page_url_id": page_url_id, "reason": "main content is empty"})
        elif item.get("state") != "complete":
            excluded.append({"page_url_id": item.get("page_url_id"), "reason": item.get("reason")})
        elif type(item.get("content_tokens")) is not int:
            excluded.append({"page_url_id": page_url_id, "reason": "content length is unavailable"})
        elif item["content_tokens"] < min_tokens:
            excluded.append({"page_url_id": page_url_id, "reason": "main content is too short"})
        elif not include_nonindexable and item.get("indexable") is not True:
            reason = (
                "non-indexable" if item.get("indexable") is False else "indexability is unmeasured"
            )
            excluded.append({"page_url_id": page_url_id, "reason": reason})
        else:
            canonical = item.get("canonical_target")
            source_url = (
                item.get("page_url")
                if isinstance(item.get("page_url"), str)
                else (page_urls or {}).get(page_url_id)
            )
            if canonical:
                source_identity, canonical_identity = (
                    _url_identity(source_url),
                    _url_identity(canonical),
                )
                if source_identity is None or canonical_identity is None:
                    excluded.append(
                        {
                            "page_url_id": page_url_id,
                            "reason": "canonical target identity is unknown",
                        }
                    )
                elif source_identity != canonical_identity:
                    excluded.append(
                        {"page_url_id": page_url_id, "reason": "canonicalized to another target"}
                    )
                else:
                    eligible.append(item)
            else:
                eligible.append(item)
    if len(eligible) > max_candidates:
        for item in eligible[max_candidates:]:
            excluded.append(
                {"page_url_id": item["page_url_id"], "reason": "duplicate candidate cap reached"}
            )
        eligible = eligible[:max_candidates]
        partial_reasons.append(
            "candidate cap reached before all eligible content could be compared"
        )
    exact: dict[tuple[str, str, str, str, str], set[int]] = {}
    for item in eligible:
        implementation = item["implementation"]
        key = (
            item["representation"],
            item["strategy"],
            implementation["version"],
            implementation["source_sha256"],
        )
        exact.setdefault(key + (item["normalized_hash"],), set()).add(item["page_url_id"])
    exact_groups = [
        {
            "representation": key[0],
            "strategy": key[1],
            "implementation_version": key[2],
            "implementation_source_sha256": key[3],
            "normalized_hash": key[4],
            "pages": sorted(pages),
        }
        for key, pages in sorted(exact.items())
        if len(pages) > 1
    ]
    by_identity: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for item in eligible:
        implementation = item["implementation"]
        by_identity.setdefault(
            (
                item["representation"],
                item["strategy"],
                implementation["version"],
                implementation["source_sha256"],
            ),
            [],
        ).append(item)
    witnesses, pairs_considered, capped = [], 0, False
    for identity in sorted(by_identity):
        candidates = by_identity[identity]
        for index, left in enumerate(candidates):
            for right in candidates[index + 1 :]:
                if pairs_considered >= max_pairs:
                    capped = True
                    break
                pairs_considered += 1
                if left["page_url_id"] == right["page_url_id"]:
                    continue
                if left["normalized_hash"] == right["normalized_hash"]:
                    continue
                distance = (int(left["simhash"], 16) ^ int(right["simhash"], 16)).bit_count()
                similarity = 1.0 - distance / 64.0
                if similarity >= threshold:
                    witnesses.append(
                        {
                            "left_page_url_id": left["page_url_id"],
                            "left_source_document_id": left["source_document_id"],
                            "right_page_url_id": right["page_url_id"],
                            "right_source_document_id": right["source_document_id"],
                            "similarity": similarity,
                            "representation": left["representation"],
                            "strategy": left["strategy"],
                        }
                    )
            if capped:
                break
        if capped:
            break
    if capped:
        partial_reasons.append("near-duplicate pair comparison cap reached")
    return {
        "schema_version": "content_duplicate_derivation.v2",
        "settings": {
            "threshold": threshold,
            "include_nonindexable": include_nonindexable,
            "minimum_tokens": min_tokens,
            "identity": "normalized_hash and simhash within representation, strategy and implementation identity",
            "max_candidates": max_candidates,
            "max_pairs": max_pairs,
        },
        "exact_groups": exact_groups,
        "near_witnesses": sorted(
            witnesses,
            key=lambda row: (-row["similarity"], row["left_page_url_id"], row["right_page_url_id"]),
        ),
        "excluded": excluded,
        "partial": bool(partial_reasons),
        "partial_reasons": partial_reasons,
        "comparisons": {
            "eligible_candidates": len(eligible),
            "pairs_considered": pairs_considered,
            "pair_cap_reached": capped,
        },
    }
