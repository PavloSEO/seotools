"""Saved replay capability reflects inputs and preserves older writer disclosures."""

import hashlib
import json
import sqlite3

import pytest

from seohead.storage import ScanError, open_scan
from tests.test_reanalysis_pages import _scan_with_raw_and_dom
from tests.test_scan_reanalysis_integration import _source


def _capability(path):
    con = open_scan(path, require_audit=False)
    try:
        return json.loads(con.execute("SELECT capabilities_json FROM scan").fetchone()[0])[
            "offline_reanalysis"
        ]
    finally:
        con.close()


def _set_capability(path, state):
    with sqlite3.connect(path) as con:
        capabilities = json.loads(con.execute("SELECT capabilities_json FROM scan").fetchone()[0])
        capabilities["offline_reanalysis"] = {
            "state": state,
            "reason": "older writer" if state == "unavailable" else "",
        }
        con.execute("UPDATE scan SET capabilities_json=?", (json.dumps(capabilities),))


def test_native_capability_supports_retained_raw_and_serialized_dom(tmp_path):
    path = tmp_path / "retained.sqlite"
    _scan_with_raw_and_dom(path)
    assert _capability(path)["state"] == "complete"


def test_old_unavailable_disclosure_is_read_without_upgrade(tmp_path):
    path = tmp_path / "old.sqlite"
    _source(path)
    _set_capability(path, "unavailable")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert _capability(path) == {"state": "unavailable", "reason": "older writer"}
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_missing_bodies_cannot_claim_complete_offline_reanalysis(tmp_path):
    path = tmp_path / "missing.sqlite"
    _source(path, body_mode="off")
    assert _capability(path)["state"] == "unavailable"
    _set_capability(path, "complete")
    with pytest.raises(ScanError, match="offline reanalysis capability disagrees"):
        _capability(path)
