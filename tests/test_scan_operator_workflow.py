"""One bounded, offline operator workflow over retained native scan evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
import zlib
from pathlib import Path

from seohead import cli
from seohead.storage import read_audit
from tests.test_scan_artifact_office import frozen_office_clock as frozen_office_clock
from tests.test_scan_reanalysis_integration import (
    MIT_SYNTHETIC_HTML,
    _forbid_network,
    _source,
)


def _cli(capsys, *argv: str) -> dict:
    assert cli.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


def _audit_bytes(path: Path) -> bytes:
    with sqlite3.connect(path) as con:
        return (
            con.execute("SELECT document_json FROM audit WHERE singleton=1").fetchone()[0].encode()
        )


def _documented_stdlib_read(path: Path) -> None:
    """Exercise the documented read-only SQL and bounded body-decoding recipe."""
    deadline = time.monotonic() + 30
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        con.execute("PRAGMA trusted_schema=OFF")
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        assert con.execute("PRAGMA application_id").fetchone()[0] == 1397051208
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        identity = con.execute(
            "SELECT scan_uuid, format_version, writer_revision, source_kind, lifecycle, "
            "crawl_partial, corpus_partial, capabilities_json FROM scan WHERE singleton=1"
        ).fetchone()
        assert identity[1] == "scan.v1" and identity[4] == "finished"
        assert con.execute("SELECT format_version FROM scan").fetchone()[0] == "scan.v1"
        assert con.execute(
            "SELECT status_code, COUNT(*) AS pages FROM pages "
            "GROUP BY status_code ORDER BY pages DESC, status_code"
        ).fetchall() == [(200, 1)]
        assert con.execute(
            "SELECT u.url, COUNT(*) AS inlink_occurrences "
            "FROM links AS l JOIN urls AS u ON u.url_id = l.destination_url_id "
            "GROUP BY l.destination_url_id ORDER BY inlink_occurrences DESC, u.url LIMIT 20"
        ).fetchone() == ("https://example.test/", 1)
        sha256, codec, decoded_bytes, data = con.execute(
            "SELECT sha256, codec, decoded_bytes, data FROM bodies LIMIT 1"
        ).fetchone()
        if codec == "identity":
            decoded = data
        else:
            decoder = zlib.decompressobj()
            decoded = decoder.decompress(data, 5 * 1024 * 1024 + 1)
            assert decoder.eof and not decoder.unconsumed_tail and not decoder.unused_data
        assert len(decoded) == decoded_bytes <= 5 * 1024 * 1024
        assert hashlib.sha256(decoded).hexdigest() == sha256
        assert decoded == MIT_SYNTHETIC_HTML
    finally:
        con.close()


def test_offline_saved_scan_operator_workflow(tmp_path, monkeypatch, capsys, frozen_office_clock):
    """Operators can inspect, preserve, reanalyze, compare, and report one artifact offline."""
    source = tmp_path / "source.sqlite"
    snapshot = tmp_path / "snapshot.sqlite"
    derived = tmp_path / "derived.sqlite"
    _source(source)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    source_audit = read_audit(source)
    assert source_audit["issues"]

    # FastMCP may allocate a local socketpair while creating an event loop, so do this
    # before the retained-evidence phase prohibits every socket and network route.
    loop = asyncio.new_event_loop()
    try:
        from seohead.servers.mcp_server import build_server

        server = build_server()
        attempts = _forbid_network(monkeypatch, [])

        listed = _cli(capsys, "scan", "list", "--directory", str(tmp_path))
        mcp_listed = server._tool_manager.get_tool("seo_scan_list").fn(directory=str(tmp_path))
        assert listed["total"] == mcp_listed["total"] == 1
        inspected = _cli(capsys, "scan", "inspect", "--input", str(source), "--table", "pages")
        assert inspected["rows"][0]["title"] == "Owned iframe fixture"

        copied = _cli(capsys, "scan", "snapshot", "--input", str(source), "--out", str(snapshot))
        assert Path(copied["snapshot"]) == snapshot
        assert _audit_bytes(snapshot) == _audit_bytes(source)

        assert _cli(capsys, "scan", "pin", "--input", str(snapshot))["pinned"] is True
        assert _cli(capsys, "scan", "pin", "--input", str(snapshot), "--unpin")["pinned"] is False
        body = _cli(
            capsys,
            "scan",
            "body-diff",
            "--left",
            str(source),
            "--right",
            str(snapshot),
            "--url",
            "https://example.test/",
        )
        assert body["status"] == "unchanged"

        reanalysis = _cli(
            capsys,
            "scan",
            "reanalyze",
            "--input",
            str(source),
            "--out",
            str(derived),
            "--producer-build",
            "b" * 40,
        )
        assert reanalysis["source_kind"] == "reanalysis" and reanalysis["audit_available"] is True
        assert read_audit(derived)["issues"] == source_audit["issues"]
        assert _cli(capsys, "scan", "inspect", "--input", str(derived))["rows"]
        assert _cli(capsys, "scan", "list", "--directory", str(tmp_path))["total"] == 3

        for fmt in ("json", "md", "csv", "xlsx", "docx"):
            outputs = []
            for name, scan in (("source", source), ("snapshot", snapshot)):
                output = tmp_path / name / f"report.{fmt}"
                assert _cli(
                    capsys,
                    "report-build",
                    "--audit",
                    str(scan),
                    "--format",
                    fmt,
                    "--out",
                    str(output),
                )["ok"]
                outputs.append(output)
            assert outputs[0].read_bytes() == outputs[1].read_bytes()
            if fmt == "csv":
                assert (
                    outputs[0].with_suffix(".pages.csv").read_bytes()
                    == outputs[1].with_suffix(".pages.csv").read_bytes()
                )

        for after in (snapshot, derived):
            comparison = _cli(
                capsys, "compare-crawls", "--before", str(source), "--after", str(after)
            )
            assert comparison["schema_version"] == "compare.v1"
            assert comparison["summary"] == {
                "entered": 0,
                "left": 0,
                "appeared": 0,
                "disappeared": 0,
                "by_check": {},
            }

        _documented_stdlib_read(source)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha
        assert attempts == {}
    finally:
        loop.close()
