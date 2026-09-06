"""Bounded standard-library codecs and readers for retained scan bodies."""

from __future__ import annotations

import codecs
import hashlib
import json
import re
import sqlite3
import zlib
from email.message import Message

from . import ScanError

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_DECODER_VERSION = "scan_decoder.v1"


def _renderer(document: dict[str, object], con=None) -> dict[str, object]:
    raw = document["renderer_json"]
    if not isinstance(raw, str):
        raise ScanError("document renderer provenance is invalid")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ScanError("document renderer provenance is invalid") from exc
    representation = document["representation"]
    if document["body_state"] == "complete" and (
        (representation == "rendered") != (document["fidelity"] == "serialized_dom")
    ):
        raise ScanError("document representation and body fidelity disagree")
    if representation == "static":
        if value != {}:
            raise ScanError("static document renderer must be empty")
        return {}
    required = {
        "engine",
        "engine_version",
        "settings",
        "flattened_iframes",
        "capture_limitations",
        "navigation_url_id",
        "final_url_id",
        "navigation_transform",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value["engine"]) is not str
        or type(value["engine_version"]) is not str
        or not isinstance(value["settings"], dict)
        or type(value["flattened_iframes"]) is not bool
        or not isinstance(value["capture_limitations"], list)
        or any(type(item) is not str for item in value["capture_limitations"])
        or value["navigation_transform"] not in {"direct", "legacy_escaped_fragment", "unknown"}
    ):
        raise ScanError("rendered document renderer provenance is invalid")
    if (
        representation == "legacy_fragment"
        and value["navigation_transform"] != "legacy_escaped_fragment"
    ):
        raise ScanError("legacy fragment requires a reproducible navigation transform")
    for name in ("navigation_url_id", "final_url_id"):
        if value[name] is not None and (type(value[name]) is not int or value[name] < 1):
            raise ScanError("renderer URL reference is invalid")
        if (
            con is not None
            and value[name] is not None
            and con.execute("SELECT 1 FROM urls WHERE url_id=?", (value[name],)).fetchone() is None
        ):
            raise ScanError("renderer URL reference is absent")
    transform = value["navigation_transform"]
    if document["body_state"] == "complete" and (
        transform == "unknown"
        or value["navigation_url_id"] is None
        or value["final_url_id"] is None
    ):
        raise ScanError("complete document requires known renderer navigation")
    if transform == "direct" and value["navigation_url_id"] != document["url_id"]:
        raise ScanError("direct renderer navigation differs from the logical page")
    if transform == "legacy_escaped_fragment" and con is not None:
        from seohead.tools.render import legacy_fragment_target

        logical = con.execute(
            "SELECT url FROM urls WHERE url_id=?", (document["url_id"],)
        ).fetchone()
        navigation = con.execute(
            "SELECT url FROM urls WHERE url_id=?", (value["navigation_url_id"],)
        ).fetchone()
        # This declared transform selects the existing meta-fragment branch;
        # its URL calculation must reproduce the recorded navigation.
        expected = (
            legacy_fragment_target(logical[0], '<meta name="fragment" content="!">')
            if logical
            else None
        )
        if navigation is None or navigation[0] != expected:
            raise ScanError("legacy fragment navigation does not reproduce the declared transform")
    return value


def encode_body(raw: bytes) -> dict[str, object]:
    """Encode exact content-decoded bytes using identity or smaller zlib-6 data."""
    if type(raw) is not bytes:
        raise ScanError("body bytes must be an exact bytes value")
    compressed = zlib.compress(raw, 6)
    codec, data = ("zlib", compressed) if len(compressed) < len(raw) else ("identity", raw)
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "codec": codec,
        "decoded_bytes": len(raw),
        "stored_bytes": len(data),
        "data": data,
    }


def read_body(con: sqlite3.Connection, sha256: str, *, max_decoded_bytes: int) -> bytes:
    """Read and verify one body without allowing a compressed expansion bomb."""
    if not isinstance(con, sqlite3.Connection):
        raise ScanError("body reader requires a sqlite3 connection")
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise ScanError("body SHA-256 must be lowercase hex")
    if type(max_decoded_bytes) is not int or max_decoded_bytes < 0:
        raise ScanError("body decoded-byte limit must be a nonnegative integer")
    row = con.execute(
        "SELECT sha256, codec, decoded_bytes, stored_bytes, length(data) "
        "FROM bodies WHERE sha256=?",
        (sha256,),
    ).fetchone()
    if row is None:
        raise ScanError("body unavailable: not_in_corpus")
    values = tuple(row)
    if len(values) != 5:
        raise ScanError("body row is malformed")
    stored_sha, codec, decoded_bytes, stored_bytes, actual_stored_bytes = values
    if (
        stored_sha != sha256
        or type(codec) is not str
        or type(decoded_bytes) is not int
        or type(stored_bytes) is not int
        or type(actual_stored_bytes) is not int
        or decoded_bytes < 0
        or stored_bytes < 0
        or stored_bytes != actual_stored_bytes
    ):
        raise ScanError("body metadata or stored length is invalid")
    if decoded_bytes > max_decoded_bytes:
        raise ScanError("body unavailable: decoded byte limit exceeded")
    if stored_bytes > decoded_bytes:
        raise ScanError("body stored size exceeds its decoded-byte declaration")
    data_row = con.execute("SELECT data FROM bodies WHERE sha256=?", (sha256,)).fetchone()
    if data_row is None or type(data_row[0]) is not bytes:
        raise ScanError("body data is unavailable or malformed")
    data = data_row[0]
    if codec == "identity":
        raw = data
        if len(raw) != decoded_bytes:
            raise ScanError("identity body decoded length disagrees")
    elif codec == "zlib":
        decoder = zlib.decompressobj()
        try:
            raw = decoder.decompress(data, max_decoded_bytes + 1)
        except zlib.error as exc:
            raise ScanError("compressed body has an invalid zlib stream") from exc
        if len(raw) > max_decoded_bytes or decoder.unconsumed_tail:
            raise ScanError("compressed body exceeds decoded byte limit")
        if not decoder.eof or decoder.unused_data or len(raw) != decoded_bytes:
            raise ScanError("compressed body is truncated, trailing, or length-mismatched")
    else:
        raise ScanError("body codec is unsupported")
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ScanError("body SHA-256 disagrees with decoded bytes")
    return raw


def decode_entity(raw: bytes, content_type: str) -> tuple[str, dict[str, str]]:
    """Decode entity bytes under the explicit ``scan_decoder.v1`` policy."""
    if type(raw) is not bytes or not isinstance(content_type, str):
        raise ScanError("entity decoder requires bytes and a content-type string")
    message = Message()
    message["content-type"] = content_type
    requested = message.get_content_charset()
    try:
        charset = codecs.lookup(requested).name if requested else "utf-8"
        source = "content_type_charset" if requested else "utf8_fallback"
        # Binary transformations such as base64 are codecs, but not charsets.
        b"x".decode(charset, errors="replace")
    except LookupError:
        charset = "utf-8"
        source = "utf8_fallback"
    return raw.decode(charset, errors="replace"), {
        "decoder_version": _DECODER_VERSION,
        "decoder_source": source,
        "decoder_charset": charset,
        "decoder_errors": "replace",
    }


def read_document(con: sqlite3.Connection, document_id: int, *, max_decoded_bytes: int) -> str:
    """Read one stored document only when its state and response lineage are complete."""
    if type(document_id) is not int or document_id < 1:
        raise ScanError("document ID must be a positive integer")
    cursor = con.execute(
        "SELECT d.*, r.request_url_id AS response_request_url_id, "
        "r.body_sha256 AS response_body_sha256, r.content_type AS response_content_type "
        ", r.body_state AS response_body_state, r.body_fidelity AS response_body_fidelity "
        "FROM documents d LEFT JOIN responses r ON r.response_id=d.source_response_id "
        "WHERE d.document_id=?",
        (document_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ScanError("document unavailable: not_in_corpus")
    document = {
        column[0]: value for column, value in zip(cursor.description or (), row, strict=True)
    }
    state = document["body_state"]
    renderer = _renderer(document, con)
    body_sha = document["body_sha256"]
    if state != "complete" or body_sha is None:
        raise ScanError(f"document unavailable: {state}/{document['body_reason']}")
    fidelity = document["fidelity"]
    raw = read_body(con, body_sha, max_decoded_bytes=max_decoded_bytes)
    if fidelity in {"entity_bytes", "reencoded_text"}:
        expected_request_url = document["url_id"]
        if document["representation"] == "legacy_fragment":
            expected_request_url = renderer["navigation_url_id"]
            if type(expected_request_url) is not int:
                raise ScanError("legacy fragment navigation URL provenance is invalid")
        if (
            document["source_response_id"] is None
            or document["response_request_url_id"] != expected_request_url
            or document["response_body_sha256"] != body_sha
            or document["response_body_state"] != "complete"
            or document["response_body_fidelity"] != fidelity
        ):
            raise ScanError(
                "document response state, fidelity, URL, or body hash lineage disagrees"
            )
        if fidelity == "entity_bytes":
            text, decoder = decode_entity(raw, document["response_content_type"])
        else:
            text = raw.decode("utf-8", errors="replace")
            decoder = {
                "decoder_version": _DECODER_VERSION,
                "decoder_source": "legacy_unknown",
                "decoder_charset": "unknown",
                "decoder_errors": "unknown",
            }
    elif fidelity == "serialized_dom":
        if document["representation"] != "rendered":
            raise ScanError("serialized DOM requires rendered representation")
        text = raw.decode("utf-8", errors="strict")
        decoder = {
            "decoder_version": _DECODER_VERSION,
            "decoder_source": "renderer_utf8",
            "decoder_charset": "utf-8",
            "decoder_errors": "not_applicable",
        }
    else:
        raise ScanError("document fidelity is unavailable or unsupported")
    if any(document[key] != value for key, value in decoder.items()):
        raise ScanError("document decoder metadata disagrees with stored bytes")
    return text
