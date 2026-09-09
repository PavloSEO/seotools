"""Optional scan.v2 event-table persistence and read-only bounded timeline views."""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any

from seohead.crawl.events import FORMAT, MAX_EVENTS, validate
from seohead.storage import ScanError

TABLE = "scan_events"
META_TABLE = "scan_event_meta"


def ensure_schema(con: sqlite3.Connection) -> None:
    """Create the optional v2 extension tables inside the caller's existing transaction lane."""
    con.execute(
        "CREATE TABLE IF NOT EXISTS scan_events("
        "sequence INTEGER PRIMARY KEY CHECK(sequence > 0),"
        "event_type TEXT NOT NULL,occurred_at TEXT,timestamp_state TEXT NOT NULL,"
        "payload_json TEXT NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS scan_event_meta("
        "singleton INTEGER PRIMARY KEY CHECK(singleton=1),cap INTEGER NOT NULL,"
        "captured INTEGER NOT NULL,dropped INTEGER NOT NULL)"
    )


def append(con: sqlite3.Connection, event: dict[str, Any]) -> None:
    """Persist one already-bounded event; sequence must remain monotonic in this artifact."""
    event = validate(event)
    previous = con.execute(f"SELECT MAX(sequence) FROM {TABLE}").fetchone()[0]
    if event["sequence"] != (previous or 0) + 1:
        raise ScanError("scan event sequence is not monotonic")
    con.execute(
        f"INSERT INTO {TABLE}(sequence,event_type,occurred_at,timestamp_state,payload_json) VALUES(?,?,?,?,?)",
        (
            event["sequence"], event["event_type"], event["occurred_at"], event["timestamp_state"],
            json.dumps(event["payload"], sort_keys=True, separators=(",", ":"), allow_nan=False),
        ),
    )


def set_coverage(con: sqlite3.Connection, coverage: dict[str, Any]) -> None:
    """Persist the sink's cap outcome without inventing unrecorded event times."""
    if (
        not isinstance(coverage, dict)
        or set(coverage) != {"state", "captured", "dropped", "cap"}
        or coverage["state"] not in {"complete", "partial"}
        or any(type(coverage[name]) is not int or coverage[name] < 0 for name in ("captured", "dropped", "cap"))
        or not 1 <= coverage["cap"] <= MAX_EVENTS
        or coverage["captured"] > coverage["cap"]
        or (coverage["state"] == "complete") != (coverage["dropped"] == 0)
    ):
        raise ScanError("scan event coverage is invalid")
    con.execute(
        f"INSERT INTO {META_TABLE}(singleton,cap,captured,dropped) VALUES(1,?,?,?) "
        "ON CONFLICT(singleton) DO UPDATE SET cap=excluded.cap,captured=excluded.captured,dropped=excluded.dropped",
        (coverage["cap"], coverage["captured"], coverage["dropped"]),
    )


def timeline(con: sqlite3.Connection, *, limit: int = 1_000) -> dict[str, Any]:
    """Read one bounded ordered timeline; this helper never writes or contacts a provider."""
    if type(limit) is not int or not 1 <= limit <= MAX_EVENTS:
        raise ValueError(f"timeline limit must be an integer from 1 to {MAX_EVENTS}")
    try:
        rows = list(
            con.execute(
                f"SELECT sequence,event_type,occurred_at,timestamp_state,payload_json FROM {TABLE} "
                "ORDER BY sequence DESC LIMIT ?",
                (limit,),
            )
        )
        meta = con.execute(f"SELECT cap,captured,dropped FROM {META_TABLE} WHERE singleton=1").fetchone()
    except sqlite3.Error as exc:
        raise ScanError("scan does not contain the optional event timeline") from exc
    events = []
    for row in reversed(rows):
        try:
            events.append(validate({"format": FORMAT, "sequence": row[0], "event_type": row[1], "occurred_at": row[2], "timestamp_state": row[3], "payload": json.loads(row[4])}))
        except (TypeError, ValueError) as exc:
            raise ScanError("scan event timeline is malformed") from exc
    coverage = (
        {"state": "partial" if meta[2] else "complete", "captured": meta[1], "dropped": meta[2], "cap": meta[0]}
        if meta is not None
        else {"state": "unknown", "captured": len(events), "dropped": "unknown", "cap": "unknown"}
    )
    return {"format": "seohead.crawl-timeline.v1", "events": events, "coverage": coverage}


def read_timeline(path: str, *, limit: int = 1_000) -> dict[str, Any]:
    """Open an existing SQLite artifact read-only and return its bounded event timeline."""
    if not isinstance(path, str) or not path:
        raise ValueError("timeline path is required")
    if os.path.islink(path) or not os.path.isfile(path):
        raise ScanError("timeline path must be an existing non-symlink SQLite artifact")
    from seohead.storage import open_scan
    con = open_scan(path, require_audit=False)
    try:
        con.execute("PRAGMA query_only=ON")
        return timeline(con, limit=limit)
    finally:
        con.close()
