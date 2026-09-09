"""Unexecuted regression coverage: scan.v2 readers retain native validation guarantees."""

from __future__ import annotations

import sqlite3

import pytest

from seohead.storage import ScanError, open_scan
from seohead.storage.native_scan import NativeScan
from seohead.crawl.throttle import Throttle
from tests.test_batch_resource_graph import _v2_metadata


def _v2(path):
    with NativeScan.create(path, format_version="scan.v2", **_v2_metadata()):
        pass


@pytest.mark.parametrize(
    "tamper",
    [
        lambda con: con.execute("UPDATE scan SET config_fingerprint='tampered'"),
        lambda con: con.execute("UPDATE resume_state SET throttle_state_json='{}' WHERE singleton=1"),
    ],
)
def test_v2_reader_rejects_tampered_native_core_state(tmp_path, tamper):
    path = tmp_path / "scan.sqlite"
    _v2(path)
    with sqlite3.connect(path) as con:
        tamper(con)
        con.commit()

    with pytest.raises(ScanError):
        open_scan(path, require_audit=False)


def test_v2_reader_rejects_discovery_coverage_mismatch(tmp_path):
    path = tmp_path / "scan.sqlite"
    _v2(path)
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO discovery_ledger_coverage VALUES(1,'static',1,0,'complete','')"
        )
        con.commit()

    with pytest.raises(ScanError, match="discovery"):
        open_scan(path, require_audit=False)


def test_throttle_restores_native_v2_runtime_shape_without_counting_it_as_live_state():
    throttle = Throttle(max_concurrency=3)
    throttle.restore_state(
        {
            "schema_version": "scan_throttle.v2",
            "delay_seconds": 0.0,
            "concurrency": 1,
            "consecutive_ok": 0,
            "requests_used": 7,
        }
    )
    assert throttle.snapshot_state() == {
        "delay_seconds": 0.0,
        "concurrency": 1,
        "consecutive_ok": 0,
    }
