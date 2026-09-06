"""Atomic derived-scan storage tests without replay orchestration."""

from __future__ import annotations

import hashlib
import json

import pytest

from seohead.storage import ScanError, open_scan
from seohead.storage.native_scan import NativeScan
from seohead.storage.reanalysis import derived_scan
from tests.test_scan_native import _metadata, _record, _runtime


def test_derived_native_capture_cannot_claim_legacy_cache_transport(tmp_path):
    import sqlite3

    from seohead.servers.reanalysis_handlers import reanalyze_scan
    from tests.test_scan_reanalysis_integration import _source

    source, derived = tmp_path / "source.sqlite", tmp_path / "derived.sqlite"
    _source(source)
    reanalyze_scan(str(source), str(derived), producer_build="b" * 40)
    with sqlite3.connect(derived) as con:
        con.execute("UPDATE responses SET transport_source='cache'")
    with pytest.raises(ScanError, match="cache transport"):
        open_scan(derived)


def test_derived_scan_publishes_new_parented_finished_artifact_without_mutating_source(tmp_path):
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "derived.sqlite"
    with NativeScan.create(source, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(scan.claim(1)[0], _record(), runtime=_runtime())
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    with derived_scan(
        source,
        derived,
        producer_version="reanalysis-test",
        producer_revision="b" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
    ) as (writer, source_con):
        assert source_con.execute("PRAGMA query_only").fetchone()[0] == 1
        assert writer.con.execute("SELECT source_kind FROM scan").fetchone()[0] == "reanalysis"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    with open_scan(derived, require_audit=False) as reader:
        scan = reader.execute("SELECT * FROM scan").fetchone()
        context = json.loads(
            reader.execute(
                "SELECT payload_json FROM context_items WHERE kind='reanalysis_provenance'"
            ).fetchone()[0]
        )
    assert scan["source_kind"] == "reanalysis"
    assert scan["lifecycle"] == "finished"
    assert scan["parent_scan_uuid"] == context["parent_scan_uuid"]
    assert scan["evidence_revision"] == context["derived_evidence_revision"]
    assert context["derived_evidence_revision"] == context["source_evidence_revision"] + 1
    assert json.loads(scan["capabilities_json"])["resume"]["state"] == "unavailable"
    assert json.loads(scan["capabilities_json"])["offline_reanalysis"]["state"] == "complete"
    with pytest.raises(ScanError, match="derived reanalysis artifacts cannot resume"):
        NativeScan.open(derived)


def test_derived_scan_failure_publishes_no_output(tmp_path):
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "derived.sqlite"
    with NativeScan.create(source, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(scan.claim(1)[0], _record(), runtime=_runtime())
    with (
        pytest.raises(ScanError, match="missing retained HTML"),
        derived_scan(
            source,
            derived,
            producer_version="reanalysis-test",
            producer_revision="b" * 40,
            runtime_versions={
                "python": "test",
                "sqlite": "test",
                "httpx": "test",
                "lxml": "test",
                "beautifulsoup4": "test",
            },
        ),
    ):
        raise ScanError("missing retained HTML")
    assert not derived.exists()


def test_derived_scan_can_chain_while_preserving_original_capture_provenance(tmp_path):
    source = tmp_path / "source.sqlite"
    first = tmp_path / "first.sqlite"
    second = tmp_path / "second.sqlite"
    versions = {
        "python": "test",
        "sqlite": "test",
        "httpx": "test",
        "lxml": "test",
        "beautifulsoup4": "test",
    }
    with NativeScan.create(source, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(scan.claim(1)[0], _record(), runtime=_runtime())
        capture_uuid = scan.con.execute("SELECT scan_uuid FROM scan").fetchone()[0]
    with derived_scan(source, first, "one", "b" * 40, versions):
        pass
    with derived_scan(first, second, "two", "c" * 40, versions):
        pass
    with open_scan(second, require_audit=False) as reader:
        scan = reader.execute("SELECT scan_uuid,parent_scan_uuid FROM scan").fetchone()
        provenance = json.loads(
            reader.execute(
                "SELECT payload_json FROM context_items WHERE kind='reanalysis_provenance'"
            ).fetchone()[0]
        )
    assert scan["parent_scan_uuid"] != capture_uuid
    assert provenance["capture_scan_uuid"] == capture_uuid
    assert provenance["capture_writer_revision"] == "a" * 40
    assert provenance["capture_writer_version"] == "3.0.0"


def test_reader_refuses_malformed_derived_provenance(tmp_path):
    source = tmp_path / "source.sqlite"
    derived = tmp_path / "derived.sqlite"
    versions = {
        "python": "test",
        "sqlite": "test",
        "httpx": "test",
        "lxml": "test",
        "beautifulsoup4": "test",
    }
    with NativeScan.create(source, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(scan.claim(1)[0], _record(), runtime=_runtime())
    with derived_scan(source, derived, "one", "b" * 40, versions):
        pass
    import sqlite3

    with sqlite3.connect(derived) as con:
        con.execute("UPDATE context_items SET payload_json='{}' WHERE kind='reanalysis_provenance'")
    with pytest.raises(ScanError, match=r"provenance|context"):
        open_scan(derived, require_audit=False)
