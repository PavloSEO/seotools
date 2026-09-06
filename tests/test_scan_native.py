"""Offline native scan transaction and validation tests."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import fingerprint, load
from seohead.storage import ScanError
from seohead.storage.native_scan import Lease, NativeScan

SOURCE = Path(__file__).resolve().parents[1]


def _metadata(**overrides):
    config = load(overrides={"speed.min_delay_seconds": 0, **overrides})
    return {
        "start_url": "https://example.test/",
        "config": config,
        "config_fingerprint": fingerprint(config),
        "writer_version": "3.0.0",
        "writer_revision": "a" * 40,
        "runtime_versions": {
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
    }


def _runtime():
    return {
        "max_depth_reached": 1,
        "elapsed_seconds": 0.0,
        "circuit_timeout_streak": 0,
        "circuit_server_error_streak": 0,
        "crawl_delay_applied": None,
        "throttle": {"delay_seconds": 0.0, "concurrency": 1, "consecutive_ok": 0},
    }


def _record(url="https://example.test/"):
    return vars(
        PageRecord(
            url=url,
            content_type="text/html",
            title="Home",
            crawl_depth=0 if url == "https://example.test/" else 1,
        )
    )


def _link(source, destination, anchor=""):
    return {
        "source": source,
        "destination": destination,
        "anchor": anchor,
        "nofollow": False,
        "position": "",
        "rel": (),
        "target": "",
        "raw_href": "",
    }


def test_atomic_page_unit_and_idempotence(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        receipt = scan.commit_page(
            lease,
            _record(),
            links=[
                _link(lease.url, "https://example.test/a", "A"),
                _link(lease.url, "https://example.test/a", "Again"),
            ],
            forms=[{"page": lease.url, "method": "post", "action": "/send", "has_password": True}],
            decisions=[
                {"url": "https://example.test/no", "reason": "nofollow", "source": lease.url}
            ],
            discovered=[("https://example.test/a", 1)],
            query_reservations=[("/a", "x=1", "https://example.test/a?x=1")],
            runtime=_runtime(),
        )
        assert receipt.evidence_revision == 1
        assert scan.commit_page(
            lease,
            _record(),
            links=[
                _link(lease.url, "https://example.test/a", "A"),
                _link(lease.url, "https://example.test/a", "Again"),
            ],
            forms=[{"page": lease.url, "method": "post", "action": "/send", "has_password": True}],
            decisions=[
                {"url": "https://example.test/no", "reason": "nofollow", "source": lease.url}
            ],
            discovered=[("https://example.test/a", 1)],
            query_reservations=[("/a", "x=1", "https://example.test/a?x=1")],
            runtime=_runtime(),
        ).already_committed
    inspected = NativeScan.inspect(path)
    assert inspected["counts"] == {
        "pages": 1,
        "links": 2,
        "forms": 1,
        "decisions": 1,
        "frontier": 2,
        "query_variants": 1,
    }


def test_commit_failure_rolls_back_everything(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]

        def full(point):
            if point == "after_observations":
                raise __import__("sqlite3").OperationalError("database or disk is full")

        scan.failpoint = full
        with pytest.raises(Exception, match="full"):
            scan.commit_page(
                lease,
                _record(),
                links=[_link(lease.url, "https://example.test/a")],
                runtime=_runtime(),
            )
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
        assert scan.con.execute("SELECT state FROM frontier").fetchone()[0] == "inflight"


def test_recover_and_live_snapshot_are_portable(tmp_path):
    path, snap = tmp_path / "scan.sqlite", tmp_path / "copy.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        scan.commit_page(lease, _record(), runtime=_runtime())
        scan.snapshot(snap)
        assert not snap.with_name(snap.name + "-wal").exists()
    assert NativeScan.inspect(snap)["counts"]["pages"] == 1
    with NativeScan.open(
        path, expected_start_url="https://example.test/", expected_config=_metadata()["config"]
    ) as reopened:
        reopened.enqueue([("https://example.test/a", 1)])
        reopened.claim(1)
    with NativeScan.open(path) as reopened:
        assert reopened.recover_inflight() == 1


def test_second_writer_refused(tmp_path):
    path = tmp_path / "scan.sqlite"
    with (
        NativeScan.create(path, **_metadata()),
        pytest.raises((BlockingIOError, ScanError, OSError)),
    ):
        NativeScan.open(path)


def test_sigkill_leaves_no_partial_page(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
    script = f"""import os, signal, sys; sys.path[:0]=[{str(SOURCE)!r},{str(Path(__file__).parent)!r}]; from seohead.storage.native_scan import NativeScan; from tests.test_scan_native import _link,_record,_runtime; s=NativeScan.open({str(path)!r}); l=s.claim(1)[0]; s.failpoint=lambda p: os.kill(os.getpid(), signal.SIGKILL) if p == "after_observations" else None; s.commit_page(l,_record(),links=[_link(l.url,"https://example.test/a")],runtime=_runtime())"""
    result = subprocess.run([sys.executable, "-c", script], check=False)
    assert result.returncode != 0
    with NativeScan.open(path) as scan:
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
        assert scan.recover_inflight() == 1


def test_over_budget_query_becomes_atomic_rejection_not_rollback(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(**{"limits.max_query_variants_per_path": 1})) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            _record(),
            links=[_link(lease.url, "https://example.test/a")],
            query_reservations=[
                ("/catalog", "one=1", "https://example.test/catalog?one=1"),
                ("/catalog", "two=2", "https://example.test/catalog?two=2"),
            ],
            runtime=_runtime(),
        )
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1
        assert scan.con.execute("SELECT COUNT(*) FROM query_variants").fetchone()[0] == 1
        assert tuple(
            scan.con.execute(
                "SELECT url, reason FROM decisions WHERE reason='query_variants_limit'"
            ).fetchone()
        ) == ("https://example.test/catalog?two=2", "query_variants_limit")
        assert (
            scan.con.execute(
                "SELECT state FROM frontier JOIN urls USING(url_id) WHERE url=?",
                ("https://example.test/catalog?two=2",),
            ).fetchone()[0]
            == "excluded"
        )


def test_terminal_scan_refuses_mutation_but_inspects(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.finish_without_audit()
        with pytest.raises(ScanError, match="immutable"):
            scan.enqueue([("https://example.test/", 0)])
    assert NativeScan.inspect(path)["scan"]["lifecycle"] == "finished"


def test_strict_metadata_and_snapshot_space_validation(tmp_path):
    path = tmp_path / "scan.sqlite"
    wrong = _metadata()
    wrong["config_fingerprint"] = "wrong"
    with pytest.raises(ScanError, match="fingerprint"):
        NativeScan.create(tmp_path / "wrong.sqlite", **wrong)
    with (
        NativeScan.create(path, **_metadata()) as scan,
        pytest.raises(ScanError, match="insufficient free space"),
    ):
        scan.snapshot(tmp_path / "too-large.sqlite", reserve_bytes=10**18)
    con = sqlite3.connect(path)
    con.execute("UPDATE resume_state SET throttle_state_json='{}'")
    con.commit()
    con.close()
    with pytest.raises(ScanError, match="runtime state"):
        NativeScan.inspect(path)


def test_native_inspect_rejects_gapped_frontier_ordinals(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0), ("https://example.test/a", 1)])
    con = sqlite3.connect(path)
    con.execute("UPDATE frontier SET queue_ordinal=99 WHERE queue_ordinal=0")
    con.commit()
    con.close()
    with pytest.raises(ScanError, match="queue ordinals"):
        NativeScan.inspect(path)


def test_native_inspect_rejects_null_current_page_evidence(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(scan.claim(1)[0], _record(), runtime=_runtime())
    con = sqlite3.connect(path)
    con.execute("UPDATE pages SET content_frames=NULL")
    con.commit()
    con.close()
    with pytest.raises(ScanError, match="current PageRecord"):
        NativeScan.inspect(path)


def test_writer_open_never_creates_or_mutates_unknown_files(tmp_path):
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(ScanError, match="existing regular"):
        NativeScan.open(missing)
    assert not missing.exists()
    foreign = tmp_path / "foreign.sqlite"
    foreign.write_bytes(b"not sqlite")
    before = foreign.read_bytes()
    with pytest.raises(ScanError, match="cannot inspect"):
        NativeScan.open(foreign)
    assert foreign.read_bytes() == before
    assert not foreign.with_name(foreign.name + "-wal").exists()


def test_forged_lease_and_invalid_inputs_rollback_before_page_insert(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        forged = Lease(lease.url_id, lease.url, lease.depth + 1, lease.queue_ordinal)
        with pytest.raises(ScanError, match="forged"):
            scan.commit_page(forged, _record(), runtime=_runtime())
        with pytest.raises(ScanError, match="link input"):
            scan.commit_page(
                lease,
                _record(),
                links=[{**_link(lease.url, "https://example.test/a"), "nofollow": "false"}],
                runtime=_runtime(),
            )
        invalid = _record()
        invalid["body_unavailable"] = "invented"
        with pytest.raises(ScanError, match="body_unavailable"):
            scan.commit_page(lease, invalid, runtime=_runtime())
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0


def test_page_ordinals_ignore_excluded_frontier_entries(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(**{"limits.max_query_variants_per_path": 1})) as scan:
        scan.enqueue([("https://example.test/", 0)])
        first = scan.claim(1)[0]
        scan.commit_page(
            first,
            _record(),
            discovered=[("https://example.test/a", 1)],
            query_reservations=[
                ("/p", "one=1", "https://example.test/p?one=1"),
                ("/p", "two=2", "https://example.test/p?two=2"),
            ],
            runtime=_runtime(),
        )
        second = scan.claim(1)[0]
        assert second.url == "https://example.test/a"
        scan.commit_page(second, _record(second.url), runtime=_runtime())
        assert [
            row[0]
            for row in scan.con.execute("SELECT page_ordinal FROM pages ORDER BY page_ordinal")
        ] == [0, 1]


def test_context_rejects_raw_html_and_failed_scan_stays_inspectable(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        with pytest.raises(ScanError, match="robots_blocked_url"):
            scan.commit_page(
                lease,
                _record(),
                runtime=_runtime(),
                context=[
                    {
                        "kind": "native_start_page",
                        "item_key": "run",
                        "payload_version": "scan_context.v1",
                        "payload_json": '{"html":"secret"}',
                        "completeness": "complete",
                        "reason": "no",
                    }
                ],
            )
        scan.interrupt("test")
    assert NativeScan.inspect(path)["scan"]["lifecycle"] == "interrupted"
    con = sqlite3.connect(path)
    con.execute("UPDATE scan SET lifecycle='failed', crawl_partial=1, finish_reason='disk_full'")
    con.commit()
    con.close()
    assert NativeScan.inspect(path)["scan"]["lifecycle"] == "failed"


def test_documented_robots_context_is_the_only_accepted_context(tmp_path):
    import json

    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(**{"robots.policy": "report_only"})) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            _record(),
            runtime=_runtime(),
            context=[
                {
                    "kind": "robots_blocked_url",
                    "item_key": f"url:{lease.url_id}",
                    "payload_version": "scan_context.v1",
                    "completeness": "complete",
                    "reason": "robots.txt",
                    "payload_json": json.dumps(
                        {"url_id": lease.url_id, "token": "SEOHEAD-Tools", "policy": "report_only"}
                    ),
                }
            ],
        )
        assert (
            scan.con.execute(
                "SELECT COUNT(*) FROM context_items WHERE kind='robots_blocked_url'"
            ).fetchone()[0]
            == 1
        )
    assert NativeScan.inspect(path)["counts"]["pages"] == 1


def test_form_input_is_strict_and_cannot_silently_cast_false(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        with pytest.raises(ScanError, match="form input"):
            scan.commit_page(
                lease,
                _record(),
                runtime=_runtime(),
                forms=[
                    {
                        "page": lease.url,
                        "method": "post",
                        "action": "/send",
                        "has_password": "false",
                    }
                ],
            )


def test_terminal_scan_cannot_reopen_writer_and_inspection_timeout_is_bounded(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.finish_without_audit()
    with pytest.raises(ScanError, match="cannot be opened for writing"):
        NativeScan.open(path)
    with pytest.raises(ScanError, match="timeout"):
        NativeScan.inspect(path, timeout_seconds=0)


def test_rejected_query_does_not_make_page_ordinal_follow_frontier_ordinal(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(**{"limits.max_query_variants_per_path": 1})) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            _record(),
            discovered=[("https://example.test/next", 1)],
            query_reservations=[
                ("/same", "a=1", "https://example.test/same?a=1"),
                ("/same", "b=2", "https://example.test/same?b=2"),
            ],
            runtime=_runtime(),
        )
        next_lease = scan.claim(1)[0]
        assert next_lease.queue_ordinal > 1
        scan.commit_page(next_lease, _record(next_lease.url), runtime=_runtime())
        assert scan.con.execute("SELECT MAX(page_ordinal) FROM pages").fetchone()[0] == 1
