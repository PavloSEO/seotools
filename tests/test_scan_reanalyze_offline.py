"""Offline re-analysis of a saved scan.v1 (#382).

``reanalyze_scan`` must make zero network attempts, never mutate the source
file, and name the seven checks scan.v1 cannot re-measure rather than either
silently dropping them from coverage or fabricating an answer.
"""

from __future__ import annotations

import hashlib
import socket

import pytest

from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.servers.scan_handlers import UNMEASURABLE_OFFLINE_CHECKS, reanalyze_scan
from tests.test_scan_analysis_parity import _fixture, _settings
from tests.test_scan_crawl_parity import _fetcher, _runtime_versions


def _build_scan(tmp_path):
    settings = _settings("complete")
    path = tmp_path / "scan.sqlite"
    crawl_to_scan(
        "https://example.test/",
        scan_out=str(path),
        settings=settings,
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions=_runtime_versions(),
        fetcher=_fetcher(_fixture()),
        sleeper=lambda _seconds: None,
    )
    return str(path)


def _sha256(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def test_reanalyze_makes_zero_network_attempts(tmp_path, monkeypatch):
    scan_path = _build_scan(tmp_path)

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("reanalyze_scan attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)

    result = reanalyze_scan(scan_path, producer_build="b" * 40)

    assert result["audit_available"] is True
    assert result["reanalysis"] is True


def test_reanalyze_never_mutates_source_file(tmp_path):
    scan_path = _build_scan(tmp_path)
    before = _sha256(scan_path)

    reanalyze_scan(scan_path, producer_build="b" * 40)

    assert _sha256(scan_path) == before
    assert reanalyze_scan(scan_path, producer_build="b" * 40)["source_sha256"] == before


def test_reanalyze_names_the_seven_unmeasurable_checks(tmp_path):
    scan_path = _build_scan(tmp_path)

    result = reanalyze_scan(scan_path, producer_build="b" * 40)

    skipped_ids = {s["id"] for s in result["audit"]["run"]["checks_skipped"]}
    assert set(UNMEASURABLE_OFFLINE_CHECKS) <= skipped_ids
    reasons = {s["id"]: s["reason"] for s in result["audit"]["run"]["checks_skipped"]}
    for check_id in UNMEASURABLE_OFFLINE_CHECKS:
        assert reasons[check_id], f"{check_id} has no stated reason"
    # Subtracted from the coverage denominator, not silently missing while a
    # score is still reported (the #382 dishonesty this is meant to prevent).
    coverage = result["coverage"]
    assert coverage["checks_skipped"] >= len(UNMEASURABLE_OFFLINE_CHECKS)
    assert (
        coverage["checks_total"]
        == coverage["checks_fired"]
        + coverage["checks_skipped"]
        + (coverage["checks_disabled"])
        + coverage["checks_silent"]
    )


def test_reanalyze_marks_output_as_reanalysis_with_provenance(tmp_path):
    scan_path = _build_scan(tmp_path)

    result = reanalyze_scan(scan_path, producer_build="c" * 40)

    assert result["audit"]["run"]["input_mode"] == "reanalysis"
    assert result["source_scan"] == scan_path
    assert result["parent_scan_uuid"]
    assert result["analyzer_revision"] == "c" * 40
    assert result["reanalysis_uuid"]
    # This fixture scan was captured with crawl_to_scan directly and never had
    # save_audit() called on it, so there is honestly no original audit to
    # compare against -- the field says so rather than fabricating a baseline.
    assert result["original_audit_available"] is False
    assert result["original_coverage"] is None


def test_reanalyze_compares_against_a_saved_original_audit(tmp_path):
    from seohead.storage.native_scan import NativeScan

    scan_path = _build_scan(tmp_path)
    first_pass = reanalyze_scan(scan_path, producer_build="d" * 40)
    with NativeScan.open(scan_path) as scan:
        scan.save_audit(first_pass["audit"])

    result = reanalyze_scan(scan_path, producer_build="d" * 40)

    assert result["original_audit_available"] is True
    assert result["original_coverage"] == first_pass["coverage"]


def test_reanalyze_missing_source_is_a_named_error_not_a_crash(tmp_path):
    with pytest.raises(ValueError):
        reanalyze_scan(str(tmp_path / "missing.sqlite"))
