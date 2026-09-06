"""Legacy exports from scan.v1 remain deterministic and safely published."""

from __future__ import annotations

import json

import pytest

from seohead.storage import ScanError, import_run, open_scan
from seohead.storage.exports import export_run
from tests.test_scan_artifact import BUILD
from tests.test_scan_artifact import legacy_run as legacy_run


@pytest.fixture
def artifact(legacy_run, tmp_path):
    path = tmp_path / "scan.sqlite"
    import_run(legacy_run, path, producer_build=BUILD)
    return path


def _pages(path):
    return [json.loads(line) for line in (path / "pages.jsonl").read_text().splitlines()]


def _links(path):
    return [json.loads(line) for line in (path / "links.jsonl").read_text().splitlines()]


def test_export_reimports_all_observations_and_audit_bytes(legacy_run, artifact, tmp_path):
    before = artifact.read_bytes()
    exported = tmp_path / "exported"
    result = export_run(artifact, exported)
    assert result == {"ok": True, "path": str(exported), "counts": {"pages": 2, "links": 3}}
    assert artifact.read_bytes() == before
    assert {path.name for path in exported.iterdir()} == {
        "pages.jsonl",
        "links.jsonl",
        "audit.json",
    }
    assert (exported / "audit.json").read_bytes() == (legacy_run / "audit.json").read_bytes()
    assert _pages(exported) == _pages(legacy_run)
    assert _links(exported) == _links(legacy_run)

    reimported = tmp_path / "reimported.sqlite"
    import_run(exported, reimported, producer_build=BUILD)
    original = open_scan(artifact)
    restored = open_scan(reimported)
    try:
        assert [
            tuple(row) for row in original.execute("SELECT * FROM pages ORDER BY page_ordinal")
        ] == [tuple(row) for row in restored.execute("SELECT * FROM pages ORDER BY page_ordinal")]
        assert [tuple(row) for row in original.execute("SELECT * FROM links ORDER BY link_id")] == [
            tuple(row) for row in restored.execute("SELECT * FROM links ORDER BY link_id")
        ]
        assert (
            original.execute("SELECT document_json FROM audit").fetchone()[0]
            == restored.execute("SELECT document_json FROM audit").fetchone()[0]
        )
    finally:
        original.close()
        restored.close()


def test_repeated_exports_are_byte_identical(artifact, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    export_run(artifact, first)
    export_run(artifact, second)
    for name in ("pages.jsonl", "links.jsonl", "audit.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_export_omits_known_late_fields_that_were_absent_in_legacy_jsonl(legacy_run, tmp_path):
    pages = _pages(legacy_run)
    for page in pages:
        for field in (
            "content_frames",
            "content_frames_same_origin",
            "hreflang",
            "body_unavailable",
        ):
            page.pop(field)
    (legacy_run / "pages.jsonl").write_text("".join(json.dumps(page) + "\n" for page in pages))
    artifact = tmp_path / "older.sqlite"
    import_run(legacy_run, artifact, producer_build=BUILD)
    exported = tmp_path / "older-export"
    export_run(artifact, exported)
    for page in _pages(exported):
        assert not {
            "content_frames",
            "content_frames_same_origin",
            "hreflang",
            "body_unavailable",
        } & set(page)


def test_existing_and_symlinked_destinations_are_refused(artifact, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(existing, target_is_directory=True)
    for target in (existing, alias):
        with pytest.raises(ScanError, match="destination already exists"):
            export_run(artifact, target)
    assert existing.is_dir()
    assert alias.is_symlink()


def test_export_failure_removes_only_its_own_files(artifact, tmp_path, monkeypatch):
    import seohead.storage.exports as exports

    original = exports._write_jsonl

    def fail_links(path, rows, owned):
        if path.name == ".links.jsonl.tmp":
            raise OSError("injected write failure")
        original(path, rows, owned)

    monkeypatch.setattr(exports, "_write_jsonl", fail_links)
    destination = tmp_path / "failed-export"
    with pytest.raises(ScanError, match="injected write failure"):
        export_run(artifact, destination)
    assert not destination.exists()


def test_recovery_only_partialness_cannot_be_exported_as_complete(legacy_run, tmp_path):
    with (legacy_run / "links.jsonl").open("ab") as stream:
        stream.write(b'{"source":')
    artifact = import_run(legacy_run, tmp_path / "recovered.sqlite", producer_build=BUILD)
    before = artifact.read_bytes()
    destination = tmp_path / "cannot-represent-recovery"
    with pytest.raises(ScanError, match="would hide recovered crawl partialness"):
        export_run(artifact, destination)
    assert not destination.exists()
    assert artifact.read_bytes() == before


def test_partialness_recorded_in_audit_survives_export_and_reimport(legacy_run, tmp_path):
    path = legacy_run / "audit.json"
    audit = json.loads(path.read_text())
    audit["run"]["crawl_partial"] = True
    path.write_text(json.dumps(audit))
    artifact = import_run(legacy_run, tmp_path / "partial.sqlite", producer_build=BUILD)
    destination = tmp_path / "partial-export"
    export_run(artifact, destination)
    reimported = import_run(destination, tmp_path / "partial-again.sqlite", producer_build=BUILD)
    con = open_scan(reimported)
    try:
        assert con.execute("SELECT crawl_partial FROM scan").fetchone()[0] == 1
    finally:
        con.close()
