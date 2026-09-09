"""Offline regression coverage for browser artifact references; intentionally no browser runtime."""

from __future__ import annotations

import json
import sqlite3

from seohead.storage.browser_artifacts import save, validate_context


def _connection():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE pages(url_id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE documents(document_id INTEGER PRIMARY KEY,url_id INTEGER)")
    con.execute("INSERT INTO pages VALUES(1)")
    con.execute("INSERT INTO documents VALUES(2,1)")
    return con


def test_browser_artifact_context_is_document_scoped_and_failed_console_is_unavailable(tmp_path):
    con = _connection()
    try:
        item = save(
            tmp_path / "scan.sqlite",
            1,
            2,
            {"ok": False},
            screenshots=False,
            console_errors=True,
        )
        payload = json.loads(item["payload_json"])

        assert item["item_key"] == "page:1:document:2"
        assert payload["console"] == {
            "state": "unavailable",
            "reason": "render did not complete; console evidence is unavailable",
            "ref": None,
        }
        assert item["completeness"] == "unavailable"
        validate_context(con, item, payload)
    finally:
        con.close()
