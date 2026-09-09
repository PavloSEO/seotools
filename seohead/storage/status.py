"""Read-only operational status summaries for validated scan artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import open_scan


def scan_status(input_path: str) -> dict[str, Any]:
    """Summarize one scan snapshot without fetching, retrying, or changing its bytes."""
    if not isinstance(input_path, str) or not input_path:
        raise ValueError("input_path is required")
    con = open_scan(Path(input_path), require_audit=False)
    try:
        scan = dict(con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
        source = {
            key: scan[key]
            for key in (
                "scan_uuid",
                "format_version",
                "source_kind",
                "parent_scan_uuid",
                "writer_version",
                "writer_revision",
                "evidence_revision",
                "created_at",
                "finished_at",
                "lifecycle",
                "finish_reason",
            )
        }
        source.update(
            crawl_partial=bool(scan["crawl_partial"]), corpus_partial=bool(scan["corpus_partial"])
        )
        outcomes = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0, "other": 0, "no_response": 0}
        for row in con.execute("SELECT status_code FROM pages"):
            code = row[0]
            if code is None:
                outcomes["no_response"] += 1
            elif 200 <= code < 300:
                outcomes["2xx"] += 1
            elif 300 <= code < 400:
                outcomes["3xx"] += 1
            elif 400 <= code < 500:
                outcomes["4xx"] += 1
            elif 500 <= code < 600:
                outcomes["5xx"] += 1
            else:
                outcomes["other"] += 1
        if scan["source_kind"] == "legacy_import":
            frontier = {
                "state": "unavailable",
                "reason": "legacy import retains no native frontier",
                "counts": None,
            }
        else:
            counts = {state: 0 for state in ("queued", "inflight", "done", "excluded")}
            for state, count in con.execute("SELECT state,COUNT(*) FROM frontier GROUP BY state"):
                counts[state] = count
            frontier = {"state": "available", "reason": "", "counts": counts}
        return {
            "ok": True,
            "source": source,
            "frontier": frontier,
            "committed_page_outcomes": outcomes,
        }
    finally:
        con.close()
