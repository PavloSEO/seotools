"""Adversarial persisted-state validation for the native scan writer."""

from __future__ import annotations

import sqlite3

import pytest

from seohead.storage import ScanError
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _link, _metadata, _record, _runtime


def _committed_scan(tmp_path):
    path = tmp_path / "scan.sqlite"
    scan = NativeScan.create(path, **_metadata())
    scan.enqueue([("https://example.test/", 0)])
    lease = scan.claim(1)[0]
    scan.commit_page(
        lease,
        _record(),
        links=[
            _link(lease.url, "https://example.test/a", "first"),
            _link(lease.url, "https://example.test/b", "second"),
        ],
        forms=[
            {"page": lease.url, "method": "post", "action": "/one", "has_password": False},
            {"page": lease.url, "method": "get", "action": "/two", "has_password": True},
        ],
        discovered=[("https://example.test/next", 1)],
        runtime=_runtime(),
    )
    scan.close()
    return path


def test_create_refuses_a_non_resolved_config(tmp_path):
    metadata = _metadata()
    metadata["config"] = {
        "speed": {"concurrency": 1},
        "limits": {"max_query_variants_per_path": 1},
    }
    metadata["config_fingerprint"] = None

    with pytest.raises(ScanError, match="config"):
        NativeScan.create(tmp_path / "invalid.sqlite", **metadata)

    assert not (tmp_path / "invalid.sqlite").exists()


def test_create_requires_non_result_config_fields_too(tmp_path):
    metadata = _metadata()
    del metadata["config"]["output"]
    with pytest.raises(ScanError, match="complete resolved"):
        NativeScan.create(tmp_path / "invalid.sqlite", **metadata)
    assert not (tmp_path / "invalid.sqlite").exists()


def test_unsupported_writer_platform_publishes_no_file(tmp_path, monkeypatch):
    import seohead.storage.native_scan as native

    monkeypatch.setattr(native, "fcntl", None)
    with pytest.raises(ScanError, match="POSIX"):
        NativeScan.create(tmp_path / "invalid.sqlite", **_metadata())
    assert not (tmp_path / "invalid.sqlite").exists()


def test_native_inspector_rejects_unknown_raw_context(tmp_path):
    path = _committed_scan(tmp_path)
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO context_items(kind,item_key,payload_version,payload_json,completeness,reason) "
            "VALUES(?,?,?,?,?,?)",
            (
                "native_start_page",
                "run",
                "scan_context.v1",
                '{"html":"untrusted retained body"}',
                "complete",
                "raw",
            ),
        )

    with pytest.raises(ScanError, match="context"):
        NativeScan.inspect(path)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE pages SET page_ordinal=99", "page ordinals"),
        ("UPDATE frontier SET depth=-1 WHERE state='queued'", "frontier depth"),
    ],
)
def test_native_inspector_rejects_invalid_page_and_frontier_order(tmp_path, statement, message):
    path = _committed_scan(tmp_path)
    with sqlite3.connect(path) as con:
        con.execute(statement)

    with pytest.raises(ScanError, match=message):
        NativeScan.inspect(path)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE scan SET writer_revision='not-a-full-sha'", "writer revision"),
        ("UPDATE scan SET runtime_versions_json='{}'", "runtime provenance"),
        (
            "UPDATE scan SET retention_json="
            '\'{"policy_version":"scan_retention.v1","body_mode":"off",'
            '"max_body_bytes":0,"max_body_store_bytes":0,"min_free_bytes":0,'
            '"history_warning_bytes":0,"automatic_delete":true}\'',
            "retention",
        ),
    ],
)
def test_native_inspector_rejects_forged_provenance_and_retention(tmp_path, statement, message):
    path = _committed_scan(tmp_path)
    with sqlite3.connect(path) as con:
        con.execute(statement)

    with pytest.raises(ScanError, match=message):
        NativeScan.inspect(path)


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("UPDATE links SET ordinal=7 WHERE ordinal=1", "link ordinals"),
        ("UPDATE forms SET ordinal=7 WHERE ordinal=1", "form ordinals"),
    ],
)
def test_native_inspector_rejects_link_and_form_ordinal_gaps(tmp_path, statement, message):
    path = _committed_scan(tmp_path)
    with sqlite3.connect(path) as con:
        con.execute(statement)

    with pytest.raises(ScanError, match=message):
        NativeScan.inspect(path)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE scan SET corpus_partial=0",
        "UPDATE scan SET evidence_version='other.v9'",
        "UPDATE scan SET pinned=1",
        "UPDATE scan SET parent_scan_uuid='parent'",
    ],
)
def test_native_inspector_refuses_unsupported_capture_metadata(tmp_path, statement):
    path = _committed_scan(tmp_path)
    with sqlite3.connect(path) as con:
        con.execute(statement)
    with pytest.raises(ScanError, match="metadata"):
        NativeScan.inspect(path)


def test_page_budget_refuses_oversized_record_before_serialization(tmp_path, monkeypatch):
    import seohead.storage.native_scan as native

    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        record = _record()
        record["title"] = "x" * (8 * 1024 * 1024 + 1)

        def unexpected_serialization(*args, **kwargs):
            pytest.fail("oversized scalar reached JSON serialization")

        monkeypatch.setattr(native.json.JSONEncoder, "iterencode", unexpected_serialization)
        with pytest.raises(ScanError, match="byte limit"):
            scan.commit_page(lease, record, runtime=_runtime())
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
