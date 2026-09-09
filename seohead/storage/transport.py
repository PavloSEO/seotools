"""Optional scan.v2 response transport facts; unknown phases stay explicitly unknown."""

from __future__ import annotations

import sqlite3
from typing import Any

from . import ScanError


def ensure_schema(con: sqlite3.Connection) -> None:
    if con.execute("PRAGMA user_version").fetchone()[0] == 2:
        con.execute(
            "CREATE TABLE IF NOT EXISTS response_transport_meta(response_id INTEGER PRIMARY KEY,"
            "protocol TEXT,protocol_state TEXT NOT NULL,total_seconds REAL,"
            "dns_state TEXT NOT NULL,connect_state TEXT NOT NULL,tls_state TEXT NOT NULL,ttfb_state TEXT NOT NULL)"
        )


def record(con: sqlite3.Connection, response_id: int, event: Any) -> None:
    if con.execute("PRAGMA user_version").fetchone()[0] != 2:
        return
    ensure_schema(con)
    protocol = event.http_version
    if protocol is not None and (not isinstance(protocol, str) or len(protocol) > 32):
        raise ScanError("captured HTTP protocol is invalid")
    if event.timing_state not in {"partial", "unavailable"}:
        raise ScanError("captured timing state is invalid")
    con.execute(
        "INSERT INTO response_transport_meta VALUES(?,?,?,?,?,?,?,?)",
        (
            response_id, protocol, "known" if protocol else "unavailable", event.response_time,
            "unknown", "unknown", "unknown", "unknown",
        ),
    )
