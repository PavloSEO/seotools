#!/usr/bin/env python3
"""Opt-in release profile runner for the existing native scan capacity fixtures.

This wrapper never alters the production URL budget. It runs one existing
analysis profile per requested page size, and the 50,000-page case requires
the explicit ``--large`` opt-in because it is expensive, not unauthorized.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ANALYSIS = ROOT / "scripts" / "profile_scan_analysis.py"
SOURCES = (
    "scripts/profile_scan_collector.py",
    "scripts/profile_scan_graph.py",
    "scripts/profile_scan_analysis.py",
    "scripts/profile_scan_release.py",
)
CASES = (
    {"pages": 10_000, "edges_per_page": 30, "links": 300_000},
    {"pages": 10_000, "edges_per_page": 150, "links": 1_500_000},
    {"pages": 50_000, "edges_per_page": 150, "links": 7_500_000},
)
CHILD_TIMEOUT_SECONDS = 900


def _rss() -> tuple[float, str]:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (raw / (1024 * 1024), "bytes") if platform.system() == "Darwin" else (raw / 1024, "KiB")


def _source_manifest() -> dict[str, Any]:
    from scripts import profile_scan_collector as collector

    return {
        "profile_kinds": {
            "build_pages_graph_audit_report": "direct_seeded_sqlite_microprofile",
            "whole": "offline_true_link_discovery",
        },
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        **collector.source_manifest(
            tuple(
                dict.fromkeys(
                    (
                        *collector.SOURCES,
                        *SOURCES,
                        "seohead/storage/resources.py",
                        "seohead/storage/corpus.py",
                        "seohead/storage/scan_v1.sql",
                        "seohead/storage/analysis_graph.py",
                        "seohead/servers/handlers.py",
                        "seohead/servers/scan_handlers.py",
                    )
                )
            )
        ),
    }


def _production_budget(pages: int) -> dict[str, Any]:
    from seohead.crawl.settings import MAX_URLS_CEILING, checked_url_budget

    try:
        checked_url_budget(pages)
    except ValueError as exc:
        return {"accepted": False, "ceiling": MAX_URLS_CEILING, "reason": str(exc)}
    return {"accepted": True, "ceiling": MAX_URLS_CEILING, "reason": ""}


def _not_measured(case: dict[str, int], reason: str) -> dict[str, Any]:
    return {**case, "status": "not_measured", "blocking_reason": reason}


def _run_analysis(command, *, log_dir: Path, label: str, timeout: int):
    """Stream progress to retained logs and stop the owned process tree on timeout."""
    stdout_path = log_dir / f"{label}.stdout.log"
    stderr_path = log_dir / f"{label}.stderr.log"
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        stdout_path.read_text(encoding="utf-8"),
        stderr_path.read_text(encoding="utf-8"),
    )


def run_release_profile(
    *, execute: bool, include_large: bool = False, log_dir: Path | None = None
) -> dict[str, Any]:
    """Return a release manifest and run only the bounded baseline cases on request."""
    if execute and log_dir is None:
        raise ValueError("--execute requires --log-dir so child stdout and stderr are retained")
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
    disk_before = shutil.disk_usage(log_dir or ROOT)
    started = time.perf_counter()
    manifest = _source_manifest()
    results: list[dict[str, Any]] = []
    analysis_summaries: dict[str, Any] = {}
    seen_pages: set[int] = set()
    for case in CASES:
        if case["pages"] in seen_pages:
            continue
        seen_pages.add(case["pages"])
        expected = [candidate for candidate in CASES if candidate["pages"] == case["pages"]]
        budget = _production_budget(case["pages"])
        if not budget["accepted"]:
            results.extend(
                _not_measured(candidate, str(budget["reason"])) for candidate in expected
            )
            continue
        if case["pages"] == 50_000 and not include_large:
            results.extend(
                _not_measured(
                    candidate, "--large is required to execute the 50,000-page release case"
                )
                for candidate in expected
            )
            continue
        if not execute:
            results.extend(
                _not_measured(candidate, "--execute is required to run release profiles")
                for candidate in expected
            )
            continue
        assert log_dir is not None
        label = f"{case['pages']}-pages"
        command = [sys.executable, str(ANALYSIS), "--pages", str(case["pages"])]
        source_manifest = log_dir / "source-manifest.json"
        source_manifest.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        command.extend(("--source-manifest", str(source_manifest)))
        command.extend(("--retain-dir", str(log_dir)))
        if case["pages"] == 50_000:
            command.extend(("--edges-only", str(case["edges_per_page"])))
        began = time.perf_counter()
        try:
            completed = _run_analysis(
                command,
                log_dir=log_dir,
                label=label,
                timeout=CHILD_TIMEOUT_SECONDS * 6 * len(expected) + 60,
            )
        except subprocess.TimeoutExpired as exc:
            results.extend(
                {
                    **candidate,
                    "status": "failed",
                    "returncode": None,
                    "blocking_reason": f"analysis profile exceeded {exc.timeout} seconds",
                    "analysis_process_wall_seconds": round(time.perf_counter() - began, 3),
                }
                for candidate in expected
            )
            continue
        (log_dir / f"{label}.stdout.log").write_text(completed.stdout, encoding="utf-8")
        (log_dir / f"{label}.stderr.log").write_text(completed.stderr, encoding="utf-8")
        if completed.returncode:
            results.extend(
                {
                    **candidate,
                    "status": "failed",
                    "returncode": completed.returncode,
                    "analysis_process_wall_seconds": round(time.perf_counter() - began, 3),
                }
                for candidate in expected
            )
            continue
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("analysis profile did not emit JSON") from exc
        process_wall_seconds = round(time.perf_counter() - began, 3)
        analysis_summaries[str(case["pages"])] = {
            key: payload[key]
            for key in (
                "fixture",
                "rss_delta_mib",
                "whole_pipeline_rss_delta_mib",
                "observed_memory_budget_violations",
                "memory_budgets_mib",
            )
            if key in payload
        }
        selected = {
            item["links"]: item
            for item in payload.get("results", [])
            if item.get("links")
            in {candidate["links"] for candidate in CASES if candidate["pages"] == case["pages"]}
        }
        for candidate in expected:
            profile = selected.get(candidate["links"])
            if profile is None:
                results.append(
                    {
                        **candidate,
                        "status": "blocked",
                        "blocking_reason": "analysis profile JSON lacks the requested case",
                        "analysis_process_wall_seconds": process_wall_seconds,
                    }
                )
            elif "blocking" in profile:
                results.append(
                    {
                        **candidate,
                        "status": "blocked",
                        "blocking": profile["blocking"],
                        "analysis_process_wall_seconds": process_wall_seconds,
                        "wall_seconds": profile.get("case_wall_seconds"),
                        "profile": profile,
                    }
                )
            else:
                results.append(
                    {
                        **candidate,
                        "status": "measured",
                        "analysis_process_wall_seconds": process_wall_seconds,
                        "wall_seconds": profile.get("case_wall_seconds"),
                        "profile": profile,
                    }
                )
    rss, rss_unit = _rss()
    disk_after = shutil.disk_usage(log_dir or ROOT)
    return {
        "manifest": manifest,
        "cases": results,
        "analysis_summaries": analysis_summaries,
        "runner": {
            "executed": execute,
            "large_case_requested": include_large,
            "wall_seconds": round(time.perf_counter() - started, 3),
            "runner_peak_rss_mib": round(rss, 2),
            "rss_source_unit": rss_unit,
            "disk_free_bytes_before": disk_before.free,
            "disk_free_bytes_after": disk_after.free,
            "log_directory": str(log_dir) if log_dir is not None else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--execute", "--run", dest="execute", action="store_true", help="execute release profiles"
    )
    parser.add_argument("--large", action="store_true", help="also execute the 50,000-page case")
    parser.add_argument("--log-dir", type=Path, help="directory for child stdout/stderr logs")
    args = parser.parse_args()
    print(
        json.dumps(
            run_release_profile(
                execute=args.execute, include_large=args.large, log_dir=args.log_dir
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
