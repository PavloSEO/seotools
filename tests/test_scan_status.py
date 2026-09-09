"""Offline status summaries distinguish frontier work from committed outcomes."""

from __future__ import annotations

import hashlib

from seohead.storage.native_scan import NativeScan
from seohead.storage.status import scan_status
from tests.test_scan_native import _metadata, _record, _runtime


def test_native_status_counts_disjoint_frontier_and_http_outcomes(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0), ("https://example.test/next", 1)])
        lease = scan.claim(1)[0]
        record = _record(lease.url)
        record["status_code"] = 503
        scan.commit_page(lease, record, runtime=_runtime())
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = scan_status(str(path))
    assert result["frontier"]["counts"] == {"queued": 1, "inflight": 0, "done": 1, "excluded": 0}
    assert result["committed_page_outcomes"]["5xx"] == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_status_rejects_missing_artifact(tmp_path):
    try:
        scan_status(str(tmp_path / "missing.sqlite"))
    except ValueError:
        pass
    else:
        raise AssertionError("expected validated reader refusal")
