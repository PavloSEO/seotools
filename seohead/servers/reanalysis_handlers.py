"""Offline scan orchestration over the existing parser and audit registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from seohead.storage import ScanError


def reanalyze_scan(input_path: str, out: str, producer_build: str | None = None) -> dict[str, Any]:
    """Publish a new analysis only after retained inputs and output validate."""
    if not isinstance(input_path, str) or not input_path:
        raise ValueError("input_path must name a retained SQLite scan")
    if not isinstance(out, str) or not out:
        raise ValueError("out must name a new derived SQLite scan")

    from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
    from seohead.servers.handlers import _audit_crawl_result
    from seohead.servers.scan_handlers import (
        MAX_AUDIT_FORMS,
        MAX_AUDIT_PAGES,
        _producer_provenance,
        _rebuild_page_result,
    )
    from seohead.storage.native_audit import AuditSizeError
    from seohead.storage.reanalysis import derived_scan, replace_reparsed_page
    from seohead.storage.reanalysis_pages import iterate_reparsed_pages

    version, revision, runtime = _producer_provenance(producer_build)
    with derived_scan(
        input_path,
        out,
        producer_version=version,
        producer_revision=revision,
        runtime_versions=runtime,
    ) as (scan, source):
        parent = dict(source.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
        settings = json.loads(parent["config_json"])
        count = source.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        if count > MAX_AUDIT_PAGES:
            raise ScanError(
                f"reanalysis unavailable: audit page limit exceeded ({MAX_AUDIT_PAGES})"
            )
        old_audit = source.execute("SELECT document_json FROM audit WHERE singleton=1").fetchone()
        captured_run = json.loads(old_audit[0])["run"] if old_audit is not None else {}
        start_gate = None
        selected_start_html = None
        for replay in iterate_reparsed_pages(source, settings):
            replace_reparsed_page(scan, replay)
            if replay.start_page_gate is not None:
                start_gate = dict(replay.start_page_gate)
                selected_start_html = replay.selected_html

        forms = scan.con.execute("SELECT COUNT(*) FROM forms").fetchone()[0]
        reason = ""
        audit = None
        if forms > MAX_AUDIT_FORMS:
            reason = f"reanalysis audit form limit exceeded ({MAX_AUDIT_FORMS})"
        elif start_gate is None:
            reason = "reanalysis unavailable: start-page raw evidence is not_in_corpus"
        else:
            result = _rebuild_page_result(scan)
            provenance = json.loads(
                scan.con.execute(
                    "SELECT payload_json FROM context_items WHERE kind='reanalysis_provenance' AND item_key='run'"
                ).fetchone()[0]
            )
            result.finish_reason = provenance["capture_run"]["finish_reason"]
            result.stopped_reason = result.finish_reason if result.partial else ""
            if result.partial and result.stopped_reason in {"", "running", "finished"}:
                result.stopped_reason = "source coverage or offline parsed observations are partial"
            result.start_page_evidence = start_gate
            if selected_start_html is not None:
                result._rendered_start_html = selected_start_html
            roots = scan.sitemap_roots()
            sitemap_seed = {
                "sitemap_url": roots[0]["url"] if roots else None,
                "sitemap_urls": [root["url"] for root in roots],
                "declared": [],
            }
            discovery = {
                "mode": "spider",
                "directive_policy": settings["robots"]["policy"],
                "robots_blocked": len(result.robots_blocked),
                "sitemap_url": sitemap_seed["sitemap_url"],
                "sitemap_urls": sitemap_seed["sitemap_urls"],
                "sitemap_seeded": len(result.seed_urls),
            }
            with prepare_sitemap_reconciliation(
                scan.con, start_url=parent["start_url"]
            ) as reconciliation:
                _, audit = _audit_crawl_result(
                    result,
                    settings=settings,
                    url=parent["start_url"],
                    sitemap_seed=sitemap_seed,
                    discovery=discovery,
                    out_dir=None,
                    pages_resume_path=None,
                    stored_scan=scan,
                    stored_sitemap=reconciliation,
                    offline=True,
                    captured_render_summary=captured_run.get("render_escalation"),
                )
            audit["run"]["reanalysis"] = {
                "parent_scan_uuid": parent["scan_uuid"],
                "parent_writer_version": parent["writer_version"],
                "parent_writer_revision": parent["writer_revision"],
                "analyzer_revision": revision,
                "network": "disabled",
            }
            try:
                scan.save_audit(audit)
            except AuditSizeError as exc:
                reason = str(exc)
        if reason:
            scan.note_audit_unavailable(reason)
        header = dict(scan.con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
        response = {
            "ok": True,
            "scan": str(Path(out).absolute()),
            "scan_uuid": header["scan_uuid"],
            "parent_scan_uuid": parent["scan_uuid"],
            "source_kind": "reanalysis",
            "analyzer_revision": revision,
            "pages": count,
            "audit_available": not bool(reason),
            "audit_reason": reason,
            "crawl_partial": bool(header["crawl_partial"]),
            "corpus_partial": bool(header["corpus_partial"]),
            "capabilities": json.loads(header["capabilities_json"]),
        }
    return response
