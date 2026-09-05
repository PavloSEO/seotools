"""Existing audit-consuming paths accept a scan without changing the audit."""

import json

import pytest

from seohead.reports import build_report
from seohead.storage import import_run
from tests.test_scan_artifact import BUILD
from tests.test_scan_artifact import legacy_run as legacy_run
from tests.test_scan_artifact_office import frozen_office_clock as frozen_office_clock


@pytest.fixture
def scan(legacy_run, tmp_path):
    return import_run(legacy_run, tmp_path / "scan.sqlite", producer_build=BUILD)


@pytest.mark.parametrize("fmt", ["json", "md", "csv", "xlsx", "docx"])
def test_existing_report_accepts_the_artifact_directly(
    legacy_run, scan, tmp_path, frozen_office_clock, fmt
):
    first, second = (
        tmp_path / "json-input" / f"report.{fmt}",
        tmp_path / "scan-input" / f"report.{fmt}",
    )
    assert build_report(str(legacy_run / "audit.json"), fmt, str(first))["ok"]
    assert build_report(str(scan), fmt, str(second))["ok"]
    assert first.read_bytes() == second.read_bytes()
    if fmt == "csv":
        assert (
            first.with_suffix(".pages.csv").read_bytes()
            == second.with_suffix(".pages.csv").read_bytes()
        )


def test_stale_adjacent_export_is_reported_without_becoming_input(legacy_run, scan, tmp_path):
    adjacent = scan.with_name("audit.json")
    adjacent.write_text('{"this":"is not the saved audit"}')
    output = tmp_path / "report.json"
    result = build_report(str(scan), "json", str(output))
    assert result["ok"]
    assert result["input_diagnostics"][0]["code"] == "adjacent_audit_mismatch"
    assert json.loads(output.read_text()) == json.loads((legacy_run / "audit.json").read_text())
