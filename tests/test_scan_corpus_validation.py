"""Read-only corpus lineage validation over small scan.v1 table populations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from seohead.storage import ScanError
from seohead.storage.bodies import encode_body
from seohead.storage.corpus_validation import validate_corpus


def _con() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.executescript(Path("seohead/storage/scan_v1.sql").read_text(encoding="utf-8"))
    con.executemany(
        "INSERT INTO urls(url_id,url) VALUES(?,?)",
        [(1, "https://example.test/"), (2, "https://example.test/final")],
    )
    return con


def _corpus(con: sqlite3.Connection) -> None:
    body = encode_body(b"<p>ok</p>")
    con.execute(
        "INSERT INTO bodies VALUES(:sha256,:codec,:decoded_bytes,:stored_bytes,:data)", body
    )
    con.execute(
        "INSERT INTO responses(response_id,request_url_id,request_ordinal,effective_url_id,redirect_chain_json,method,purpose,requested_at,request_headers_redacted_json,credentials_used,variant_key,response_headers_redacted_json,effective_headers_redacted_json,content_type,charset,content_encoding,transport_source,cache_status,body_sha256,body_fidelity,body_state,body_reason,error,error_kind) VALUES(1,1,1,1,'[]','GET','page','2026-01-01T00:00:00Z','[]',0,'variant','[]','[]','text/html; charset=utf-8','','','network','',?,'entity_bytes','complete','none','','')",
        (body["sha256"],),
    )
    con.execute(
        "INSERT INTO documents VALUES(1,1,'static',1,?,'2026-01-01T00:00:00Z','scan_decoder.v1','content_type_charset','utf-8','replace','entity_bytes','complete','none','{}')",
        (body["sha256"],),
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


def _scan() -> dict[str, str]:
    return {"source_kind": "native"}


def _captured_policy() -> dict[str, object]:
    return {"body_mode": "captured_entity_bytes", "max_body_bytes": 1024}


def test_empty_pre_g_body_lanes_validate_under_off_policy():
    validate_corpus(_con(), _scan(), {"body_mode": "off"})


def test_captured_policy_allows_empty_lanes_before_first_request():
    validate_corpus(_con(), _scan(), _captured_policy())


@pytest.mark.parametrize(
    "statement, message",
    [
        ("UPDATE responses SET transport_source='cache'", "cache"),
        ("UPDATE responses SET body_state='unavailable'", "unavailable"),
        ("UPDATE documents SET body_sha256=NULL", "complete"),
    ],
)
def test_corpus_rejects_body_state_and_response_metadata_mismatches(statement, message):
    con = _con()
    _corpus(con)
    con.execute(statement)
    with pytest.raises(ScanError, match=message):
        validate_corpus(con, _scan(), _captured_policy())


def test_corpus_keeps_variant_keys_opaque():
    con = _con()
    _corpus(con)
    con.execute("UPDATE responses SET variant_key='Upper Variant=1'")
    validate_corpus(con, _scan(), _captured_policy())


def test_corpus_verifies_bodies_from_metadata_before_single_body_reads():
    con = _con()
    _corpus(con)
    statements = []
    con.set_trace_callback(statements.append)
    validate_corpus(con, _scan(), _captured_policy())
    assert not any("SELECT * FROM bodies" in statement for statement in statements)
    assert any("length(data) AS actual_size" in statement for statement in statements)


def test_corpus_rejects_document_response_and_page_selection_mismatches():
    con = _con()
    _corpus(con)
    con.execute("UPDATE documents SET url_id=2")
    with pytest.raises(ScanError, match=r"lineage|selected"):
        validate_corpus(con, _scan(), _captured_policy())


def test_corpus_rejects_resource_refs_without_document_provenance():
    con = _con()
    _corpus(con)
    con.execute(
        "INSERT INTO resource_refs(page_url_id,ordinal,resource_url_id,source_document_id,kind,representation,raw_url,capture_state,reason) VALUES(1,0,2,NULL,'script','static','/app.js','not_fetched','not fetched')"
    )
    with pytest.raises(ScanError, match="provenance"):
        validate_corpus(con, _scan(), _captured_policy())
