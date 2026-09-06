"""The release profile runner plans bounded cases without running heavy fixtures in tests."""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from scripts import profile_scan_collector as collector
from scripts import profile_scan_release as release
from seohead.storage import open_scan


def test_default_manifest_does_not_run_profiles_and_names_the_heavy_case(tmp_path):
    result = release.run_release_profile(execute=False, log_dir=tmp_path)

    assert [case["links"] for case in result["cases"]] == [300_000, 1_500_000, 7_500_000]
    assert all(case["status"] == "not_measured" for case in result["cases"])
    assert "--large" in result["cases"][-1]["blocking_reason"]
    assert result["manifest"]["source_sha256"]
    assert re.fullmatch(r"[0-9a-f]{40}", result["manifest"]["source_revision"])
    assert isinstance(result["manifest"]["source_dirty"], bool)
    assert result["runner"]["rss_source_unit"] in {"bytes", "KiB"}


def test_source_manifest_rejects_an_unvalidated_git_revision(monkeypatch):
    monkeypatch.setattr(
        collector.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Completed", (), {"returncode": 0, "stdout": "not-a-sha\n", "stderr": ""}
        )(),
    )

    with pytest.raises(RuntimeError, match="full lowercase Git HEAD SHA"):
        collector.source_manifest()


def test_run_executes_only_10k_cases_sequentially_and_preserves_child_logs(monkeypatch, tmp_path):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["git", "rev-parse"]:
            return type("Completed", (), {"returncode": 0, "stdout": "a" * 40, "stderr": ""})()
        if command[:2] == ["git", "status"]:
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": " M scripts/profile.py\n", "stderr": ""},
            )()
        if "--pages" not in command:
            return type(
                "Completed", (), {"returncode": 0, "stdout": "test-revision\n", "stderr": ""}
            )()
        payload = {
            "results": [
                {"links": 300_000, "pages": {"pages": 10_000}},
                {"links": 1_500_000, "pages": {"pages": 10_000}},
            ]
        }
        return type(
            "Completed",
            (),
            {"returncode": 0, "stdout": json.dumps(payload), "stderr": "progress\n"},
        )()

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    monkeypatch.setattr(release, "_run_analysis", fake_run)
    result = release.run_release_profile(execute=True, log_dir=tmp_path)

    assert len(calls) == 3  # Git provenance plus one 10k profile covering both densities
    assert all("--pages" in command for command in calls if command[:1] != ["git"])
    assert [case["status"] for case in result["cases"]] == [
        "measured",
        "measured",
        "not_measured",
    ]
    assert list(tmp_path.glob("*.stdout.log")) and list(tmp_path.glob("*.stderr.log"))
    assert (tmp_path / "source-manifest.json").is_file()


def test_large_opt_in_includes_the_50k_case_without_lowering_its_requested_config(
    monkeypatch, tmp_path
):
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[:2] == ["git", "rev-parse"]:
            return type("Completed", (), {"returncode": 0, "stdout": "a" * 40, "stderr": ""})()
        if command[:2] == ["git", "status"]:
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        pages = int(command[command.index("--pages") + 1]) if "--pages" in command else 0
        payload = (
            {
                "results": [
                    {"links": 300_000, "pages": {"pages": 10_000}},
                    {"links": 1_500_000, "pages": {"pages": 10_000}},
                ]
            }
            if pages == 10_000
            else {"results": [{"links": 7_500_000, "pages": {"pages": pages}}]}
        )
        return type(
            "Completed", (), {"returncode": 0, "stdout": json.dumps(payload), "stderr": ""}
        )()

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    monkeypatch.setattr(release, "_run_analysis", fake_run)
    result = release.run_release_profile(execute=True, include_large=True, log_dir=tmp_path)

    assert [case["status"] for case in result["cases"]] == ["measured", "measured", "measured"]
    profile_calls = [command for command in calls if "--pages" in command]
    assert profile_calls[-1][profile_calls[-1].index("--pages") + 1] == "50000"
    assert profile_calls[-1][profile_calls[-1].index("--source-manifest") + 1] == str(
        tmp_path / "source-manifest.json"
    )
    assert result["cases"][-1]["pages"] == 50_000


def test_tiny_real_whole_profile_collects_before_audit_and_report(tmp_path):
    database = tmp_path / "profile.sqlite"
    report = tmp_path / "report.md"
    completed = subprocess.run(
        [
            sys.executable,
            str(release.ANALYSIS),
            "--child",
            "--stage",
            "whole",
            "--database",
            str(database),
            "--pages",
            "3",
            "--edges",
            "30",
            "--report-out",
            str(report),
        ],
        cwd=release.ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(completed.stdout)
    whole = payload["whole"]

    assert whole["status"] == "measured"
    assert whole["collection"]["fetched_pages"] == 3
    assert whole["collection"]["counts"] == {"pages": 3, "links": 90, "forms": 0, "bodies": 3}
    assert whole["saved_audit"] is True
    assert database.with_name("profile.whole.sqlite").is_file()
    assert report.is_file() and report.read_bytes()
    assert re.fullmatch(r"[0-9a-f]{40}", payload["source_revision"])
    assert isinstance(payload["source_dirty"], bool)
    with open_scan(database.with_name("profile.whole.sqlite")) as con:
        assert (
            con.execute("SELECT writer_revision FROM scan").fetchone()[0]
            == payload["source_revision"]
        )


def test_analysis_timeout_terminates_its_child_process_group_and_keeps_progress(tmp_path):
    code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "print(p.pid,flush=True); print('started',file=sys.stderr,flush=True); time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        release._run_analysis(
            [sys.executable, "-c", code], log_dir=tmp_path, label="timeout", timeout=1
        )
    child = int((tmp_path / "timeout.stdout.log").read_text().strip())
    assert "started" in (tmp_path / "timeout.stderr.log").read_text()
    result = subprocess.run(["ps", "-p", str(child), "-o", "stat="], capture_output=True, text=True)
    assert not result.stdout.strip() or result.stdout.strip().startswith("Z")


def test_tiny_profile_runs_every_stage_for_both_densities():
    completed = subprocess.run(
        [sys.executable, str(release.ANALYSIS), "--pages", "3"],
        cwd=release.ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=120,
    )
    result = json.loads(completed.stdout)
    assert [row["links"] for row in result["results"]] == [90, 450]
    for row in result["results"]:
        assert "blocking" not in row
        assert {"build", "pages", "graph", "audit", "report", "whole"} <= row.keys()
        assert row["whole"]["collection"]["fetched_pages"] == 3
        assert row["whole"]["saved_audit"] is True
    assert "collector" in result["rss_delta_mib"]
