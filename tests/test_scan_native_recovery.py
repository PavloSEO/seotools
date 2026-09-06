"""Process-loss, disk-full and reader-contention evidence for scan commits."""

from __future__ import annotations

import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from seohead.storage import ScanError
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _metadata, _record, _runtime


@pytest.mark.parametrize(
    "point",
    [
        "before_claim",
        "after_claim",
        "after_page",
        "after_observations",
        "after_query_budget",
        "after_frontier",
        "after_runtime",
        "before_commit",
        "after_commit",
    ],
)
def test_sigkill_preserves_one_whole_evidence_revision(tmp_path, point):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
    script = """
import os, signal, sys
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _record, _runtime, _link
path, point = sys.argv[1:]
with NativeScan.open(path) as scan:
    def stop(name):
        if name == point:
            os.kill(os.getpid(), signal.SIGKILL)
    scan.failpoint = stop
    lease = scan.claim(1)[0]
    runtime = _runtime()
    runtime.update(max_depth_reached=3, elapsed_seconds=123.0, circuit_timeout_streak=2)
    scan.commit_page(
        lease, _record(),
        links=[_link(lease.url, 'https://example.test/next', 'First'),
               _link(lease.url, 'https://example.test/next', 'Second')],
        forms=[{'page':lease.url, 'method':'post', 'action':'/send', 'has_password':True}],
        decisions=[{'url':'https://example.test/excluded', 'reason':'nofollow', 'source':lease.url}],
        discovered=[('https://example.test/next', 1)],
        query_reservations=[('/next', 'a=1', 'https://example.test/next?a=1')],
        runtime=runtime,
    )
"""
    # The subprocess imports the same checkout and fixture without site installation.
    prefix = f"import sys; sys.path[:0] = {[str(Path(__file__).parents[1]), str(Path(__file__).parent)]!r}\n"
    killed = subprocess.run(
        [sys.executable, "-c", prefix + script, str(path), point],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    expected_revision = int(point == "after_commit")
    with NativeScan.open(path) as scan:
        header = scan.con.execute("SELECT evidence_revision FROM scan").fetchone()
        assert header[0] == expected_revision
        for table, count in {
            "pages": 1,
            "links": 2,
            "forms": 1,
            "decisions": 1,
            "query_variants": 1,
        }.items():
            assert (
                scan.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                == count * expected_revision
            )
        runtime = scan.con.execute(
            "SELECT elapsed_seconds, circuit_timeout_streak FROM resume_state"
        ).fetchone()
        assert tuple(runtime) == ((123.0, 2) if expected_revision else (0.0, 0))
        scan.recover_inflight()
        next_urls = [lease.url for lease in scan.claim(1)]
        assert next_urls == [
            "https://example.test/next" if expected_revision else "https://example.test/"
        ]


def test_real_sqlite_full_preserves_previous_page_and_resume(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(
            scan.claim(1)[0],
            _record(),
            discovered=[("https://example.test/next", 1)],
            runtime=_runtime(),
        )
        lease = scan.claim(1)[0]
        page_count = scan.con.execute("PRAGMA page_count").fetchone()[0]
        scan.con.execute(f"PRAGMA max_page_count={page_count + 1}")
        record = _record(lease.url)
        record["title"] = "x" * 100_000
        with pytest.raises(sqlite3.OperationalError, match="full") as error:
            scan.commit_page(lease, record, runtime=_runtime())
        # Python 3.10 exposes the real SQLite failure only through its message;
        # newer runtimes additionally expose the numeric extended error code.
        if sys.version_info >= (3, 11):
            assert error.value.sqlite_errorcode == sqlite3.SQLITE_FULL
    assert NativeScan.inspect(path)["counts"]["pages"] == 1
    with NativeScan.open(path) as scan:
        assert scan.recover_inflight() == 1
        assert scan.claim(1)[0].url == "https://example.test/next"


@pytest.mark.parametrize("exception", [OSError("injected I/O failure"), KeyboardInterrupt()])
def test_io_failure_and_cancellation_leave_claim_recoverable(tmp_path, exception):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]

        def fail(point):
            if point == "before_commit":
                raise exception

        scan.failpoint = fail
        with pytest.raises(type(exception)):
            scan.commit_page(lease, _record(), runtime=_runtime())
        scan.failpoint = None
        scan.interrupt("io_failure" if isinstance(exception, OSError) else "cancelled")
    assert NativeScan.inspect(path)["scan"]["lifecycle"] == "interrupted"
    with NativeScan.open(path) as scan:
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
        assert scan.recover_inflight() == 1


def test_snapshot_is_independent_of_writer_and_source_directory(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    path, snapshot = source / "scan.sqlite", destination / "copy.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(scan.claim(1)[0], _record(), runtime=_runtime())
        scan.snapshot(snapshot)
        scan.enqueue([("https://example.test/later", 1)])
        scan.commit_page(
            scan.claim(1)[0], _record("https://example.test/later"), runtime=_runtime()
        )
        assert NativeScan.inspect(snapshot)["counts"]["pages"] == 1
        assert NativeScan.inspect(path)["counts"]["pages"] == 2
    assert list(destination.iterdir()) == [snapshot]
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as con:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("SELECT evidence_revision FROM scan").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def test_snapshot_rejects_existing_destination_without_mutation(tmp_path):
    path, target = tmp_path / "scan.sqlite", tmp_path / "copy.sqlite"
    target.write_bytes(b"keep me")
    with (
        NativeScan.create(path, **_metadata()) as scan,
        pytest.raises(ScanError, match="already exists"),
    ):
        scan.snapshot(target)
    assert target.read_bytes() == b"keep me"


def test_hardlink_alias_cannot_acquire_another_writer(tmp_path):
    import os

    path, alias = tmp_path / "scan.sqlite", tmp_path / "alias.sqlite"
    with NativeScan.create(path, **_metadata()):
        os.link(path, alias)
        with pytest.raises(ScanError, match="link"):
            NativeScan.open(alias)


def test_contiguous_commit_order_is_enforced(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(**{"speed.concurrency": 2})) as scan:
        scan.enqueue([("https://example.test/", 0), ("https://example.test/next", 1)])
        first, second = scan.claim(2)
        with pytest.raises(ScanError, match=r"order|prefix"):
            scan.commit_page(second, _record(second.url), runtime=_runtime())
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
        scan.commit_page(first, _record(), runtime=_runtime())
        scan.commit_page(second, _record(second.url), runtime=_runtime())
        assert scan.con.execute("SELECT evidence_revision FROM scan").fetchone()[0] == 2


def test_reader_blocks_finalization_with_a_finite_recoverable_outcome(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM pages").fetchone()
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(scan.claim(1)[0], _record(), runtime=_runtime())
        started = time.monotonic()
        assert scan.finish_without_audit(timeout_seconds=0.1) is False
        assert time.monotonic() - started < 2
        header = NativeScan.inspect(path)["scan"]
        assert header["lifecycle"] == "interrupted"
        assert header["finish_reason"] == "finalization_blocked"
        assert header["crawl_partial"] == 0  # Collection and file finalization are independent.
        reader.close()
        assert scan.resume_or_finalize() is True
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert con.execute("SELECT lifecycle FROM scan").fetchone()[0] == "finished"


def test_snapshot_cancellation_removes_only_its_temporary_file(tmp_path):
    path, target = tmp_path / "scan.sqlite", tmp_path / "snapshot.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        existing_names = set(tmp_path.iterdir())
        with pytest.raises(ScanError, match="cancelled"):
            scan.snapshot(target, cancelled=lambda: True)
        assert not target.exists()
        assert set(tmp_path.iterdir()) == existing_names
        assert NativeScan.inspect(path)["counts"]["pages"] == 0


def test_claims_cannot_exceed_the_inflight_window(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0), ("https://example.test/next", 1)])
        first = scan.claim(100)
        assert len(first) == 1
        assert scan.claim(100) == []
        scan.commit_page(first[0], _record(), runtime=_runtime())
        assert scan.claim(100)[0].url == "https://example.test/next"


def test_wal_backpressure_stops_before_more_work_and_recovers(tmp_path, monkeypatch):
    import seohead.storage.native_scan as native

    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.con.execute("PRAGMA busy_timeout=50")
        reader = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM pages").fetchone()
        scan.enqueue([("https://example.test/", 0)])
        monkeypatch.setattr(native, "WAL_BACKPRESSURE_BYTES", 1)
        with pytest.raises(ScanError, match="WAL backpressure"):
            scan.claim(1)
        assert scan.con.execute("SELECT state FROM frontier").fetchone()[0] == "queued"
        reader.close()
        assert scan.claim(1)[0].url == "https://example.test/"
