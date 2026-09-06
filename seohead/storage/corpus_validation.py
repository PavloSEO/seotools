"""Read-only integrity validation for the retained scan corpus lanes."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from . import ScanError
from .bodies import read_body

_SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization"}
_OMITTED = {
    "not_enabled",
    "cache_control_no_store",
    "credentialed",
    "unsupported_media",
    "body_budget_exhausted",
    "resource_budget_exhausted",
}
_UNAVAILABLE = {"not_fetched", "not_in_corpus", "legacy_not_retained", "fetch_failed"}


def _iter_rows(con: sqlite3.Connection, sql: str) -> Iterator[dict[str, Any]]:
    cursor = con.execute(sql)
    names = [column[0] for column in cursor.description or ()]
    for row in cursor:
        yield dict(zip(names, row, strict=True))


def _json(value: Any, label: str) -> Any:
    if not isinstance(value, str):
        raise ScanError(f"{label} must be JSON text")
    try:
        return json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ScanError(f"{label} is invalid JSON") from exc


def _timestamp(value: Any, label: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str):
        raise ScanError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScanError(f"{label} is not an RFC3339 timestamp") from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ScanError(f"{label} must be UTC")


def _headers(value: Any, label: str) -> None:
    pairs = _json(value, label)
    if not isinstance(pairs, list):
        raise ScanError(f"{label} must be an ordered header-pair list")
    for pair in pairs:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(type(part) is not str for part in pair)
            or pair[0].lower() in _SENSITIVE_HEADERS
        ):
            raise ScanError(f"{label} contains an unredacted or malformed header")


def _has_no_store(value: Any) -> bool:
    from seohead.crawl.cache import _parse_cache_control

    return any(
        pair[0].lower() == "cache-control" and "no-store" in _parse_cache_control(pair[1])
        for pair in _json(value, "response headers")
        if isinstance(pair, list) and len(pair) == 2 and all(type(part) is str for part in pair)
    )


def _state(row: Mapping[str, Any], *, document: bool) -> None:
    state, reason, sha, fidelity = (
        row["body_state"],
        row["body_reason"],
        row["body_sha256"],
        row["fidelity" if document else "body_fidelity"],
    )
    if state == "complete":
        allowed = (
            {"entity_bytes", "reencoded_text", "serialized_dom"}
            if document
            else {
                "entity_bytes",
                "reencoded_text",
            }
        )
        if (
            sha is None
            or reason not in {"none", "preexisting_cache_snapshot"}
            or fidelity not in allowed
        ):
            raise ScanError("complete corpus row has invalid hash, reason, or fidelity")
    elif state == "truncated":
        if sha is not None or reason != "truncated" or fidelity != "unavailable":
            raise ScanError("truncated corpus row must retain no body")
    elif state == "omitted":
        if sha is not None or reason not in _OMITTED or fidelity != "unavailable":
            raise ScanError("omitted corpus row has an invalid reason or body")
    elif state == "unavailable":
        if sha is not None or reason not in _UNAVAILABLE or fidelity != "unavailable":
            raise ScanError("unavailable corpus row has an invalid reason or body")
    else:
        raise ScanError("unknown corpus body state")


def validate_corpus(
    con: sqlite3.Connection,
    scan: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    verify_bodies: bool = True,
) -> None:
    """Validate body/response/document lineage without mutating the artifact."""
    if not isinstance(con, sqlite3.Connection):
        raise ScanError("corpus validator requires a sqlite3 connection")
    if not isinstance(scan, Mapping) or not isinstance(policy, Mapping):
        raise ScanError("corpus validator requires scan and policy metadata")
    if (
        con.execute("SELECT 1 FROM resource_refs LIMIT 1").fetchone() is not None
        or con.execute(
            "SELECT 1 FROM context_items WHERE kind='resource_inventory' LIMIT 1"
        ).fetchone()
    ):
        from .resources import validate_resources

        validate_resources(con)
    mode = policy.get("body_mode")
    cap = policy.get("max_body_bytes", 0)
    if mode not in {"off", "captured_entity_bytes"} or type(cap) is not int or cap < 0:
        raise ScanError("corpus body policy is invalid")
    if mode == "off" and con.execute("SELECT 1 FROM bodies LIMIT 1").fetchone():
        raise ScanError("off body policy cannot retain body BLOBs")
    stored_total = con.execute("SELECT COALESCE(SUM(stored_bytes),0) FROM bodies").fetchone()[0]
    if "max_body_store_bytes" in policy and stored_total > policy["max_body_store_bytes"]:
        raise ScanError("retained body bytes exceed the recorded store budget")
    for body in _iter_rows(
        con,
        "SELECT sha256,codec,decoded_bytes,stored_bytes,length(data) AS actual_size FROM bodies",
    ):
        if (
            body["codec"] not in {"identity", "zlib"}
            or type(body["decoded_bytes"]) is not int
            or body["decoded_bytes"] < 0
            or body["decoded_bytes"] > cap
            or body["stored_bytes"] != body["actual_size"]
        ):
            raise ScanError("body metadata exceeds policy or disagrees with stored size")
        if verify_bodies:
            raw = read_body(con, body["sha256"], max_decoded_bytes=min(cap, 64 * 1024 * 1024))
            if con.execute(
                "SELECT 1 FROM documents WHERE body_sha256=? AND fidelity='serialized_dom' LIMIT 1",
                (body["sha256"],),
            ).fetchone():
                try:
                    raw.decode("utf-8", errors="strict")
                except UnicodeError as exc:
                    raise ScanError("serialized DOM contains invalid UTF-8") from exc

    for row in _iter_rows(con, "SELECT * FROM responses"):
        _timestamp(row["requested_at"], "response requested_at")
        _timestamp(row["received_at"], "response received_at", nullable=True)
        _headers(row["request_headers_redacted_json"], "request headers")
        _headers(row["response_headers_redacted_json"], "response headers")
        _headers(row["effective_headers_redacted_json"], "effective headers")
        if not isinstance(row["variant_key"], str) or not row["variant_key"]:
            raise ScanError("response variant key must be a nonempty opaque string")
        if row["transport_source"] == "cache" and scan.get("source_kind") in {
            "native",
            "reanalysis",
        }:
            raise ScanError("native corpus response cannot claim legacy cache transport")
        _state(row, document=False)
        if row["body_state"] == "complete" and (
            row["credentials_used"] != 0
            or _has_no_store(row["response_headers_redacted_json"])
            or _has_no_store(row["effective_headers_redacted_json"])
        ):
            raise ScanError("credentialed or no-store response cannot retain a complete body")
        source = row["source_response_id"]
        if row["status_code"] == 304 and row["body_state"] == "complete" and source is None:
            raise ScanError("complete 304 response requires a source response")
        if source is not None:
            cursor = con.execute("SELECT * FROM responses WHERE response_id=?", (source,))
            item = cursor.fetchone()
            names = [column[0] for column in cursor.description or ()]
            previous = dict(zip(names, item, strict=True)) if item is not None else None
            if (
                previous is None
                or previous["request_ordinal"] >= row["request_ordinal"]
                or row["status_code"] != 304
                or previous["body_sha256"] != row["body_sha256"]
                or previous["request_url_id"] != row["request_url_id"]
                or previous["variant_key"] != row["variant_key"]
                or previous["method"] != row["method"]
                or (
                    row["body_state"] == "complete"
                    and (
                        previous["body_state"] != "complete"
                        or previous["body_fidelity"] != row["body_fidelity"]
                        or previous["effective_url_id"] != row["effective_url_id"]
                        or previous["effective_status_code"] != row["effective_status_code"]
                    )
                )
            ):
                raise ScanError("304 response source ordering or body lineage disagrees")
        chain = _json(row["redirect_chain_json"], "response redirect chain")
        if not isinstance(chain, list):
            raise ScanError("response redirect chain must be an ordered list")
        current = row["request_url_id"]
        for hop in chain:
            if (
                not isinstance(hop, dict)
                or set(hop)
                != {"request_url_id", "status_code", "location_raw", "next_url_id", "blocked"}
                or hop["request_url_id"] != current
                or type(hop["status_code"]) is not int
                or type(hop["location_raw"]) is not str
                or type(hop["blocked"]) is not bool
            ):
                raise ScanError("response redirect hop is malformed or disconnected")
            if hop["status_code"] not in {301, 302, 303, 307, 308}:
                raise ScanError("redirect hop status is not an HTTP redirect")
            if hop["blocked"]:
                if hop["next_url_id"] is not None:
                    raise ScanError("blocked redirect cannot name a next URL")
                current = None
                break
            if type(hop["next_url_id"]) is not int:
                raise ScanError("redirect next URL is invalid")
            request_url = con.execute(
                "SELECT url FROM urls WHERE url_id=?", (hop["request_url_id"],)
            ).fetchone()
            next_url = con.execute(
                "SELECT url FROM urls WHERE url_id=?", (hop["next_url_id"],)
            ).fetchone()
            if (
                request_url is None
                or next_url is None
                or urljoin(request_url[0], hop["location_raw"]) != next_url[0]
            ):
                raise ScanError("redirect hop location does not reproduce its next URL")
            current = hop["next_url_id"]
        if row["body_state"] == "complete" and current != row["effective_url_id"]:
            raise ScanError("complete response effective URL disagrees with redirect chain")

    for row in _iter_rows(con, "SELECT * FROM documents"):
        _timestamp(row["captured_at"], "document captured_at")
        _state(row, document=True)
        from .bodies import _renderer as validate_renderer
        from .bodies import decode_entity

        renderer = validate_renderer(row, con)
        if row["body_state"] != "complete":
            expected_decoder = ("scan_decoder.v1", "not_applicable", "unknown", "not_applicable")
        elif row["fidelity"] == "serialized_dom":
            expected_decoder = ("scan_decoder.v1", "renderer_utf8", "utf-8", "not_applicable")
        elif row["fidelity"] == "reencoded_text":
            expected_decoder = ("scan_decoder.v1", "legacy_unknown", "unknown", "unknown")
        else:
            response_type = con.execute(
                "SELECT content_type FROM responses WHERE response_id=?",
                (row["source_response_id"],),
            ).fetchone()
            if response_type is None:
                raise ScanError("entity document has no source response")
            _, decoder = decode_entity(b"", response_type[0])
            expected_decoder = tuple(decoder.values())
        if (
            tuple(
                row[name]
                for name in (
                    "decoder_version",
                    "decoder_source",
                    "decoder_charset",
                    "decoder_errors",
                )
            )
            != expected_decoder
        ):
            raise ScanError("document decoder metadata is inconsistent with its fidelity")
        if row["source_response_id"] is not None and renderer:
            navigation = con.execute(
                "SELECT request_url_id,effective_url_id FROM responses WHERE response_id=?",
                (row["source_response_id"],),
            ).fetchone()
            if (
                navigation is None
                or navigation[0] != renderer["navigation_url_id"]
                or (row["body_state"] == "complete" and navigation[1] != renderer["final_url_id"])
            ):
                raise ScanError("renderer navigation disagrees with its source response")
        if (
            row["fidelity"] in {"entity_bytes", "reencoded_text"}
            and row["body_state"] == "complete"
        ):
            expected_request_url = row["url_id"]
            if row["representation"] == "legacy_fragment":
                renderer = _json(row["renderer_json"], "legacy fragment renderer")
                expected_request_url = renderer["navigation_url_id"]
                if type(expected_request_url) is not int:
                    raise ScanError("legacy fragment navigation URL provenance is invalid")
            cursor = con.execute(
                "SELECT * FROM responses WHERE response_id=?", (row["source_response_id"],)
            )
            item = cursor.fetchone()
            names = [column[0] for column in cursor.description or ()]
            response = dict(zip(names, item, strict=True)) if item is not None else None
            if (
                response is None
                or response["request_url_id"] != expected_request_url
                or response["body_sha256"] != row["body_sha256"]
                or response["body_state"] != "complete"
            ):
                raise ScanError("document response URL, state, or hash lineage disagrees")
    for page in _iter_rows(con, "SELECT * FROM pages"):
        if page["document_id"] is not None:
            cursor = con.execute(
                "SELECT * FROM documents WHERE document_id=?", (page["document_id"],)
            )
            item = cursor.fetchone()
            names = [column[0] for column in cursor.description or ()]
            document = dict(zip(names, item, strict=True)) if item is not None else None
            if (
                document is None
                or document["url_id"] != page["url_id"]
                or document["representation"] != page["representation"]
            ):
                raise ScanError("selected page document URL or representation disagrees")
    for table, page_column, representation_column in (
        ("links", "source_url_id", "evidence_representation"),
        ("forms", "page_url_id", "evidence_representation"),
    ):
        for row in _iter_rows(con, f"SELECT * FROM {table}"):
            if row["source_document_id"] is not None:
                cursor = con.execute(
                    "SELECT * FROM documents WHERE document_id=?", (row["source_document_id"],)
                )
                item = cursor.fetchone()
                names = [column[0] for column in cursor.description or ()]
                document = dict(zip(names, item, strict=True)) if item is not None else None
                if (
                    document is None
                    or document["url_id"] != row[page_column]
                    or document["representation"] != row[representation_column]
                ):
                    raise ScanError(f"{table} source document representation disagrees")
