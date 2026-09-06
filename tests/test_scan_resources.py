"""Resource declaration occurrences are transactional evidence, not fetch work."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from seohead.storage import ScanError
from seohead.storage.bodies import encode_body
from seohead.storage.resources import put_declarations, resource_capabilities, validate_resources


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(Path("seohead/storage/scan_v1.sql").read_text(encoding="utf-8"))
    con.execute("INSERT INTO urls(url_id,url) VALUES(1,'https://example.test/page')")
    con.execute(
        "INSERT INTO documents VALUES(1,1,'static',NULL,NULL,'2026-01-01T00:00:00Z','scan_decoder.v1','not_applicable','unknown','not_applicable','unavailable','unavailable','not_fetched','{}')"
    )
    values = {}
    for _cid, name, kind, _notnull, _default, _primary in con.execute("PRAGMA table_info(pages)"):
        values[name] = "" if kind == "TEXT" else 0 if kind == "INTEGER" else 0.0
    values.update(
        url_id=1,
        page_ordinal=0,
        document_id=1,
        content_type="text/html",
        representation="static",
        redirect_chain_json="[]",
    )
    con.execute(
        f"INSERT INTO pages({','.join(values)}) VALUES({','.join('?' for _ in values)})",
        tuple(values.values()),
    )
    return con


def test_declarations_preserve_every_occurrence_and_replace_one_representation():
    con = _con()
    result = put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=False,
        declarations=[
            {"kind": "script", "url": "https://example.test/a.js", "raw_url": "/a.js"},
            {"kind": "script", "url": "https://example.test/a.js", "raw_url": "/a.js"},
            {"kind": "stylesheet", "url": "https://example.test/a.css", "raw_url": "/a.css"},
        ],
    )
    assert result == {"script": 2, "stylesheet": 1, "omitted": 0}
    assert [
        tuple(row)
        for row in con.execute("SELECT kind,ordinal FROM resource_refs ORDER BY kind,ordinal")
    ] == [("script", 0), ("script", 1), ("stylesheet", 0)]
    put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=False,
        declarations=[],
    )
    assert con.execute("SELECT COUNT(*) FROM resource_refs").fetchone()[0] == 0
    assert resource_capabilities(con, fetch_enabled=False)["resource_refs"]["state"] == "complete"


def test_resource_validation_rejects_fetch_state_response_mismatch():
    con = _con()
    put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=False,
        declarations=[{"kind": "script", "url": "https://example.test/a.js", "raw_url": "/a.js"}],
    )
    con.commit()
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("UPDATE resource_refs SET response_id=1")
    con.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(ScanError, match="unfetched"):
        validate_resources(con)


def test_capabilities_distinguish_absent_and_partial_inventory():
    con = _con()
    assert (
        resource_capabilities(con, fetch_enabled=False)["resource_refs"]["state"] == "unavailable"
    )
    put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=True,
        inventory_state="partial",
        omitted=2,
        declarations=[],
    )
    caps = resource_capabilities(con, fetch_enabled=True)
    assert caps["resource_refs"]["state"] == "partial"
    assert caps["resource_bodies"]["state"] == "unavailable"


def _complete_resource_response(con: sqlite3.Connection, resource_url_id: int) -> None:
    body = encode_body(b"resource")
    con.execute(
        "INSERT INTO bodies VALUES(:sha256,:codec,:decoded_bytes,:stored_bytes,:data)", body
    )
    con.execute(
        "INSERT INTO responses(response_id,request_url_id,request_ordinal,effective_url_id,redirect_chain_json,method,purpose,requested_at,request_headers_redacted_json,credentials_used,variant_key,status_code,effective_status_code,response_headers_redacted_json,effective_headers_redacted_json,content_type,charset,content_encoding,transport_source,cache_status,body_sha256,body_fidelity,body_state,body_reason,error,error_kind) VALUES(1,?,1,?,'[]','GET','script','2026-01-01T00:00:00Z','[]',0,'resource',200,200,'[]','[]','text/javascript','','','network','',?,'entity_bytes','complete','none','','')",
        (resource_url_id, resource_url_id, body["sha256"]),
    )


def test_fetch_enabled_measured_empty_inventory_has_complete_body_coverage():
    con = _con()
    put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=True,
        declarations=[],
    )
    caps = resource_capabilities(con, fetch_enabled=True)
    assert caps["resource_refs"]["state"] == "complete"
    assert caps["resource_bodies"]["state"] == "complete"


def test_all_measured_refs_with_complete_matching_response_have_complete_body_coverage():
    con = _con()
    put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=True,
        declarations=[{"kind": "script", "url": "https://example.test/a.js", "raw_url": "/a.js"}],
    )
    resource_id = con.execute("SELECT resource_url_id FROM resource_refs").fetchone()[0]
    _complete_resource_response(con, resource_id)
    con.execute("UPDATE resource_refs SET response_id=1,capture_state='measured',reason='' ")
    validate_resources(con)
    assert resource_capabilities(con, fetch_enabled=True)["resource_bodies"]["state"] == "complete"


def test_mixed_fetches_are_partial_and_measured_needs_complete_response():
    con = _con()
    put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=True,
        declarations=[
            {"kind": "script", "url": "https://example.test/a.js", "raw_url": "/a.js"},
            {"kind": "script", "url": "https://example.test/b.js", "raw_url": "/b.js"},
        ],
    )
    resource_id = con.execute(
        "SELECT resource_url_id FROM resource_refs WHERE ordinal=0"
    ).fetchone()[0]
    _complete_resource_response(con, resource_id)
    con.execute(
        "UPDATE resource_refs SET response_id=1,capture_state='measured',reason='' WHERE ordinal=0"
    )
    validate_resources(con)
    assert resource_capabilities(con, fetch_enabled=True)["resource_bodies"]["state"] == "partial"
    con.execute("UPDATE resource_refs SET capture_state='measured',reason='' WHERE ordinal=1")
    with pytest.raises(ScanError, match=r"response|measured"):
        validate_resources(con)


def test_resource_ordinal_gap_is_rejected_and_current_complete_replaces_old_partial_context():
    con = _con()
    put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=False,
        inventory_state="partial",
        omitted=1,
        declarations=[{"kind": "script", "url": "https://example.test/a.js", "raw_url": "/a.js"}],
    )
    put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=False,
        inventory_state="complete",
        declarations=[
            {"kind": "script", "url": "https://example.test/a.js", "raw_url": "/a.js"},
            {"kind": "script", "url": "https://example.test/b.js", "raw_url": "/b.js"},
        ],
    )
    assert resource_capabilities(con, fetch_enabled=False)["resource_refs"]["state"] == "complete"
    con.execute("UPDATE resource_refs SET ordinal=3 WHERE ordinal=1")
    with pytest.raises(ScanError, match="ordinal"):
        validate_resources(con)


def _inventory_capability_work(count: int) -> tuple[dict[str, dict[str, str]], int]:
    con = sqlite3.connect(":memory:")
    con.executescript(Path("seohead/storage/scan_v1.sql").read_text(encoding="utf-8"))
    con.executemany(
        "INSERT INTO urls(url_id,url) VALUES(?,?)",
        [(index, f"https://example.test/{index}") for index in range(1, count + 1)],
    )
    documents = [(1, 1, "static"), (2, 1, "static")]
    inventories = [("document:1", "partial"), ("document:2", "complete")]
    for index in range(2, count + 1):
        document_id = index + 1
        documents.append((document_id, index, "static"))
        inventories.append((f"document:{document_id}", "complete"))
    con.executemany(
        "INSERT INTO documents(document_id,url_id,representation,source_response_id,body_sha256,"
        "captured_at,decoder_version,decoder_source,decoder_charset,decoder_errors,fidelity,"
        "body_state,body_reason,renderer_json) VALUES(?,?,?,NULL,NULL,'x','scan_decoder.v1',"
        "'not_applicable','unknown','not_applicable','unavailable','unavailable','not_fetched','{}')",
        documents,
    )
    con.executemany(
        "INSERT INTO context_items(kind,item_key,payload_version,payload_json,completeness,reason) "
        "VALUES('resource_inventory',?,'scan_context.v1','{}',?,'')",
        inventories,
    )
    work = [0]
    con.set_progress_handler(lambda: work.__setitem__(0, work[0] + 1), 1)
    try:
        capabilities = resource_capabilities(con, fetch_enabled=False)
    finally:
        con.set_progress_handler(None, 0)
        con.close()
    return capabilities, work[0]


def test_resource_capabilities_scales_linearly_with_current_inventory_rows():
    small_capabilities, small_work = _inventory_capability_work(24)
    large_capabilities, large_work = _inventory_capability_work(48)

    expected = {
        "resource_refs": {"state": "complete", "reason": ""},
        "resource_bodies": {"state": "unavailable", "reason": "resources.fetch disabled"},
    }
    assert small_capabilities == large_capabilities == expected
    assert large_work < small_work * 3


def test_unavailable_inventory_is_distinct_from_empty_measured_inventory():
    con = _con()
    put_declarations(
        con,
        page_url_id=1,
        document_id=1,
        representation="static",
        fetch_enabled=False,
        inventory_state="unavailable",
        declarations=[],
    )
    caps = resource_capabilities(con, fetch_enabled=False)
    assert caps["resource_refs"]["state"] == "unavailable"
    assert caps["resource_bodies"]["state"] == "unavailable"
