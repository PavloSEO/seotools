"""Finite handler bridge from a native SQLite scan to the existing audit path."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from seohead import __version__

MAX_AUDIT_PAGES = 5_000
MAX_AUDIT_LINKS = 100_000
MAX_AUDIT_FORMS = 20_000
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _installed_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _producer_provenance(producer_build: str | None) -> tuple[str, str, dict[str, str]]:
    """Return actual source provenance; an explicit fixture build never runs Git."""
    if producer_build is not None:
        if not isinstance(producer_build, str) or not _SHA.fullmatch(producer_build):
            raise ValueError("producer_build must be a full lowercase Git commit SHA")
        revision = producer_build
    else:
        root = Path(__file__).resolve().parents[2]
        try:
            top = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(
                "source revision is unavailable; pass producer_build explicitly"
            ) from exc
        if top.returncode or Path(top.stdout.strip()).resolve() != root:
            raise ValueError(
                "installed package has no verified source checkout; pass producer_build explicitly"
            )
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        revision_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        revision = revision_result.stdout.strip()
        if (
            status.returncode
            or revision_result.returncode
            or status.stdout
            or not _SHA.fullmatch(revision)
        ):
            raise ValueError(
                "native scan provenance requires a clean source checkout; pass producer_build explicitly"
            )
    return (
        __version__,
        revision,
        {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "httpx": _installed_version("httpx"),
            "lxml": _installed_version("lxml"),
            "beautifulsoup4": _installed_version("beautifulsoup4"),
        },
    )


def _rebuild_spider_result(scan) -> Any:
    """Materialize only an already-admitted compatibility population."""
    from seohead.crawl.collect import PageRecord
    from seohead.crawl.spider import FormEdge, LinkEdge, SpiderResult
    from seohead.storage.exports import _link_rows, _page_rows

    result = SpiderResult()
    result.pages = [PageRecord(**page) for page in _page_rows(scan.con)]
    result.links = [
        LinkEdge(**(link | {"rel": tuple(link["rel"])})) for link in _link_rows(scan.con)
    ]
    result.forms = [
        FormEdge(
            page=row["page"],
            method=row["method"],
            action=row["action"],
            has_password=bool(row["has_password"]),
        )
        for row in scan.con.execute(
            "SELECT f.*, u.url AS page FROM forms AS f JOIN urls AS u ON u.url_id=f.page_url_id "
            "ORDER BY f.form_id"
        )
    ]
    context = [
        (row["kind"], json.loads(row["payload_json"]))
        for row in scan.con.execute(
            "SELECT kind,payload_json FROM context_items WHERE kind IN ('robots_blocked_url','seed_url','robots_summary') ORDER BY kind,item_key"
        )
    ]
    result.robots_blocked = [
        scan.con.execute("SELECT url FROM urls WHERE url_id=?", (item["url_id"],)).fetchone()[0]
        for kind, item in context
        if kind == "robots_blocked_url"
    ]
    result.seed_urls = [
        scan.con.execute("SELECT url FROM urls WHERE url_id=?", (item["url_id"],)).fetchone()[0]
        for kind, item in context
        if kind == "seed_url"
    ]
    robots_summary = next((item for kind, item in context if kind == "robots_summary"), None)
    result.robots_note = robots_summary["note"] if robots_summary is not None else ""
    snapshot = scan.resume_snapshot()
    result.partial = bool(snapshot["scan"]["crawl_partial"])
    result.finish_reason = snapshot["scan"]["finish_reason"]
    result.stopped_reason = snapshot["scan"]["finish_reason"] if result.partial else ""
    result.max_depth_reached = snapshot["runtime"]["max_depth_reached"]
    result.crawl_delay_applied = snapshot["runtime"]["crawl_delay_applied"]
    result.effective_delay = snapshot["runtime"]["throttle"]["delay_seconds"]
    result.effective_concurrency = snapshot["runtime"]["throttle"]["concurrency"]
    result.limitations = json.loads(snapshot["scan"]["limitations_json"])
    return result


def _response(run, *, audit_available: bool, audit_reason: str, finalized: bool) -> dict[str, Any]:
    return {
        "scan": run.path,
        "urls_collected": run.pages,
        "links_collected": run.links,
        "forms_collected": run.forms,
        "partial": run.partial,
        "finish_reason": run.finish_reason,
        "audit_available": audit_available,
        "audit_reason": audit_reason,
        "finalized": finalized,
        "limitations": list(run.limitations),
    }


def _bridge_reason(counts: dict[str, int], start_page_gate: dict[str, Any] | None) -> str | None:
    if (
        counts["pages"] > MAX_AUDIT_PAGES
        or counts["links"] > MAX_AUDIT_LINKS
        or counts["forms"] > MAX_AUDIT_FORMS
    ):
        return (
            "compatibility audit limit exceeded "
            f"(pages={counts['pages']}/{MAX_AUDIT_PAGES}, "
            f"links={counts['links']}/{MAX_AUDIT_LINKS}, "
            f"forms={counts['forms']}/{MAX_AUDIT_FORMS}); SQL graph/analyzer bridge is E/F"
        )
    if start_page_gate is None:
        return "start-page transient raw evidence is unavailable after resume; audit waits for G"
    if set(start_page_gate) != {"html", "outlinks", "external_outlinks"} or not all(
        isinstance(start_page_gate[name], str if name == "html" else int)
        for name in start_page_gate
    ):
        return "start-page transient gate is invalid; audit is unavailable"
    return None


def _has_saved_audit(scan) -> bool:
    return scan.con.execute("SELECT 1 FROM audit WHERE singleton=1").fetchone() is not None


def crawl_site_scan(
    url: str,
    *,
    scan_out: str,
    settings: dict[str, Any],
    sitemap: str | None = None,
    producer_build: str | None = None,
) -> dict[str, Any]:
    """Collect a native scan, then audit it only within the finite compatibility bounds."""
    if not isinstance(url, str) or not url:
        raise ValueError("url is required for a SQLite scan crawl")
    if not isinstance(scan_out, str) or not scan_out:
        raise ValueError("scan_out is required for a SQLite scan crawl")
    producer_version, producer_revision, runtime_versions = _producer_provenance(producer_build)
    sitemap_seed = {
        "sitemap_url": sitemap,
        "sitemap_urls": [sitemap] if sitemap else [],
        "declared": [],
    }

    from seohead.servers.scan_sitemaps import initial_sitemaps, load_sitemaps

    def seed_loader(scan, emit_seeds):
        load_sitemaps(scan, emit_seeds, settings=settings, result=sitemap_seed)

    from seohead.crawl.sqlite_adapter import crawl_to_scan
    from seohead.storage.native_scan import NativeScan

    run = crawl_to_scan(
        url,
        scan_out=scan_out,
        settings=settings,
        producer_version=producer_version,
        producer_revision=producer_revision,
        runtime_versions=runtime_versions,
        initial_sitemaps=initial_sitemaps(sitemap),
        seed_loader=seed_loader,
    )
    with NativeScan.open(run.path) as scan:
        snapshot = scan.resume_snapshot(include_edges=True)
        roots = scan.sitemap_roots()
        sitemap_seed.update(
            sitemap_url=roots[0]["url"] if roots else None,
            sitemap_urls=[root["url"] for root in roots],
            declared=[],
        )
        counts = {table: snapshot["counts"][table] for table in ("pages", "links", "forms")}
        reason = _bridge_reason(counts, run.start_page_gate)
        if reason is not None and run.start_page_gate is None and _has_saved_audit(scan):
            finalized = scan.finish_capture(reason="finished")
            return _response(
                run,
                audit_available=True,
                audit_reason="reused current saved audit after interrupted finalization",
                finalized=finalized,
            )
        if reason is not None:
            scan.note_audit_unavailable(reason)
            finalized = scan.finish_capture(reason=run.finish_reason)
            return _response(run, audit_available=False, audit_reason=reason, finalized=finalized)
        result = _rebuild_spider_result(scan)
        result.start_page_evidence = dict(run.start_page_gate)
        result.resumed = getattr(run, "resumed", False)
        result.finish_reason = run.finish_reason
        if result.partial and result.stopped_reason in {"running", "finished", ""}:
            result.stopped_reason = (
                "; ".join(note for note in run.limitations if "observations_omitted" in note)
                or "collection evidence is partial"
            )
        discovery = {
            "mode": "spider",
            "directive_policy": settings["robots"]["policy"],
            "robots_blocked": len(result.robots_blocked),
            "sitemap_url": sitemap_seed["sitemap_url"],
            "sitemap_urls": sitemap_seed["sitemap_urls"],
            "sitemap_seeded": len(result.seed_urls),
        }
        from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
        from seohead.servers.handlers import _audit_crawl_result

        with prepare_sitemap_reconciliation(scan.con, start_url=url) as reconciliation:
            _response_data, audit = _audit_crawl_result(
                result,
                settings=settings,
                url=url,
                sitemap_seed=sitemap_seed,
                discovery=discovery,
                out_dir=None,
                pages_resume_path=None,
                finite_json=True,
                stored_scan=scan,
                stored_sitemap=reconciliation,
            )
        scan.save_audit(audit)
        finalized = scan.finish_capture(reason=run.finish_reason)
    _response_data.update(
        _response(run, audit_available=True, audit_reason="", finalized=finalized)
    )
    return _response_data
