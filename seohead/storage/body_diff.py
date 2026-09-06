"""Bounded, offline comparison of retained scan body observations."""

from __future__ import annotations

import difflib
import json
import sqlite3
from typing import Any

from . import ScanError
from .bodies import decode_entity, read_body

_MAX_BYTES = 64 * 1024 * 1024
_MAX_LINES = 1_000_000
_REPRESENTATIONS = {"static", "rendered", "legacy_fragment"}


def _connection(value: Any) -> sqlite3.Connection:
    con = value if isinstance(value, sqlite3.Connection) else getattr(value, "con", None)
    if not isinstance(con, sqlite3.Connection):
        raise TypeError("body diff requires sqlite3 connections or open scan readers")
    return con


def _rows(con: sqlite3.Connection, sql: str, values: tuple[Any, ...]) -> list[dict[str, Any]]:
    cursor = con.execute(sql, values)
    names = [column[0] for column in cursor.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cursor]


def _observation(
    con: sqlite3.Connection,
    url: str,
    representation: str,
    variant_key: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    identity = (url, representation)
    variants = _rows(
        con,
        "SELECT DISTINCT r.variant_key FROM documents d JOIN urls u ON u.url_id=d.url_id "
        "LEFT JOIN responses r ON r.response_id=d.source_response_id "
        "WHERE u.url=? AND d.representation=? LIMIT 2",
        identity,
    )
    if not variants:
        if representation != "static":
            return None, "body observation is missing"
        return _resource_observation(con, url, variant_key)
    if variant_key is not None:
        clause, values = " AND r.variant_key=?", (*identity, variant_key)
    else:
        if len(variants) > 1:
            return None, "multiple HTTP variants are available; variant_key is required"
        clause, values = "", identity
    rows = _rows(
        con,
        "SELECT d.document_id,d.representation,d.body_sha256,d.fidelity,d.body_state,d.body_reason,"
        "d.source_response_id,r.variant_key,r.content_type,d.renderer_json,"
        "'page' AS purpose FROM documents d "
        "JOIN urls u ON u.url_id=d.url_id LEFT JOIN responses r ON r.response_id=d.source_response_id "
        "LEFT JOIN pages p ON p.url_id=d.url_id "
        "LEFT JOIN context_items i ON i.kind='resource_inventory' "
        "AND i.item_key='document:'||d.document_id "
        "WHERE u.url=? AND d.representation=?" + clause + " "
        "ORDER BY (p.document_id=d.document_id) DESC,(i.item_key IS NOT NULL) DESC,"
        "d.document_id DESC LIMIT 1",
        values,
    )
    if not rows:
        return None, "requested HTTP variant is missing"
    return rows[0], None


def _resource_observation(
    con: sqlite3.Connection, url: str, variant_key: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Return a retained static JS/CSS response when no page document exists."""
    associated = (
        "EXISTS(SELECT 1 FROM resource_refs rr WHERE rr.response_id=r.response_id) "
        "OR NOT EXISTS(SELECT 1 FROM resource_refs rr WHERE rr.resource_url_id=r.request_url_id "
        "AND rr.response_id IS NOT NULL)"
    )
    variants = _rows(
        con,
        "SELECT DISTINCT r.variant_key FROM responses r JOIN urls u ON u.url_id=r.request_url_id "
        "WHERE u.url=? AND r.purpose IN ('script','stylesheet') AND (" + associated + ") LIMIT 2",
        (url,),
    )
    if not variants:
        return None, "body observation is missing"
    if variant_key is not None:
        clause, values = " AND r.variant_key=?", (url, variant_key)
    else:
        if len(variants) > 1:
            return None, "multiple HTTP variants are available; variant_key is required"
        clause, values = "", (url,)
    rows = _rows(
        con,
        "SELECT NULL AS document_id,'static' AS representation,r.body_sha256,"
        "r.body_fidelity AS fidelity,r.body_state,r.body_reason,r.response_id AS source_response_id,"
        "r.variant_key,r.content_type,NULL AS renderer_json,r.purpose FROM responses r "
        "JOIN urls u ON u.url_id=r.request_url_id WHERE u.url=? "
        "AND r.purpose IN ('script','stylesheet') AND (" + associated + ")" + clause + " "
        "ORDER BY (EXISTS(SELECT 1 FROM resource_refs rr WHERE rr.response_id=r.response_id)) DESC,"
        "r.response_id DESC LIMIT 1",
        values,
    )
    if not rows:
        return None, "requested HTTP variant is missing"
    return rows[0], None


def _summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "document_id": row["document_id"],
        "variant_key": row["variant_key"],
        "fidelity": row["fidelity"],
        "body_state": row["body_state"],
        "body_reason": row["body_reason"],
        "sha256": row["body_sha256"],
        "content_type": row["content_type"],
    }


def _result(
    status: str,
    reason: str,
    *,
    url: str,
    representation: str,
    variant_key: str | None,
    left: dict[str, Any] | None,
    right: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "url": url,
        "representation": representation,
        "variant_key": variant_key,
        "left": _summary(left),
        "right": _summary(right),
    }


def _text(raw: bytes, row: dict[str, Any]) -> str:
    fidelity = row["fidelity"]
    if fidelity == "entity_bytes":
        return decode_entity(raw, row["content_type"] or "")[0]
    if fidelity == "serialized_dom":
        return raw.decode("utf-8", errors="strict")
    if fidelity == "reencoded_text":
        return raw.decode("utf-8", errors="replace")
    raise ScanError("body diff cannot decode an unavailable fidelity")


def _textual(row: dict[str, Any]) -> bool:
    if row["fidelity"] in {"serialized_dom", "reencoded_text"}:
        return True
    content_type = (row["content_type"] or "").partition(";")[0].strip().lower()
    return content_type.startswith("text/") or content_type in {
        "application/javascript",
        "application/ecmascript",
        "application/json",
        "application/xhtml+xml",
        "application/xml",
    }


def _renderer_provenance(row: dict[str, Any]) -> dict[str, Any] | None:
    """Accept only the complete, normalized renderer record written by the corpus layer."""
    try:
        value = json.loads(row["renderer_json"])
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    settings = value.get("settings")
    if not (
        isinstance(value.get("engine"), str)
        and value["engine"]
        and isinstance(value.get("engine_version"), str)
        and value["engine_version"]
        and isinstance(settings, dict)
        and isinstance(settings.get("navigation"), dict)
        and isinstance(settings.get("transforms"), dict)
        and value.get("navigation_transform") == "direct"
        and type(value.get("navigation_url_id")) is int
        and type(value.get("final_url_id")) is int
    ):
        return None
    return {
        "engine": value["engine"],
        "engine_version": value["engine_version"],
        "settings": settings,
        "navigation_transform": value["navigation_transform"],
    }


def _limits(max_bytes: int, max_lines: int) -> None:
    if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_BYTES:
        raise ValueError("max_bytes must be a positive finite body-diff limit")
    if type(max_lines) is not int or not 1 <= max_lines <= _MAX_LINES:
        raise ValueError("max_lines must be a positive finite body-diff line limit")


def body_diff(
    left: Any,
    right: Any,
    url: str,
    variant_key: str | None = None,
    representation: str = "static",
    text: bool = False,
    max_bytes: int = 5 * 1024 * 1024,
    max_lines: int = 10_000,
) -> dict[str, Any]:
    """Compare exactly matched retained bodies without fetching or SEO scoring."""
    if not isinstance(url, str) or not url:
        raise ValueError("url must be a nonempty string")
    if representation not in _REPRESENTATIONS:
        raise ValueError("representation must be static, rendered, or legacy_fragment")
    if variant_key is not None and not isinstance(variant_key, str):
        raise ValueError("variant_key must be a string or null")
    if type(text) is not bool:
        raise ValueError("text must be a boolean")
    _limits(max_bytes, max_lines)
    left_row, left_problem = _observation(_connection(left), url, representation, variant_key)
    right_row, right_problem = _observation(_connection(right), url, representation, variant_key)
    if left_problem or right_problem:
        return _result(
            "not_comparable"
            if any(problem and "variant" in problem for problem in (left_problem, right_problem))
            else "missing_evidence",
            "; ".join(
                side + ": " + problem
                for side, problem in (("left", left_problem), ("right", right_problem))
                if problem
            ),
            url=url,
            representation=representation,
            variant_key=variant_key,
            left=left_row,
            right=right_row,
        )
    assert left_row is not None and right_row is not None
    if variant_key is None and left_row["variant_key"] != right_row["variant_key"]:
        return _result(
            "not_comparable",
            "HTTP variants differ between scans; variant_key is required",
            url=url,
            representation=representation,
            variant_key=variant_key,
            left=left_row,
            right=right_row,
        )
    for side, row in (("left", left_row), ("right", right_row)):
        if row["body_state"] != "complete" or row["body_sha256"] is None:
            return _result(
                "missing_evidence",
                f"{side} body is {row['body_state']}/{row['body_reason']}",
                url=url,
                representation=representation,
                variant_key=variant_key,
                left=left_row,
                right=right_row,
            )
    if left_row["fidelity"] != right_row["fidelity"]:
        return _result(
            "not_comparable",
            "body fidelity differs between scans",
            url=url,
            representation=representation,
            variant_key=variant_key,
            left=left_row,
            right=right_row,
        )
    if left_row["purpose"] != right_row["purpose"]:
        return _result(
            "not_comparable",
            "body purpose differs between scans",
            url=url,
            representation=representation,
            variant_key=variant_key,
            left=left_row,
            right=right_row,
        )
    if representation == "rendered":
        left_renderer = _renderer_provenance(left_row)
        right_renderer = _renderer_provenance(right_row)
        if left_renderer is None or right_renderer is None:
            sides = ", ".join(
                side
                for side, renderer in (("left", left_renderer), ("right", right_renderer))
                if renderer is None
            )
            return _result(
                "not_comparable",
                f"rendered renderer provenance is unavailable for {sides}",
                url=url,
                representation=representation,
                variant_key=variant_key,
                left=left_row,
                right=right_row,
            )
        if left_renderer != right_renderer:
            return _result(
                "not_comparable",
                "rendered renderer provenance differs between scans",
                url=url,
                representation=representation,
                variant_key=variant_key,
                left=left_row,
                right=right_row,
            )
    status = "unchanged" if left_row["body_sha256"] == right_row["body_sha256"] else "changed"
    result = _result(
        status,
        "body hashes match" if status == "unchanged" else "body hashes differ",
        url=url,
        representation=representation,
        variant_key=variant_key,
        left=left_row,
        right=right_row,
    )
    if not text or status == "unchanged":
        return result
    if not _textual(left_row) or not _textual(right_row):
        result.update(status="not_comparable", reason="text diff requires textual body fidelity")
        return result
    left_size = _rows(
        _connection(left),
        "SELECT decoded_bytes FROM bodies WHERE sha256=?",
        (left_row["body_sha256"],),
    )
    right_size = _rows(
        _connection(right),
        "SELECT decoded_bytes FROM bodies WHERE sha256=?",
        (right_row["body_sha256"],),
    )
    if (
        not left_size
        or not right_size
        or left_size[0]["decoded_bytes"] > max_bytes
        or right_size[0]["decoded_bytes"] > max_bytes
    ):
        result.update(
            status="not_comparable", reason="text diff exceeds max_bytes before materialization"
        )
        return result
    left_raw = read_body(_connection(left), left_row["body_sha256"], max_decoded_bytes=max_bytes)
    right_raw = read_body(_connection(right), right_row["body_sha256"], max_decoded_bytes=max_bytes)
    left_lines = _text(left_raw, left_row).splitlines()
    right_lines = _text(right_raw, right_row).splitlines()
    if len(left_lines) > max_lines or len(right_lines) > max_lines:
        result.update(
            status="not_comparable", reason="text diff exceeds max_lines before materialization"
        )
        return result
    diff: list[str] = []
    output_bytes = 0
    for line in difflib.unified_diff(
        left_lines, right_lines, fromfile="left", tofile="right", lineterm=""
    ):
        line_bytes = len((line + "\n").encode("utf-8"))
        if len(diff) >= max_lines or output_bytes + line_bytes > max_bytes:
            result.update(
                status="not_comparable", reason="text diff output exceeds configured limits"
            )
            return result
        diff.append(line)
        output_bytes += line_bytes
    result["text_diff"] = diff
    return result


__all__ = ["body_diff"]
