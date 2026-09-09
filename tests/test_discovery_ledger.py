"""Offline contract coverage for the scan.v2 discovery occurrence ledger."""

from __future__ import annotations

import sqlite3

import pytest

from seohead.storage import ScanError
from seohead.storage.discovery_ledger import (
    MAX_OCCURRENCES_PER_DOCUMENT,
    _relation_items,
    ensure_schema,
    put,
    read,
    store_document_relations,
)


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        "CREATE TABLE urls(url_id INTEGER PRIMARY KEY,url TEXT UNIQUE);"
        "CREATE TABLE frontier(url_id INTEGER,state TEXT);"
        "CREATE TABLE decisions(decision_id INTEGER PRIMARY KEY,url TEXT,reason TEXT);"
    )
    con.execute("INSERT INTO urls VALUES(1,'https://example.test/page')")
    con.execute("INSERT INTO frontier VALUES(1,'done')")
    ensure_schema(con)
    return con


def _occurrence(key: str = "one") -> dict:
    return {
        "occurrence_key": key,
        "source_kind": "html",
        "relation": "canonical",
        "source_url_id": 1,
        "source_document_id": 1,
        "source_response_id": 2,
        "representation": "static",
        "carrier": "link[0]",
        "raw_value": "/page",
        "resolved_value": "https://example.test/page",
        "depth": 0,
        "outcome": "fetched",
        "reason": "",
        "attributes": {},
    }


def test_link_header_keeps_quoted_comma_and_hreflang_metadata():
    items = _relation_items(
        '<base href="https://example.test/base/"><link rel="canonical" href="preferred">',
        "https://example.test/page",
        {"link": '<alt>; title="one,two"; hreflang=fr; rel=alternate, <next>; rel="next"'},
    )
    assert ("canonical", "link[0]", "preferred", "https://example.test/base/preferred", {}) in items
    assert any(item[0] == "hreflang" and item[4]["hreflang"] == "fr" for item in items)
    assert any(item[0] == "next" for item in items)


def test_occurrence_retry_is_idempotent_but_mismatch_is_refused():
    con = _con()
    put(con, _occurrence())
    put(con, _occurrence())
    changed = _occurrence()
    changed["raw_value"] = "/other"
    with pytest.raises(ScanError, match="immutable"):
        put(con, changed)


def test_document_cap_is_partial_not_a_page_write_failure():
    con = _con()
    forms = [{"action": f"/form-{index}"} for index in range(MAX_OCCURRENCES_PER_DOCUMENT + 1)]
    store_document_relations(
        con,
        source_url_id=1,
        source_document_id=1,
        source_response_id=None,
        representation="static",
        source_url="https://example.test/page",
        depth=0,
        html=None,
        headers={},
        links=[],
        candidates=[],
        decisions=[],
        forms=forms,
    )
    result = read(con)
    assert result["state"] == "partial"
    assert result["coverage"][0]["omitted"] == 1
