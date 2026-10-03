"""Offline capacity evidence uses the native writer and names blocked stages."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import profile_scan_capacity as profile
from seohead.crawl.settings import ConfigError
from seohead.storage.native_scan import NativeScan


def _args(scan: Path, **changes) -> argparse.Namespace:
    values = {
        "scan": scan,
        "pages": 3,
        "links_per_page": 1,
        "body_bytes": 64,
        "dom_bytes": 64,
        "interrupt_after": 0,
        "experimental_synthetic": False,
        "max_seconds": 60,
        "max_rss_mib": 1024,
        "max_disk_mib": 512,
        "min_free_mib": 0,
    }
    values.update(changes)
    return argparse.Namespace(**values)


def test_dense_native_scan_stages_reopen_and_snapshot(tmp_path):
    args = _args(tmp_path / "dense.sqlite")
    build = profile._build(args)
    assert build["finished"] is True
    assert build["pages_after"] == 3
    assert build["disk_bytes"]["database"] > 0
    assert profile._read(args)["pages"] == 3
    inspected = profile._inspect(args)
    assert inspected["lifecycle"] == "finished"
    assert inspected["window_rows"] == 2
    snapshot = profile._snapshot(args)
    assert snapshot["bytes"] > 0
    assert NativeScan.inspect(tmp_path / "dense-snapshot.sqlite")["counts"]["pages"] == 3
    integrity = profile._integrity(args)
    assert integrity["integrity_check"] == integrity["foreign_key_check"] == "ok"


def test_process_loss_resumes_retained_pages_without_inventing_a_finished_scan(tmp_path):
    scan = tmp_path / "resume.sqlite"
    script = Path(profile.__file__)
    command = [
        sys.executable,
        str(script),
        "build",
        "--scan",
        str(scan),
        "--pages",
        "5",
        "--links-per-page",
        "1",
        "--body-bytes",
        "64",
        "--min-free-mib",
        "0",
    ]
    interrupted = subprocess.run(
        [*command, "--interrupt-after", "2"], capture_output=True, text=True, timeout=60
    )
    assert interrupted.returncode == 75
    assert NativeScan.inspect(scan)["counts"]["pages"] == 2
    resumed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert resumed.returncode in (0, 3), resumed.stderr
    result = json.loads(resumed.stdout)
    assert result["resumed"] is True
    assert result["pages_before"] == 2
    assert result["pages_after"] == 5
    lifecycle = NativeScan.inspect(scan)["scan"]["lifecycle"]
    assert result["finished"] == (lifecycle == "finished")
    assert result["status"] == ("measured" if result["finished"] else "blocked")


def test_current_crawler_ceiling_is_a_named_block_not_a_million_page_claim(tmp_path):
    args = _args(tmp_path / "refused.sqlite", pages=100_000, body_bytes=0, dom_bytes=0)
    with pytest.raises(ConfigError, match="50,000"):
        profile._config(args)
    assert not args.scan.exists()


def test_budget_stop_reports_retained_partial_counts_without_success(tmp_path):
    scan = tmp_path / "budget.sqlite"
    command = [
        sys.executable,
        str(Path(profile.__file__)),
        "build",
        "--scan",
        str(scan),
        "--pages",
        "3",
        "--max-seconds",
        "0.001",
        "--min-free-mib",
        "0",
    ]
    stopped = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert stopped.returncode == 3
    result = json.loads(stopped.stdout)
    assert result["status"] == "blocked"
    assert result["reason"] == "declared stage wall-time budget exceeded"
    assert 0 <= result["pages_committed"] <= 3
    assert result["peak_rss_mib"] > 0
    assert result["disk_bytes_at_stop"]["database"] > 0


def test_fixture_sizes_are_deterministic_and_separate(tmp_path):
    assert len(profile._html(7, 64).encode()) == 64
    assert profile._html(7, 64) != profile._html(8, 64)
    assert profile._html(7, 0) == ""
    with pytest.raises(ValueError, match="at least"):
        profile._html(7, 10)
    with pytest.raises(MemoryError, match="RSS"):
        profile._budget(
            _args(tmp_path / "none.sqlite", max_rss_mib=1),
            time.perf_counter(),
            tmp_path / "none.sqlite",
        )
