"""Offline retained-body diff contract."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from seohead.crawl.capture import CaptureEvent
from seohead.storage.bodies import encode_body
from seohead.storage.body_diff import body_diff
from seohead.storage.corpus import store_response
from seohead.storage.retention import NO_BODY_RETENTION

URL = "https://example.test/page"


def _policy():
    return {
        **NO_BODY_RETENTION,
        "body_mode": "captured_entity_bytes",
        "max_body_bytes": 2 * 1024 * 1024,
        "max_body_store_bytes": 4 * 1024 * 1024,
    }


def _append(
    con, *, raw=b"<html>one</html>", variant="v1", content_type="text/html", state="complete"
):
    event = CaptureEvent(
        method="GET",
        requested_url=URL,
        effective_url=URL,
        redirect_history=(),
        requested_at="2026-01-01T00:00:00Z",
        received_at="2026-01-01T00:00:01Z",
        status_code=200,
        request_headers=(("accept", "text/html"),),
        credentials_used=False,
        response_headers=(("content-type", content_type),),
        content_type=content_type,
        content_encoding="",
        entity_bytes=raw if state == "complete" else None,
        body_fidelity="entity_bytes" if state == "complete" else "unavailable",
        body_state=state,
        body_reason="none" if state == "complete" else state,
        error="",
        error_kind="",
        effective_status_code=200,
        effective_headers=(("content-type", content_type),),
    )
    response_id, _document_id = store_response(con, event, purpose="page", policy=_policy())
    con.execute("UPDATE responses SET variant_key=? WHERE response_id=?", (variant, response_id))


def _scan(**kwargs):
    con = sqlite3.connect(":memory:")
    con.executescript(Path("seohead/storage/scan_v1.sql").read_text())
    _append(con, **kwargs)
    return con


def _complete_binary(con, raw: bytes):
    encoded = encode_body(raw)
    con.execute(
        "INSERT INTO bodies(sha256,codec,decoded_bytes,stored_bytes,data) "
        "VALUES(:sha256,:codec,:decoded_bytes,:stored_bytes,:data)",
        encoded,
    )
    con.execute(
        "UPDATE responses SET body_sha256=?,body_fidelity='entity_bytes',body_state='complete',"
        "body_reason='none'",
        (encoded["sha256"],),
    )
    con.execute(
        "UPDATE documents SET body_sha256=?,fidelity='entity_bytes',body_state='complete',"
        "body_reason='none'",
        (encoded["sha256"],),
    )


def test_hash_first_unchanged_changed_and_opt_in_text_diff():
    left = _scan(raw=b"<main>left</main>")
    same = _scan(raw=b"<main>left</main>")
    right = _scan(raw=b"<main>right</main>")
    try:
        assert body_diff(left, same, URL)["status"] == "unchanged"
        changed = body_diff(left, right, URL, text=True)
        assert changed["status"] == "changed"
        assert any(line == "-<main>left</main>" for line in changed["text_diff"])
        assert any(line == "+<main>right</main>" for line in changed["text_diff"])
        assert "SEO" not in changed["reason"]
    finally:
        left.close()
        same.close()
        right.close()


def test_variant_ambiguity_is_not_comparable_and_exact_variant_selects_one():
    extra = _scan(raw=b"a", variant="first")
    _append(extra, raw=b"b", variant="second")
    right = _scan(raw=b"a", variant="first")
    try:
        ambiguous = body_diff(extra, right, URL)
        assert ambiguous["status"] == "not_comparable"
        assert "variant_key is required" in ambiguous["reason"]
        assert body_diff(extra, right, URL, variant_key="first")["status"] == "unchanged"
    finally:
        extra.close()
        right.close()


def test_different_single_variants_and_right_only_ambiguity_are_not_comparable():
    left = _scan(raw=b"a", variant="left")
    right = _scan(raw=b"a", variant="right")
    ambiguous_right = _scan(raw=b"a", variant="one")
    _append(ambiguous_right, raw=b"b", variant="two")
    try:
        assert body_diff(left, right, URL)["status"] == "not_comparable"
        result = body_diff(left, ambiguous_right, URL)
        assert result["status"] == "not_comparable"
        assert "variant_key is required" in result["reason"]
    finally:
        left.close()
        right.close()
        ambiguous_right.close()


def test_missing_representation_does_not_fallback_to_static_html():
    left = _scan(raw=b"<html>static</html>")
    right = _scan(raw=b"<html>static</html>")
    try:
        result = body_diff(left, right, URL, representation="rendered")
        assert result["status"] == "missing_evidence"
        assert result["left"] is None and result["right"] is None
    finally:
        left.close()
        right.close()


def test_truncated_and_binary_text_requests_are_explicitly_not_clean_matches():
    complete = _scan(raw=b"<html>body</html>")
    truncated = _scan(state="truncated")
    binary_left = _scan(raw=b"\x00\x01", content_type="application/octet-stream")
    binary_right = _scan(raw=b"\x00\x02", content_type="application/octet-stream")
    _complete_binary(binary_left, b"\x00\x01")
    _complete_binary(binary_right, b"\x00\x02")
    try:
        missing = body_diff(complete, truncated, URL)
        assert missing["status"] == "missing_evidence"
        assert "truncated" in missing["reason"]
        binary = body_diff(binary_left, binary_right, URL, text=True)
        assert binary["status"] == "not_comparable"
        assert binary["reason"] == "text diff requires textual body fidelity"
    finally:
        complete.close()
        truncated.close()
        binary_left.close()
        binary_right.close()


def test_text_limits_are_checked_before_body_materialization():
    left = _scan(raw=(b"line\n" * 4))
    right = _scan(raw=(b"other\n" * 4))
    tiny_left = _scan(raw=b"a")
    tiny_right = _scan(raw=b"b")
    try:
        assert body_diff(left, right, URL, text=True, max_bytes=4)["status"] == "not_comparable"
        assert body_diff(left, right, URL, text=True, max_lines=2)["status"] == "not_comparable"
        assert (
            body_diff(tiny_left, tiny_right, URL, text=True, max_bytes=1)["status"]
            == "not_comparable"
        )
    finally:
        left.close()
        right.close()
        tiny_left.close()
        tiny_right.close()


def test_text_line_cap_recognizes_cr_and_unicode_separators_and_queries_are_bounded():
    left = _scan(raw="one\u2028two\rthree".encode())
    right = _scan(raw="one\u2028two\rfour".encode())
    try:
        assert body_diff(left, right, URL, text=True, max_lines=2)["status"] == "not_comparable"
        statements = []
        left.set_trace_callback(statements.append)
        body_diff(left, left, URL)
        document_queries = [query for query in statements if "FROM documents" in query]
        assert document_queries and all("LIMIT" in query for query in document_queries)
    finally:
        left.close()
        right.close()
