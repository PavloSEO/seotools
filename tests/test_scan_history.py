"""Validated native-artifact coverage for explicit saved-scan history actions."""

from __future__ import annotations

import fcntl
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from seohead import __version__
from seohead.crawl.capture import CaptureEvent
from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import fingerprint, load
from seohead.storage import ScanError, open_scan
from seohead.storage.history import (
    inspect_scan,
    list_scans,
    new_scan_path,
    pin_scan,
    prune_apply,
    prune_preview,
    recommended_name,
    snapshot_scan,
)
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _runtime

UTC = timezone.utc


def _metadata():
    config = load(overrides={"speed.min_delay_seconds": 0})
    return {
        "start_url": "https://example.test/",
        "config": config,
        "config_fingerprint": fingerprint(config),
        "writer_version": __version__,
        "writer_revision": "a" * 40,
        "runtime_versions": {
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
    }


def _record(url: str) -> dict:
    return vars(PageRecord(url=url, content_type="text/html", title="title", crawl_depth=0))


def _event(url: str, body: bytes) -> CaptureEvent:
    return CaptureEvent(
        method="GET",
        requested_url=url,
        effective_url=url,
        redirect_history=(),
        requested_at="2026-09-06T10:00:00Z",
        received_at="2026-09-06T10:00:01Z",
        status_code=200,
        request_headers=(("accept", "text/html"),),
        credentials_used=False,
        response_headers=(("content-type", "text/html; charset=utf-8"),),
        content_type="text/html; charset=utf-8",
        content_encoding="",
        entity_bytes=body,
        body_fidelity="entity_bytes",
        body_state="complete",
        body_reason="none",
        error="",
        error_kind="",
        effective_status_code=200,
        effective_headers=(("content-type", "text/html; charset=utf-8"),),
        response_time=0.1,
    )


def _save_audit(scan: NativeScan) -> None:
    from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
    from seohead.servers.handlers import _audit_crawl_result
    from seohead.servers.scan_handlers import _rebuild_page_result

    settings = _metadata()["config"]
    result = _rebuild_page_result(scan)
    result.start_page_evidence = {
        "html": "<html>body</html>",
        "outlinks": 0,
        "external_outlinks": 0,
    }
    with prepare_sitemap_reconciliation(scan.con, start_url="https://example.test/") as sitemap:
        _unused, audit = _audit_crawl_result(
            result,
            settings=settings,
            url="https://example.test/",
            sitemap_seed={"sitemap_url": None, "sitemap_urls": [], "declared": []},
            discovery={
                "mode": "spider",
                "directive_policy": settings["robots"]["policy"],
                "robots_blocked": 0,
                "sitemap_url": None,
                "sitemap_urls": [],
                "sitemap_seeded": 0,
            },
            stored_scan=scan,
            stored_sitemap=sitemap,
        )
    scan.save_audit(audit)


def _finished(path: Path, *, age_days: int = 31, captured: bool = True) -> None:
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            _record(lease.url),
            captures=[_event(lease.url, b"<html>body</html>")] if captured else (),
            runtime=_runtime(),
        )
        if captured:
            _save_audit(scan)
            assert scan.finish_capture("history fixture")
        else:
            assert scan.finish_without_audit("history fixture")
    finished_at = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    with sqlite3.connect(path) as con:
        con.execute("UPDATE scan SET finished_at=?", (finished_at,))


def test_list_inspect_preview_and_stale_apply_use_valid_native_artifacts(tmp_path):
    for index in range(6):
        _finished(tmp_path / f"{index}.sqlite", age_days=40 - index)
    listing = list_scans(tmp_path)
    assert listing["total"] == 6
    inspected = inspect_scan(tmp_path / "0.sqlite", max_bytes=4096)
    assert inspected["rows"][0]["title"] == "title"
    assert inspected["has_more"] is False
    plan = prune_preview(tmp_path, keep_newest=5)
    assert len(plan["candidates"]) == 1
    pin_scan(tmp_path / "0.sqlite", True)
    with pytest.raises(ScanError, match="metadata changed"):
        prune_apply(tmp_path, plan)


def test_pin_preserves_valid_retained_body_and_refuses_missing_path_without_lock(tmp_path):
    path = tmp_path / "captured.sqlite"
    _finished(path, captured=True)
    with open_scan(path, require_audit=False) as con:
        body = con.execute("SELECT sha256,data FROM bodies").fetchone()
        audit = con.execute("SELECT sha256,document_json FROM audit").fetchone()
        revision = con.execute("SELECT evidence_revision FROM scan").fetchone()[0]
    pin_scan(path, True)
    pin_scan(path, False)
    with open_scan(path, require_audit=False) as con:
        assert con.execute("SELECT evidence_revision FROM scan").fetchone()[0] == revision
        assert con.execute("SELECT sha256,data FROM bodies").fetchone() == body
        assert con.execute("SELECT sha256,document_json FROM audit").fetchone() == audit
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ScanError):
        pin_scan(missing, True)
    assert not missing.exists()
    assert not missing.with_name(missing.name + ".writer.lock").exists()


def test_pin_refuses_active_writer_and_snapshot_is_wal_independent(tmp_path):
    path = tmp_path / "scan.sqlite"
    _finished(path, captured=True)
    lock = path.with_name(path.name + ".writer.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ScanError, match="active writer"):
            pin_scan(path, True)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    target = tmp_path / "snapshot.sqlite"
    snapshot_scan(path, target)
    with open_scan(target, require_audit=False) as con:
        assert con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1
    assert not target.with_name(target.name + "-wal").exists()
    assert not target.with_name(target.name + "-shm").exists()


@pytest.mark.parametrize("kind", ("application", "version", "schema"))
def test_snapshot_refuses_foreign_input_without_creating_output(tmp_path, kind):
    source, target = tmp_path / f"{kind}.sqlite", tmp_path / "copy.sqlite"
    with sqlite3.connect(source) as con:
        if kind == "application":
            con.execute("PRAGMA application_id=1")
        elif kind == "version":
            con.execute("PRAGMA application_id=1397051208")
            con.execute("PRAGMA user_version=999")
        else:
            con.execute("PRAGMA application_id=1397051208")
            con.execute("PRAGMA user_version=1")
            con.execute("CREATE TABLE scan(singleton INTEGER PRIMARY KEY)")
    with pytest.raises(ScanError):
        snapshot_scan(source, target)
    assert not target.exists()


def test_inspection_never_reads_body_blobs_and_honors_row_byte_cap(tmp_path, monkeypatch):
    path = tmp_path / "captured.sqlite"
    _finished(path, captured=True)
    import seohead.storage.history as history

    original = history.sqlite3.connect

    def guarded_connect(*args, **kwargs):
        con = original(*args, **kwargs)

        def authorizer(action, arg1, arg2, _db, _trigger):
            if action == sqlite3.SQLITE_READ and arg1 == "bodies" and arg2 == "data":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        con.set_authorizer(authorizer)
        return con

    monkeypatch.setattr(history.sqlite3, "connect", guarded_connect)
    inspected = inspect_scan(path, table="responses", limit=100, max_bytes=1)
    assert inspected["rows"] == []
    assert inspected["bytes"] == 0
    assert inspected["has_more"] is True


def test_default_name_and_history_warning_are_explicit(tmp_path):
    _finished(tmp_path / "one.sqlite")
    name = recommended_name("https://Example.test/", "12345678-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert name.endswith("_example.test_12345678.sqlite")
    path = new_scan_path(tmp_path, "https://example.test/", "abcdef12-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    assert path.parent == tmp_path and not path.exists()
    listing = list_scans(tmp_path)
    assert listing["history_warning_bytes"] == 20 * 1024 * 1024 * 1024
    assert listing["history_warning"] is False
