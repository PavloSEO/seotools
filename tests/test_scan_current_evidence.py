"""The prerelease scan schema preserves observations added while A was reviewed."""

import json
import sqlite3

import pytest

from seohead.reports import build_report
from seohead.storage import ScanError, import_run, open_scan, read_audit
from tests.test_scan_artifact import BUILD
from tests.test_scan_artifact import legacy_run as legacy_run

NEW_FIELDS = ("content_frames", "content_frames_same_origin", "hreflang", "body_unavailable")


def test_pre_refresh_records_import_as_unknown_and_current_values_survive(legacy_run, tmp_path):
    path = legacy_run / "pages.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0].pop("meta_refresh")
    rows[1]["meta_refresh"] = "0; url=/next"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    out = import_run(legacy_run, tmp_path / "scan.sqlite", producer_build=BUILD)
    with open_scan(out) as con:
        assert [
            row[0] for row in con.execute("SELECT meta_refresh FROM pages ORDER BY page_ordinal")
        ] == [None, "0; url=/next"]
        header = con.execute(
            "SELECT crawl_partial,capabilities_json,limitations_json FROM scan"
        ).fetchone()
        assert header[0] == 0
        assert json.loads(header[1])["pages"]["state"] == "partial"
        assert "meta_refresh" in header[2]


def test_current_frames_alternates_and_unavailable_body_survive(legacy_run, tmp_path):
    out = import_run(legacy_run, tmp_path / "scan.sqlite", producer_build=BUILD)
    con = open_scan(out)
    try:
        rows = list(con.execute("SELECT * FROM pages ORDER BY page_ordinal"))
        assert rows[0]["content_frames"] == 2
        assert rows[0]["content_frames_same_origin"] == 1
        assert json.loads(rows[0]["hreflang_json"]) == [
            {"lang": "FR", "raw_href": "/fr/", "url": "https://example.com/fr/"},
            {"lang": "x-default", "raw_href": "/", "url": "https://example.com/"},
        ]
        assert rows[1]["body_unavailable"] == "oversized"
        assert rows[1]["status_code"] == 200
        assert rows[1]["title"] == ""
        assert con.execute("SELECT COUNT(*) FROM bodies").fetchone()[0] == 0
    finally:
        con.close()


@pytest.mark.parametrize("old_rows", [(0, 1), (0,)])
def test_older_and_mixed_rows_keep_unknowns_without_making_crawl_partial(
    legacy_run, tmp_path, old_rows
):
    path = legacy_run / "pages.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    for index in old_rows:
        for key in NEW_FIELDS:
            rows[index].pop(key)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    original_audit = (legacy_run / "audit.json").read_bytes()
    out = import_run(legacy_run, tmp_path / "scan.sqlite", producer_build=BUILD)
    con = open_scan(out)
    try:
        pages = list(con.execute("SELECT * FROM pages ORDER BY page_ordinal"))
        for index in old_rows:
            assert [
                pages[index][name]
                for name in (
                    "content_frames",
                    "content_frames_same_origin",
                    "hreflang_json",
                    "body_unavailable",
                )
            ] == [None] * 4
        if len(old_rows) == 1:
            assert pages[1]["body_unavailable"] == "oversized"
        scan = con.execute("SELECT * FROM scan").fetchone()
        assert scan["crawl_partial"] == 0
        assert json.loads(scan["capabilities_json"])["pages"]["state"] == "partial"
        assert json.loads(scan["capabilities_json"])["links"]["state"] == "complete"
        assert "legacy page fields unavailable" in scan["limitations_json"]
        assert (
            con.execute("SELECT document_json FROM audit").fetchone()[0].encode() == original_audit
        )
    finally:
        con.close()
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    assert build_report(str(legacy_run / "audit.json"), "json", str(first))["ok"]
    assert build_report(read_audit(out), "json", str(second))["ok"]
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize(
    "field,value",
    [
        ("content_frames", -1),
        ("content_frames_same_origin", 3),
        ("content_frames", None),
        ("content_frames", False),
        ("hreflang", [{"lang": "en", "url": "/"}]),
        ("hreflang", [{"lang": 1, "raw_href": "/", "url": "/"}]),
        ("body_unavailable", "invented_reason"),
    ],
)
def test_invalid_new_evidence_is_refused(legacy_run, tmp_path, field, value):
    path = legacy_run / "pages.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0][field] = value
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ScanError):
        import_run(legacy_run, tmp_path / "bad.sqlite", producer_build=BUILD)
    assert not (tmp_path / "bad.sqlite").exists()


def test_duplicate_and_malformed_hreflang_declarations_are_data(legacy_run, tmp_path):
    path = legacy_run / "pages.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["hreflang"] = [{"lang": "BAD", "raw_href": "", "url": ""}] * 2
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    out = import_run(legacy_run, tmp_path / "scan.sqlite", producer_build=BUILD)
    with sqlite3.connect(out) as con:
        assert (
            json.loads(
                con.execute("SELECT hreflang_json FROM pages WHERE page_ordinal=0").fetchone()[0]
            )
            == rows[0]["hreflang"]
        )


def test_racing_destination_creation_has_an_actionable_refusal(legacy_run, tmp_path, monkeypatch):
    import os

    def racing_link(source, destination):
        destination.write_bytes(b"another completed artifact")
        raise FileExistsError("already created")

    monkeypatch.setattr(os, "link", racing_link)
    out = tmp_path / "exists.sqlite"
    with pytest.raises(ScanError, match="choose a new --out path"):
        import_run(legacy_run, out, producer_build=BUILD)
    assert out.read_bytes() == b"another completed artifact"
