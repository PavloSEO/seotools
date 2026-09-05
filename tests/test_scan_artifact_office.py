"""Byte-identical Office reports from a legacy directory and its scan artifact."""

from __future__ import annotations

import datetime as dt
import zipfile

import pytest

from seohead.reports import build_report
from tests import test_scan_artifact

_FROZEN_ZIP_TIME = (2024, 1, 1, 0, 0, 0)
BUILD = test_scan_artifact.BUILD
legacy_run = test_scan_artifact.legacy_run


@pytest.fixture
def frozen_office_clock(monkeypatch):
    """Fix metadata emitted by OpenPyXL, python-docx, and ZIP writers."""
    import openpyxl.packaging.core as core

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 1, 1, tzinfo=tz)

    monkeypatch.setattr(core.datetime, "datetime", FrozenDateTime)

    original_init = zipfile.ZipInfo.__init__

    def frozen_init(self, filename="NoName", date_time=_FROZEN_ZIP_TIME):
        original_init(self, filename, _FROZEN_ZIP_TIME)

    monkeypatch.setattr(zipfile.ZipInfo, "__init__", frozen_init)

    original_from_file = zipfile.ZipInfo.from_file

    def frozen_from_file(cls, filename, arcname=None, *, strict_timestamps=True):
        info = original_from_file(filename, arcname, strict_timestamps=strict_timestamps)
        info.date_time = _FROZEN_ZIP_TIME
        return info

    monkeypatch.setattr(zipfile.ZipInfo, "from_file", classmethod(frozen_from_file))

    if hasattr(zipfile.ZipInfo, "_for_archive"):
        original_for_archive = zipfile.ZipInfo._for_archive

        def frozen_for_archive(self, archive):
            info = original_for_archive(self, archive)
            info.date_time = _FROZEN_ZIP_TIME
            return info

        monkeypatch.setattr(zipfile.ZipInfo, "_for_archive", frozen_for_archive)


@pytest.mark.parametrize("fmt", ["xlsx", "docx"])
def test_same_run_office_reports_are_byte_identical(legacy_run, tmp_path, frozen_office_clock, fmt):
    expected = tmp_path / "directory" / f"report.{fmt}"
    assert build_report(str(legacy_run / "audit.json"), fmt, str(expected))["ok"]

    from seohead.storage import import_run, read_audit

    artifact = tmp_path / "scan.sqlite"
    import_run(legacy_run, artifact, producer_build=BUILD)
    actual = tmp_path / "sqlite" / f"report.{fmt}"
    assert build_report(read_audit(artifact), fmt, str(actual))["ok"]

    assert actual.read_bytes() == expected.read_bytes()
