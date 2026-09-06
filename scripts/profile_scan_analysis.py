#!/usr/bin/env python3
# ruff: noqa: E402
"""Subprocess memory profile for the F native SQLite analysis path.

The synthetic writer fixture has complete page fields and a balanced graph.
Every analysis child opens its own artifact and uses injected sitemap handling.
The whole stage runs real CLI dispatch and saved-artifact reporting, replacing
only collection with the prepared observations. Separate subprocesses measure
page projection, SQL graph work, audit/task materialization, all five report
formats, and the saved-scan CLI-to-Markdown path.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import platform
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import profile_scan_graph as fixture
from seohead import __version__
from seohead.crawl.collect import PageRecord
from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
from seohead.reports import build_report
from seohead.servers.handlers import _audit_crawl_result
from seohead.servers.scan_handlers import _rebuild_page_result
from seohead.sf.core.inlinks import _norm_anchor
from seohead.sf.core.link_score import (
    DEFAULT_DAMPING,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_TOLERANCE,
)
from seohead.sf.core.normalize import norm_url
from seohead.sf.tasks import build_tasks
from seohead.storage import read_audit
from seohead.storage.analysis_graph import AnalysisGraph
from seohead.storage.native_scan import NativeScan


def _rss() -> tuple[float, str]:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (raw / (1024 * 1024), "bytes") if platform.system() == "Darwin" else (raw / 1024, "KiB")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _environment() -> dict[str, object]:
    files = (
        "scripts/profile_scan_analysis.py",
        "scripts/profile_scan_graph.py",
        "seohead/storage/analysis_graph.py",
        "seohead/storage/analysis_score.py",
        "seohead/storage/analysis_paths.py",
        "seohead/servers/handlers.py",
        "seohead/servers/scan_handlers.py",
    )
    return {
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files
        },
    }


def _settings(pages: int) -> dict:
    return fixture.load(
        overrides={
            "speed.min_delay_seconds": 0,
            "speed.concurrency": 1,
            "limits.max_urls": pages,
            "limits.max_depth": pages,
            "link_position.classify": True,
            "robots.policy": "ignore",
        }
    )


def _fixture_build(pages: int, edges: int, database: Path) -> dict[str, object]:
    """Balanced graph with complete page fields; output size is a separate axis.

    The E blank-field ring remains an output-stress case: it generates over
    100 MiB of audit JSON at 10k pages and exceeds the unchanged saved-audit cap.
    This fixture exercises a usable tiny-document CLI run, including saving it.
    """
    fixture.PAGES = pages
    started = time.perf_counter()
    metadata = fixture._metadata()
    metadata["writer_version"] = __version__
    sitemap = f"https://{fixture.HOST}/sitemap.xml"
    with NativeScan.create(database, initial_sitemaps=[(sitemap, "explicit")], **metadata) as scan:
        scan.enqueue([(fixture._url(page), 0 if page == 0 else 3) for page in range(pages)])
        for page in range(pages):
            lease = scan.claim(1)[0]
            title = f"Synthetic profile page {page} | Example"
            description = (
                f"Synthetic profile document number {page}. " + "Useful example content. " * 5
            )
            links = [
                {
                    "source": lease.url,
                    "destination": fixture._url((page * (edges + 1) + offset) % pages),
                    "anchor": "x",
                    "nofollow": False,
                    "position": "content" if offset % 2 else "footer",
                    "rel": (),
                    "target": "",
                    "raw_href": "",
                }
                for offset in range(1, edges + 1)
            ]
            record = PageRecord(
                url=lease.url,
                status_code=200,
                content_type="text/html; charset=utf-8",
                crawl_depth=lease.depth,
                title=title,
                h1=title,
                h2="Example details",
                meta_description="" if page % 1000 == 0 else description,
                canonical=lease.url,
                meta_robots="index, follow",
                word_count=500,
                size_bytes=8000,
                text_ratio=60.0,
                charset="utf-8",
                doctype="html",
                viewport="width=device-width, initial-scale=1",
                head_count=1,
                body_count=1,
                content_encoding="gzip",
                og_title=title,
                og_description=description,
                og_image=f"https://{fixture.HOST}/image.png",
                outlinks=edges,
            )
            scan.commit_page(
                lease, vars(record), links=links, runtime=fixture._runtime(lease.depth)
            )
            if (page + 1) % 1000 == 0:
                print(
                    f"progress pages={page + 1} edges_per_page={edges}", file=sys.stderr, flush=True
                )
        sid = scan.sitemap_roots()[0]["sitemap_url_id"]
        for start in range(0, pages, 256):
            scan.write_sitemap_members(
                sid, [(i, fixture._url(i)) for i in range(start, min(start + 256, pages))]
            )
        scan.finish_sitemap(sid, True, "")
        scan.begin_collection()
    rss, unit = _rss()
    return {
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
    }


def _start_gate() -> dict[str, object]:
    return {
        "html": "<html><head><title>profile</title></head><body><a href='/p/1'>x</a></body></html>",
        "outlinks": 1,
        "external_outlinks": 0,
    }


def _page_stage(database: Path) -> dict[str, object]:
    started = time.perf_counter()
    with NativeScan.open(database) as scan:
        result = _rebuild_page_result(scan)
    rss, unit = _rss()
    return {
        "pages": len(result.pages),
        "page_digest": _digest([vars(page) for page in result.pages]),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
    }


def _graph_stage(database: Path) -> dict[str, object]:
    started = time.perf_counter()
    with (
        NativeScan.open(database) as scan,
        AnalysisGraph(scan.con, normalize=norm_url, site_host=fixture.HOST) as graph,
    ):
        scores = graph.link_score(
            damping=DEFAULT_DAMPING,
            max_iterations=DEFAULT_MAX_ITERATIONS,
            tolerance=DEFAULT_TOLERANCE,
        )
        compositions = list(graph.iter_inlink_composition(lambda _url: True, 20))
        anchor_groups = list(
            graph.iter_anchor_groups(lambda anchor: _norm_anchor(anchor) == "x", 20)
        )
        paths = graph.begin_paths(f"https://{fixture.HOST}/p/0")
        last_path = paths.path_to(f"https://{fixture.HOST}/p/{fixture.PAGES - 1}")
        summary = {
            "score_count": scores.count if scores else 0,
            "score_median": round(scores.median, 12) if scores else None,
            "composition_count": len(compositions),
            "composition_digest": _digest([vars(row) for row in compositions]),
            "anchor_group_count": len(anchor_groups),
            "anchor_digest": _digest([vars(row) for row in anchor_groups]),
            "last_path": list(last_path) if last_path else None,
        }
    rss, unit = _rss()
    if (
        summary["score_count"] != fixture.PAGES
        or summary["composition_count"] != fixture.PAGES
        or summary["anchor_group_count"] != fixture.PAGES
    ):
        raise RuntimeError("profile graph stage did not exercise nonempty native graph evidence")
    return {
        **summary,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
    }


def _audit(database: Path, pages: int) -> dict:
    settings = _settings(pages)
    with NativeScan.open(database) as scan:
        result = _rebuild_page_result(scan)
        result.start_page_evidence = _start_gate()
        result.resumed = False
        result.finish_reason = "finished"
        import seohead.sf.core.sitemap_coverage as sitemap_module

        original = sitemap_module.run_sitemap
        sitemap_module.run_sitemap = lambda *_args, **_kwargs: {"sitemaps": []}
        try:
            with prepare_sitemap_reconciliation(
                scan.con, start_url=f"https://{fixture.HOST}/p/0"
            ) as stored_sitemap:
                _response, audit = _audit_crawl_result(
                    result,
                    settings=settings,
                    url=f"https://{fixture.HOST}/p/0",
                    sitemap_seed={"sitemap_url": None, "sitemap_urls": [], "declared": []},
                    discovery={
                        "mode": "profile",
                        "directive_policy": "ignore",
                        "robots_blocked": 0,
                    },
                    stored_scan=scan,
                    stored_sitemap=stored_sitemap,
                )
        finally:
            sitemap_module.run_sitemap = original
    return audit


def _audit_stage(database: Path, pages: int, audit_out: Path | None) -> dict[str, object]:
    started = time.perf_counter()
    audit = _audit(database, pages)
    if audit_out is not None:
        audit_out.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    tasks = build_tasks(audit, None)
    rss, unit = _rss()
    if not audit["pages"] or not audit["summary"]["totals"]["urls_crawled"]:
        raise RuntimeError("profile audit stage did not materialize a nonempty audit")
    return {
        "pages": len(audit["pages"]),
        "issues": len(audit["issues"]),
        "checks_skipped": audit["summary"]["check_coverage"]["checks_skipped"],
        "audit_digest": _digest(audit),
        "tasks_digest": _digest(tasks),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
    }


def _report_stage(audit_path: Path, out: Path) -> dict[str, object]:
    started = time.perf_counter()
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    out.mkdir(mode=0o700)
    reports: dict[str, dict[str, object]] = {}
    for fmt in ("json", "md", "csv", "xlsx", "docx"):
        path = out / f"report.{fmt}"
        result = build_report(audit, fmt, str(path))
        if not result.get("ok") or not path.exists() or not path.read_bytes():
            raise RuntimeError(f"profile report stage did not create a nonempty {fmt} report")
        reports[fmt] = {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    rss, unit = _rss()
    return {
        "formats": reports,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
    }


def _whole_stage(database: Path, pages: int, out: Path) -> dict[str, object]:
    """Run real CLI dispatch, native audit persistence, reopen, and Markdown report.

    Only collection is replaced by the prebuilt synthetic observation fixture.
    """
    from unittest.mock import patch

    from seohead.cli import main as cli_main
    from seohead.crawl.sqlite_adapter import ScanRun

    started = time.perf_counter()
    with NativeScan.open(database) as scan:
        counts = scan.resume_snapshot(include_edges=True)["counts"]
    run = ScanRun(
        path=str(database),
        pages=counts["pages"],
        links=counts["links"],
        forms=counts["forms"],
        lifecycle="running",
        finish_reason="finished",
        partial=False,
        start_page_gate=_start_gate(),
    )
    config = out.with_suffix(".config.json")
    config.write_text(json.dumps(_settings(pages)), encoding="utf-8")
    output = io.StringIO()
    with (
        patch("seohead.crawl.sqlite_adapter.crawl_to_scan", return_value=run),
        patch("seohead.sf.core.sitemap_coverage.run_sitemap", return_value={"sitemaps": []}),
        contextlib.redirect_stdout(output),
    ):
        status = cli_main(
            [
                "crawl-site",
                "--url",
                fixture._url(0),
                "--scan-out",
                str(database),
                "--producer-build",
                "0" * 40,
                "--config",
                str(config),
            ]
        )
    response = json.loads(output.getvalue())
    if status or not response.get("audit_available"):
        raise RuntimeError(f"whole CLI did not save an audit: {response.get('audit_reason')}")
    audit = read_audit(database)
    report = build_report(str(database), "md", str(out))
    rss, unit = _rss()
    if not report.get("ok") or not out.exists() or not audit["pages"]:
        raise RuntimeError("profile whole stage did not produce audit and report")
    return {
        "pages": len(audit["pages"]),
        "issues": len(audit["issues"]),
        "audit_digest": _digest(audit),
        "report_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "saved_audit": True,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
    }


def _child(
    stage: str,
    database: Path,
    pages: int,
    edges: int,
    audit_out: Path | None,
    report_out: Path | None,
    source_manifest: Path | None,
) -> dict[str, object]:
    fixture.PAGES = pages
    if stage == "build":
        result = _fixture_build(pages, edges, database)
    elif stage == "pages":
        result = _page_stage(database)
    elif stage == "graph":
        result = _graph_stage(database)
    elif stage == "audit":
        result = _audit_stage(database, pages, audit_out)
    elif stage == "report":
        if audit_out is None or report_out is None:
            raise ValueError("report stage requires audit and report paths")
        result = _report_stage(audit_out, report_out)
    else:
        if report_out is None:
            raise ValueError("whole stage requires a report path")
        result = _whole_stage(database, pages, report_out)
    environment = (
        json.loads(source_manifest.read_text(encoding="utf-8"))
        if source_manifest is not None
        else _environment()
    )
    return {stage: result, **environment}


def _run_child(
    stage: str,
    database: Path,
    pages: int,
    edges: int,
    audit_out: Path | None = None,
    report_out: Path | None = None,
    source_manifest: Path | None = None,
) -> dict:
    command = [
        sys.executable,
        __file__,
        "--child",
        "--stage",
        stage,
        "--database",
        str(database),
        "--pages",
        str(pages),
        "--edges",
        str(edges),
    ]
    if audit_out is not None:
        command.extend(("--audit-out", str(audit_out)))
    if report_out is not None:
        command.extend(("--report-out", str(report_out)))
    if source_manifest is not None:
        command.extend(("--source-manifest", str(source_manifest)))
    try:
        completed = subprocess.run(command, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.stderr or "analysis profile child failed without stderr") from exc
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    return json.loads(completed.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--stage", choices=("build", "pages", "graph", "audit", "report", "whole"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--pages", type=int, default=10_000)
    parser.add_argument("--edges", type=int, choices=(30, 150))
    parser.add_argument("--audit-out", type=Path)
    parser.add_argument("--report-out", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    args = parser.parse_args()
    if args.child:
        if args.stage is None or args.database is None or args.edges is None or args.pages < 1:
            parser.error("--child requires --stage, --database, --pages, and --edges")
        print(
            json.dumps(
                _child(
                    args.stage,
                    args.database,
                    args.pages,
                    args.edges,
                    args.audit_out,
                    args.report_out,
                    args.source_manifest,
                ),
                sort_keys=True,
            )
        )
        return

    results = []
    with tempfile.TemporaryDirectory(prefix="seohead-analysis-profile-") as temporary:
        directory = Path(temporary)
        manifest = directory / "source-manifest.json"
        manifest.write_text(json.dumps(_environment(), sort_keys=True), encoding="utf-8")
        for edges in (30, 150):
            database = directory / f"{edges}.sqlite"
            audit = directory / f"{edges}.audit.json"
            outcome: dict[str, object] = {"edges_per_page": edges, "links": args.pages * edges}
            for stage in ("build", "pages", "graph"):
                outcome.update(
                    _run_child(stage, database, args.pages, edges, source_manifest=manifest)
                )
            outcome.update(
                _run_child(
                    "audit", database, args.pages, edges, audit_out=audit, source_manifest=manifest
                )
            )
            outcome.update(
                _run_child(
                    "report",
                    database,
                    args.pages,
                    edges,
                    audit_out=audit,
                    report_out=directory / f"{edges}.reports",
                    source_manifest=manifest,
                )
            )
            outcome.update(
                _run_child(
                    "whole",
                    database,
                    args.pages,
                    edges,
                    report_out=directory / f"{edges}.whole.md",
                    source_manifest=manifest,
                )
            )
            results.append(outcome)
    results.sort(key=lambda row: int(row["edges_per_page"]))
    deltas = {
        stage: round(
            float(results[1][stage]["peak_rss_mib"]) - float(results[0][stage]["peak_rss_mib"]), 2
        )
        for stage in ("pages", "graph", "audit", "report", "whole")
    }
    if any(deltas[stage] > 128 for stage in ("graph", "audit", "whole")) or any(
        float(row["whole"]["peak_rss_mib"]) >= 1024 for row in results
    ):
        raise RuntimeError("native analysis profile exceeded the F memory acceptance budget")
    if results[0]["audit"]["audit_digest"] == "" or results[1]["audit"]["audit_digest"] == "":
        raise RuntimeError("native analysis profile has no audit digest")
    print(
        json.dumps(
            {
                "fixture": {
                    "pages": args.pages,
                    "host": fixture.HOST,
                    "network": "injected run_sitemap and no socket transport",
                },
                "results": results,
                "rss_delta_mib": deltas,
                "whole_pipeline_rss_delta_mib": deltas["whole"],
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
