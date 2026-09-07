#!/usr/bin/env python3
# ruff: noqa: E402
"""Subprocess memory profile for direct-seeded analysis and whole discovery.

The build/pages/graph/audit/report stages are direct-seeded SQLite microprofiles.
The separate whole stage runs CLI dispatch, real offline link discovery, saved
audit persistence, reopen, and Markdown reporting in one child process.
Their audit digests are never compared because their fixture depths differ.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import profile_scan_collector as collector
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
from seohead.storage import open_scan, read_audit
from seohead.storage.analysis_graph import AnalysisGraph
from seohead.storage.native_scan import NativeScan

# Published beside every measured number in docs/SQLITE_ACCEPTANCE.md, so a budget
# or ceiling that moves here must move there too; the release test compares them.
MEMORY_BUDGETS_MIB = {"edge_growth": 128, "whole_peak": 2048}
STAGE_TIMEOUT_SECONDS = 900


class ProfileTimeout(RuntimeError):
    """A timed-out child with an optional consistent partial artifact."""

    def __init__(self, message: str, partial_artifact: dict[str, object] | None = None):
        super().__init__(message)
        self.partial_artifact = partial_artifact


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
        "scripts/profile_scan_collector.py",
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
        **collector.source_manifest(files),
    }


def _load_environment(source_manifest: Path | None) -> dict[str, object]:
    environment = (
        json.loads(source_manifest.read_text(encoding="utf-8"))
        if source_manifest is not None
        else _environment()
    )
    if not isinstance(environment, dict):
        raise RuntimeError("profile source manifest must be a JSON object")
    collector.validated_source_revision(environment.get("source_revision"))
    if not isinstance(environment.get("source_dirty"), bool):
        raise RuntimeError("profile source manifest must record whether the checkout is dirty")
    if not isinstance(environment.get("source_sha256"), dict):
        raise RuntimeError("profile source manifest must include source hashes")
    return environment


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


def _fixture_build(pages: int, edges: int, database: Path, provenance: dict) -> dict[str, object]:
    """Balanced graph with complete page fields; output size is a separate axis.

    The E blank-field ring remains an output-stress case: it generates over
    100 MiB of audit JSON at 10k pages and exceeds the unchanged saved-audit cap.
    This fixture exercises a usable tiny-document CLI run, including saving it.
    """
    fixture.PAGES = pages
    started = time.perf_counter()
    metadata = fixture._metadata(provenance)
    metadata["writer_version"] = __version__
    sitemap = f"https://{fixture.HOST}/sitemap.xml"
    with NativeScan.create(database, initial_sitemaps=[(sitemap, "explicit")], **metadata) as scan:
        for start in range(0, pages, 20_000):
            scan.enqueue(
                [
                    (fixture._url(page), 0 if page == 0 else 3)
                    for page in range(start, min(start + 20_000, pages))
                ]
            )
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


def _artifact_bytes(path: Path) -> dict[str, int]:
    """Report the bounded on-disk footprint left by one whole-path child."""

    def size(candidate: Path) -> int:
        return candidate.stat().st_size if candidate.is_file() else 0

    return {
        "file": size(path),
        "wal": size(path.with_name(path.name + "-wal")),
        "shm": size(path.with_name(path.name + "-shm")),
        "temp": sum(
            size(candidate) for candidate in path.parent.glob(".native-scan-*") if candidate != path
        ),
    }


def _whole_stage(
    database: Path, pages: int, edges: int, out: Path, *, producer_build: str
) -> dict[str, object]:
    """Measure CLI collection, analysis, persistence, reopen, and Markdown reporting.

    This is deliberately a separate artifact from the build/pages/graph microprofiles:
    it must create its own scan so peak RSS includes the real collector.  The sole
    test seam is the collector fixture's deterministic transport, which prevents
    every socket, DNS, HTTP, and browser request.
    """
    from seohead.cli import main as cli_main
    from seohead.crawl import sqlite_adapter

    started = time.perf_counter()
    whole_database = database.with_name(f"{database.stem}.whole.sqlite")
    if whole_database.exists():
        raise RuntimeError(f"whole profile scan already exists: {whole_database}")
    collector.PAGES = pages
    fetcher, fetched_pages = collector.offline_fetcher(edges)
    original_crawl_to_scan = sqlite_adapter.crawl_to_scan
    collection_metrics: dict[str, object] = {}

    def offline_crawl_to_scan(*args, **kwargs):
        if "fetcher" in kwargs:
            raise AssertionError("CLI whole profile must own the sole offline transport seam")
        began = time.perf_counter()
        try:
            return original_crawl_to_scan(*args, **kwargs, fetcher=fetcher)
        finally:
            peak, source_unit = _rss()
            collection_metrics.update(
                peak_rss_mib=round(peak, 2),
                rss_source_unit=source_unit,
                wall_seconds=round(time.perf_counter() - began, 3),
            )

    config = out.with_suffix(".config.json")
    config.write_text(json.dumps(_settings(pages)), encoding="utf-8")
    import contextlib
    from io import StringIO

    output = StringIO()
    with (
        patch.object(sqlite_adapter, "crawl_to_scan", offline_crawl_to_scan),
        contextlib.redirect_stdout(output),
    ):
        status = cli_main(
            [
                "crawl-site",
                "--url",
                fixture._url(0),
                "--scan-out",
                str(whole_database),
                "--producer-build",
                collector.validated_source_revision(producer_build),
                "--config",
                str(config),
            ]
        )
    try:
        response = json.loads(output.getvalue())
    except json.JSONDecodeError as exc:
        raise RuntimeError("whole CLI did not emit a structured response") from exc
    if status:
        raise RuntimeError(f"whole CLI failed: {response.get('audit_reason', 'unknown error')}")
    con = open_scan(whole_database, require_audit=False)
    try:
        counts = {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("pages", "links", "forms", "bodies", "resource_refs")
        }
    finally:
        con.close()
    rss, unit = _rss()
    outcome: dict[str, object] = {
        "pages": counts["pages"],
        "links": counts["links"],
        "body_count": counts["bodies"],
        "resource_ref_count": counts["resource_refs"],
        "collection": {
            **collection_metrics,
            "profile_kind": "offline_true_discovery_whole_path",
            "fetched_pages": fetched_pages(),
            "counts": counts,
            "finish_reason": response.get("finish_reason"),
            "partial": response.get("partial"),
        },
        "artifact_bytes": _artifact_bytes(whole_database),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
    }
    if not response.get("audit_available"):
        return {
            **outcome,
            "status": "blocked",
            "reason": str(response.get("audit_reason") or "audit unavailable"),
            "saved_audit": False,
        }
    audit = read_audit(whole_database)
    report = build_report(str(whole_database), "md", str(out))
    rss, unit = _rss()
    if not report.get("ok") or not out.exists() or not audit["pages"]:
        raise RuntimeError("profile whole stage did not produce audit and report")
    return {
        **outcome,
        "status": "measured",
        "pages": len(audit["pages"]),
        "issues": len(audit["issues"]),
        "audit_digest": _digest(audit),
        "report_sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "saved_audit": True,
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
    environment = _load_environment(source_manifest)
    if stage == "build":
        result = _fixture_build(pages, edges, database, environment)
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
        result = _whole_stage(
            database,
            pages,
            edges,
            report_out,
            producer_build=collector.validated_source_revision(environment["source_revision"]),
        )
    return {stage: result, **environment}


def _retain_partial_artifact(
    database: Path, stage: str, pages: int, edges: int, retain_dir: Path | None
) -> dict[str, object] | None:
    """Retain a profile's SQLite artifact through Backup API, never its live WAL."""
    if retain_dir is None:
        return None
    source = database.with_name(f"{database.stem}.whole.sqlite") if stage == "whole" else database
    if not source.is_file():
        return None
    retain_dir.mkdir(parents=True, exist_ok=True)
    destination = retain_dir / f"retained-{pages}-pages-{pages * edges}-links-{stage}.sqlite"
    if destination.exists():
        raise RuntimeError(f"retained artifact destination already exists: {destination}")
    source_con = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    destination_con = sqlite3.connect(destination)
    try:
        source_con.backup(destination_con)
    finally:
        destination_con.close()
        source_con.close()
    con = sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        counts = {}
        for table in ("pages", "links", "forms", "bodies", "frontier", "audit"):
            try:
                counts[table] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                continue
        return {
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "counts": counts,
            "integrity_check": con.execute("PRAGMA integrity_check").fetchone()[0],
        }
    finally:
        con.close()


def _run_child(
    stage: str,
    database: Path,
    pages: int,
    edges: int,
    audit_out: Path | None = None,
    report_out: Path | None = None,
    source_manifest: Path | None = None,
    retain_dir: Path | None = None,
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

    def retain_logs(stdout, stderr):
        if retain_dir is not None:
            retain_dir.mkdir(parents=True, exist_ok=True)
            for stream, output in (("stdout", stdout), ("stderr", stderr)):
                data = output if isinstance(output, bytes) else (output or "").encode("utf-8")
                (
                    retain_dir / f"{pages}-pages-{pages * edges}-links-{stage}.{stream}.log"
                ).write_bytes(data)

    try:
        completed = subprocess.run(
            command, check=True, text=True, capture_output=True, timeout=STAGE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as exc:
        retain_logs(exc.stdout, exc.stderr)
        raise ProfileTimeout(
            f"analysis profile child exceeded {STAGE_TIMEOUT_SECONDS} seconds: {stage}",
            _retain_partial_artifact(database, stage, pages, edges, retain_dir),
        ) from exc
    except subprocess.CalledProcessError as exc:
        retain_logs(exc.stdout, exc.stderr)
        raise RuntimeError(exc.stderr or "analysis profile child failed without stderr") from exc
    retain_logs(completed.stdout, completed.stderr)
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    result = json.loads(completed.stdout)
    if stage == "whole" and retain_dir is not None:
        result["whole"]["retained_artifact"] = _retain_partial_artifact(
            database, stage, pages, edges, retain_dir
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--stage", choices=("build", "pages", "graph", "audit", "report", "whole"))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--pages", type=int, default=10_000)
    parser.add_argument("--edges", type=int, choices=(30, 150))
    parser.add_argument(
        "--edges-only",
        type=int,
        choices=(30, 150),
        help="run one edge-density case in profile mode",
    )
    parser.add_argument("--audit-out", type=Path)
    parser.add_argument("--report-out", type=Path)
    parser.add_argument("--source-manifest", type=Path)
    parser.add_argument("--retain-dir", type=Path)
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
        environment = _load_environment(args.source_manifest)
        manifest.write_text(json.dumps(environment, sort_keys=True), encoding="utf-8")
        for edges in (args.edges_only,) if args.edges_only is not None else (30, 150):
            case_started = time.perf_counter()
            database = directory / f"{edges}.sqlite"
            audit = directory / f"{edges}.audit.json"
            outcome: dict[str, object] = {"edges_per_page": edges, "links": args.pages * edges}
            for stage, kwargs in (
                ("build", {}),
                ("pages", {}),
                ("graph", {}),
                ("audit", {"audit_out": audit}),
                ("report", {"audit_out": audit, "report_out": directory / f"{edges}.reports"}),
                ("whole", {"report_out": directory / f"{edges}.whole.md"}),
            ):
                # The real whole path owns another artifact. It must still be
                # measured if a direct-seeded microprofile cannot finish.
                if "blocking" in outcome and stage != "whole":
                    continue
                print(
                    f"profile stage={stage} pages={args.pages} edges_per_page={edges}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    child = _run_child(
                        stage,
                        database,
                        args.pages,
                        edges,
                        source_manifest=manifest,
                        retain_dir=args.retain_dir,
                        **kwargs,
                    )
                    outcome.update(child)
                    if stage == "whole" and child["whole"].get("status") == "blocked":
                        blocker = {
                            "stage": "whole",
                            "reason": child["whole"]["reason"],
                        }
                        outcome.setdefault("blocking", blocker)
                        outcome.setdefault("blockers", []).append(blocker)
                except RuntimeError as exc:
                    blocker = {"stage": stage, "reason": str(exc)}
                    if isinstance(exc, ProfileTimeout) and exc.partial_artifact is not None:
                        blocker["partial_artifact"] = exc.partial_artifact
                    outcome.setdefault("blocking", blocker)
                    outcome.setdefault("blockers", []).append(blocker)
            outcome["case_wall_seconds"] = round(time.perf_counter() - case_started, 3)
            results.append(outcome)
    results.sort(key=lambda row: int(row["edges_per_page"]))
    deltas = {}
    for stage in ("pages", "graph", "audit", "report", "whole", "collector"):
        samples = [
            row.get("whole", {}).get("collection", {})
            if stage == "collector"
            else row.get(stage, {})
            for row in results
        ]
        if len(samples) == 2 and all("peak_rss_mib" in sample for sample in samples):
            deltas[stage] = round(
                float(samples[1]["peak_rss_mib"]) - float(samples[0]["peak_rss_mib"]), 2
            )
    violations = [
        {
            "stage": stage,
            "metric": "edge_growth_mib",
            "limit": MEMORY_BUDGETS_MIB["edge_growth"],
            "observed": deltas[stage],
        }
        for stage in ("collector", "graph", "audit", "whole")
        if stage in deltas and deltas[stage] > MEMORY_BUDGETS_MIB["edge_growth"]
    ]
    for row in results:
        peak = row.get("whole", {}).get("peak_rss_mib")
        if peak is not None and float(peak) > MEMORY_BUDGETS_MIB["whole_peak"]:
            violations.append(
                {
                    "stage": "whole",
                    "metric": "peak_rss_mib",
                    "limit": MEMORY_BUDGETS_MIB["whole_peak"],
                    "observed": peak,
                }
            )
    print(
        json.dumps(
            {
                "fixture": {
                    "pages": args.pages,
                    "host": fixture.HOST,
                    "microprofile_kind": "direct_seeded_sqlite",
                    "whole_path_kind": "offline_true_link_discovery",
                    "audit_digest_comparison": "not compared across fixture depths",
                    "network": "deterministic injected fetcher; no socket transport",
                },
                "results": results,
                "rss_delta_mib": deltas,
                "observed_memory_budget_violations": violations,
                "memory_budgets_mib": MEMORY_BUDGETS_MIB,
                "whole_pipeline_rss_delta_mib": deltas.get("whole"),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
