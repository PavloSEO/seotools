"""Typed, bounded discovery occurrences for explicit ``scan.v2`` artifacts."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import ScanError

VERSION = "discovery_ledger.v1"
MAX_OCCURRENCES_PER_DOCUMENT = 2_000
_RELATIONS = {
    "seed",
    "hyperlink",
    "redirect",
    "canonical",
    "alternate",
    "hreflang",
    "x_default",
    "next",
    "prev",
    "refresh",
    "form_action",
    "http_link",
}
_OUTCOMES = {"queued", "fetched", "excluded", "blocked", "unresolved", "unmeasured"}
_HEADER_LINK = re.compile(r"\s*<([^>]*)>\s*(?:;\s*rel=\"?([^;,\"]+)\"?)?", re.I)


def ensure_schema(con: Any) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS discovery_occurrences ("
        "occurrence_key TEXT PRIMARY KEY,source_kind TEXT NOT NULL,relation TEXT NOT NULL,"
        "source_url_id INTEGER,source_document_id INTEGER,representation TEXT NOT NULL,"
        "carrier TEXT NOT NULL,raw_value TEXT NOT NULL,resolved_value TEXT NOT NULL,"
        "target_url_id INTEGER,depth INTEGER,outcome TEXT NOT NULL,reason TEXT NOT NULL)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS discovery_occurrences_target "
        "ON discovery_occurrences(target_url_id,outcome)"
    )


def _url_id(con: Any, value: str) -> int | None:
    if not value:
        return None
    row = con.execute("SELECT url_id FROM urls WHERE url=?", (value,)).fetchone()
    return int(row[0]) if row is not None else None


def _outcome(con: Any, value: str) -> tuple[str, str]:
    target_id = _url_id(con, value)
    if target_id is None:
        return "unresolved", "target URL was not retained in this scan"
    row = con.execute("SELECT state FROM frontier WHERE url_id=?", (target_id,)).fetchone()
    if row is None:
        return "unresolved", "target URL has no frontier outcome"
    state = str(row[0])
    if state == "done":
        return "fetched", ""
    if state == "queued":
        return "queued", ""
    if state == "excluded":
        reason = con.execute(
            "SELECT reason FROM decisions WHERE url=? ORDER BY decision_id DESC LIMIT 1", (value,)
        ).fetchone()
        detail = str(reason[0]) if reason is not None else "frontier excluded target"
        return ("blocked" if "robot" in detail.casefold() else "excluded"), detail
    return "unresolved", f"frontier state is {state}"


def put(con: Any, occurrence: dict[str, Any]) -> None:
    required = {
        "occurrence_key",
        "source_kind",
        "relation",
        "source_url_id",
        "source_document_id",
        "representation",
        "carrier",
        "raw_value",
        "resolved_value",
        "depth",
        "outcome",
        "reason",
    }
    if not isinstance(occurrence, dict) or set(occurrence) != required:
        raise ScanError("discovery occurrence has unsupported fields")
    if occurrence["relation"] not in _RELATIONS or occurrence["outcome"] not in _OUTCOMES:
        raise ScanError("discovery occurrence relation or outcome is invalid")
    if any(
        not isinstance(occurrence[key], str)
        for key in (
            "occurrence_key",
            "source_kind",
            "representation",
            "carrier",
            "raw_value",
            "resolved_value",
            "reason",
        )
    ):
        raise ScanError("discovery occurrence text is invalid")
    if len(occurrence["occurrence_key"]) > 512 or any(
        len(occurrence[key]) > 8192 for key in ("raw_value", "resolved_value", "reason")
    ):
        raise ScanError("discovery occurrence exceeds its bounded text budget")
    if occurrence["depth"] is not None and (
        type(occurrence["depth"]) is not int or occurrence["depth"] < 0
    ):
        raise ScanError("discovery occurrence depth is invalid")
    target_id = _url_id(con, occurrence["resolved_value"])
    con.execute(
        "INSERT OR REPLACE INTO discovery_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            occurrence["occurrence_key"],
            occurrence["source_kind"],
            occurrence["relation"],
            occurrence["source_url_id"],
            occurrence["source_document_id"],
            occurrence["representation"],
            occurrence["carrier"],
            occurrence["raw_value"],
            occurrence["resolved_value"],
            target_id,
            occurrence["depth"],
            occurrence["outcome"],
            occurrence["reason"],
        ),
    )


def _relation_items(
    html: str | None, source_url: str, headers: dict[str, Any] | None
) -> list[tuple[str, str, str, str]]:
    """Return relation, carrier, raw and resolved values without fetching."""
    items: list[tuple[str, str, str, str]] = []
    if isinstance(html, str):
        soup = BeautifulSoup(html, features="lxml")
        for ordinal, tag in enumerate(soup.find_all("link")):
            rels = {str(value).casefold() for value in tag.get("rel", [])}
            raw = str(tag.get("href") or "").strip()
            if not raw:
                continue
            relation = (
                "canonical"
                if "canonical" in rels
                else "alternate"
                if "alternate" in rels
                else "next"
                if "next" in rels
                else "prev"
                if "prev" in rels
                else ""
            )
            if not relation:
                continue
            if relation == "alternate" and tag.get("hreflang"):
                relation = (
                    "x_default"
                    if str(tag.get("hreflang")).casefold() == "x-default"
                    else "hreflang"
                )
            items.append((relation, f"link[{ordinal}]", raw, urljoin(source_url, raw)))
        meta = soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.I)})
        if meta is not None:
            raw = str(meta.get("content") or "")
            match = re.search(r"url\s*=\s*(.+)$", raw, re.I)
            if match:
                target = match.group(1).strip(" '\"")
                items.append(
                    ("refresh", "meta[http-equiv=refresh]", raw, urljoin(source_url, target))
                )
    if isinstance(headers, dict):
        location = headers.get("location") or headers.get("Location")
        if isinstance(location, str) and location:
            items.append(("redirect", "http-location", location, urljoin(source_url, location)))
        raw_header = headers.get("link") or headers.get("Link")
        if isinstance(raw_header, str):
            for ordinal, part in enumerate(raw_header.split(",")):
                match = _HEADER_LINK.match(part)
                if match:
                    raw = match.group(1)
                    rels = {part.casefold() for part in (match.group(2) or "").split()}
                    relation = (
                        "canonical"
                        if "canonical" in rels
                        else "alternate"
                        if "alternate" in rels
                        else "next"
                        if "next" in rels
                        else "prev"
                        if "prev" in rels
                        else "http_link"
                    )
                    items.append((relation, f"http-link[{ordinal}]", raw, urljoin(source_url, raw)))
        refresh = headers.get("refresh") or headers.get("Refresh")
        if isinstance(refresh, str):
            match = re.search(r"url\s*=\s*(.+)$", refresh, re.I)
            if match:
                target = match.group(1).strip(" '\"")
                items.append(("refresh", "http-refresh", refresh, urljoin(source_url, target)))
    return items


def store_document_relations(
    con: Any,
    *,
    source_url_id: int,
    source_document_id: int | None,
    representation: str,
    source_url: str,
    depth: int,
    html: str | None,
    headers: dict[str, Any] | None,
    links: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    forms: list[dict[str, Any]],
) -> None:
    """Store bounded document relations and candidate outcomes in the active transaction."""
    if source_document_id is None:
        return
    rows: list[dict[str, Any]] = []
    for ordinal, (relation, carrier, raw, resolved) in enumerate(
        _relation_items(html, source_url, headers)
    ):
        outcome, reason = _outcome(con, resolved)
        rows.append(
            {
                "occurrence_key": f"document:{source_document_id}:{relation}:{ordinal}",
                "source_kind": "http_header" if carrier.startswith("http-") else "html",
                "relation": relation,
                "source_url_id": source_url_id,
                "source_document_id": source_document_id,
                "representation": representation,
                "carrier": carrier,
                "raw_value": raw,
                "resolved_value": resolved,
                "depth": depth,
                "outcome": outcome,
                "reason": reason,
            }
        )
    headers = headers or {}
    for ordinal, item in enumerate(links):
        raw = str(item.get("raw_href") or "")
        resolved = str(item.get("destination") or "")
        outcome, reason = _outcome(con, resolved)
        rows.append(
            {
                "occurrence_key": f"document:{source_document_id}:link:{ordinal}",
                "source_kind": "html",
                "relation": "hyperlink",
                "source_url_id": source_url_id,
                "source_document_id": source_document_id,
                "representation": representation,
                "carrier": str(item.get("position") or "a[href]"),
                "raw_value": raw,
                "resolved_value": resolved,
                "depth": depth,
                "outcome": outcome,
                "reason": reason,
            }
        )
    redirect_raw = str(headers.get("location") or headers.get("Location") or "")
    redirect_target = urljoin(source_url, redirect_raw) if redirect_raw else ""
    for ordinal, item in enumerate(candidates):
        raw = str(item.get("requested_url") or "")
        resolved = str(item.get("frontier_url") or "")
        outcome, reason = _outcome(con, resolved)
        rows.append(
            {
                "occurrence_key": f"document:{source_document_id}:candidate:{ordinal}",
                "source_kind": "document",
                "relation": "redirect" if resolved == redirect_target else "hyperlink",
                "source_url_id": source_url_id,
                "source_document_id": source_document_id,
                "representation": representation,
                "carrier": "frontier_candidate",
                "raw_value": raw,
                "resolved_value": resolved,
                "depth": item.get("depth"),
                "outcome": outcome,
                "reason": reason,
            }
        )
    for ordinal, item in enumerate(decisions):
        raw = str(item.get("url") or "")
        rows.append(
            {
                "occurrence_key": f"document:{source_document_id}:decision:{ordinal}",
                "source_kind": "decision",
                "relation": "hyperlink",
                "source_url_id": source_url_id,
                "source_document_id": source_document_id,
                "representation": representation,
                "carrier": str(item.get("source") or "decision"),
                "raw_value": raw,
                "resolved_value": raw,
                "depth": item.get("depth"),
                "outcome": "excluded",
                "reason": str(item.get("reason") or "decision unavailable"),
            }
        )
    for ordinal, item in enumerate(forms):
        raw = str(item.get("action") or "")
        resolved = urljoin(source_url, raw) if raw else ""
        outcome, reason = _outcome(con, resolved)
        rows.append(
            {
                "occurrence_key": f"document:{source_document_id}:form:{ordinal}",
                "source_kind": "document",
                "relation": "form_action",
                "source_url_id": source_url_id,
                "source_document_id": source_document_id,
                "representation": representation,
                "carrier": "form[action]",
                "raw_value": raw,
                "resolved_value": resolved,
                "depth": depth,
                "outcome": outcome if resolved else "unmeasured",
                "reason": reason if resolved else "form action is absent",
            }
        )
    if len(rows) > MAX_OCCURRENCES_PER_DOCUMENT:
        raise ScanError("discovery occurrence budget exceeded")
    for row in rows:
        put(con, row)


def store_seeds(con: Any, entries: list[dict[str, Any]]) -> None:
    for ordinal, item in enumerate(entries):
        raw = str(item.get("requested_url") or "")
        resolved = str(item.get("frontier_url") or "")
        outcome, reason = _outcome(con, resolved)
        if item.get("reason"):
            outcome, reason = "excluded", str(item["reason"])
        put(
            con,
            {
                "occurrence_key": f"seed:{ordinal}:{raw}",
                "source_kind": "seed",
                "relation": "seed",
                "source_url_id": None,
                "source_document_id": None,
                "representation": "unmeasured",
                "carrier": str(item.get("source") or "seed"),
                "raw_value": raw,
                "resolved_value": resolved,
                "depth": item.get("depth"),
                "outcome": outcome,
                "reason": reason,
            },
        )


def read(con: Any, *, limit: int = 1000, offset: int = 0) -> dict[str, Any]:
    if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 10_000 or offset < 0:
        raise ValueError("limit must be 1..10000 and offset nonnegative")
    names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "discovery_occurrences" not in names:
        return {
            "schema_version": VERSION,
            "state": "unmeasured",
            "reason": "discovery ledger is unavailable in this scan format",
            "items": [],
        }
    rows = [
        dict(row)
        for row in con.execute(
            "SELECT * FROM discovery_occurrences ORDER BY occurrence_key LIMIT ? OFFSET ?",
            (limit + 1, offset),
        )
    ]
    return {
        "schema_version": VERSION,
        "state": "complete",
        "items": rows[:limit],
        "truncated": len(rows) > limit,
    }
