"""Offline regression coverage for bounded event timestamp and resume coverage semantics."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from seohead.crawl.events import EventSink
from seohead.storage.events import append, ensure_schema, set_coverage, timeline


def test_new_events_have_actual_utc_timestamp_and_historical_unknown_is_preserved():
    sink = EventSink(cap=2)
    current = sink.emit(
        "queue", {"queue_ordinal": 0, "url_id": 1, "depth": 0, "reason": "seed", "count": 1}
    )
    historical = sink.emit("stop", {"reason": "imported historic state"}, occurred_at=None)

    assert current["timestamp_state"] == "known"
    assert current["occurred_at"].endswith("Z")
    assert datetime.fromisoformat(current["occurred_at"].replace("Z", "+00:00")).tzinfo is not None
    assert historical["occurred_at"] is None
    assert historical["timestamp_state"] == "unknown"


def test_partial_event_coverage_stays_partial_after_resume_style_update():
    con = sqlite3.connect(":memory:")
    try:
        ensure_schema(con)
        first = EventSink(cap=1, writer=lambda event: append(con, event))
        first.emit("stop", {"reason": "first run"})
        first.emit("stop", {"reason": "capped"})
        set_coverage(con, first.coverage())

        # A resumed writer has one persisted record and no new drops, but must not erase the
        # earlier cap outcome while it checkpoints current coverage.
        resumed_coverage = {"state": "complete", "captured": 1, "dropped": 0, "cap": 1}
        set_coverage(con, resumed_coverage)

        result = timeline(con)
        assert result["coverage"] == {"state": "partial", "captured": 1, "dropped": 1, "cap": 1}
        assert result["events"][0]["timestamp_state"] == "known"
    finally:
        con.close()
