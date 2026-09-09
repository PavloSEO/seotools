"""Unexecuted regression coverage: scan.v2 readers retain native validation guarantees."""

from __future__ import annotations

import sqlite3

import pytest

from seohead.storage import ScanError, open_scan
from seohead.storage.native_scan import NativeScan
from tests.test_batch_resource_graph import _v2_metadata


def _v2(path):
    with NativeScan.create(path, format_version="scan.v2", **_v2_metadata()):
        pass


@pytest.mark.parametrize(
    "tamper",
    [
        lambda con: con.execute("UPDATE scan SET config_fingerprint='tampered'"),
        lambda con: con.execute("UPDATE resume_state SET elapsed_seconds=-1 WHERE singleton=1"),
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
