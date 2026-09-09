"""Native SQLite analysis with bounded page and report materialization."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import re
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from seohead import __version__
from seohead.build_provenance import BuildProvenanceError, packaged_provenance

MAX_AUDIT_PAGES = 10_000
MAX_AUDIT_FORMS = 20_000
_SHA = re.compile(r"[0-9a-f]{40}\Z")

# scan.v1 (evidence_version crawl.v1) retains no robots.txt or sitemap document
# (that is child H/#381 scope), so these seven checks cannot be re-measured
# from stored evidence -- only from a new crawl. Pre-registered here, by name,
# so a reanalysis never silently drops them out of the coverage denominator
# while still reporting a score (#382).
UNMEASURABLE_OFFLINE_CHECKS = (
    "SITEMAP_NOT_IN_ROBOTS",
    "ROBOTS_BLOCKS_RESOURCES",
    "SITEMAP_FETCH_INCOMPLETE",
    "SITEMAP_TOO_MANY_URLS",
    "SITEMAP_TOO_LARGE",
    "SITEMAP_URL_DUPLICATED",
    "SITEMAP_STALE_LASTMOD",
)
_UNMEASURABLE_REASON = (
    "scan.v1 retains no robots.txt/sitemap document; re-measuring this check "
    "requires a new crawl, not a reanalysis of stored evidence"
)


def _installed_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _producer_provenance(producer_build: str | None) -> tuple[str, str, dict[str, str]]:
    """Return explicit, clean-checkout, or validated packaged provenance."""
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
        except (OSError, subprocess.TimeoutExpired):
            top = None
        if top is not None and not top.returncode and Path(top.stdout.strip()).resolve() == root:
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
        else:
            try:
                packaged = packaged_provenance()
            except BuildProvenanceError as exc:
                raise ValueError(
                    "no verified source checkout or packaged producer metadata; "
                    "pass producer_build explicitly"
                ) from exc
            if packaged.version != __version__:
                raise ValueError("packaged producer metadata version disagrees with this package")
            revision = packaged.revision
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


def _rebuild_page_result(scan) -> Any:
    """Materialize admitted pages and run context; the graph stays in SQLite."""
    from seohead.crawl.collect import PageRecord
    from seohead.crawl.spider import SpiderResult
    from seohead.storage.exports import _page_rows

    result = SpiderResult()
    result.pages = [PageRecord(**page) for page in _page_rows(scan.con)]
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
    response = {
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
        # Named in the result, not only as a "partial" capability flag: a corpus
        # reduced to a fifth of the pages the same document counts over is not a
        # caveat, and nothing else in the response says how much is gone (#647).
        "html_bodies": dict(run.html_bodies),
        # Same reasoning for the rendering lane: a rendered corpus reduced to
        # nothing by a cookie the site set is not a caveat either (#656).
        "rendered_bodies": dict(run.rendered_bodies),
    }
    if run.capabilities is not None:
        response.update(corpus_partial=run.corpus_partial, capabilities=run.capabilities)
    return response


def _bridge_reason(counts: dict[str, int], start_page_gate: dict[str, Any] | None) -> str | None:
    if counts["pages"] > MAX_AUDIT_PAGES or counts["forms"] > MAX_AUDIT_FORMS:
        return (
            "materialized audit population limit exceeded "
            f"(pages={counts['pages']}/{MAX_AUDIT_PAGES}, "
            f"forms={counts['forms']}/{MAX_AUDIT_FORMS})"
        )
    if start_page_gate is None:
        return (
            "start-page raw evidence was not retained; audit cannot reconstruct its rendering gate"
        )
    if set(start_page_gate) != {"html", "outlinks", "external_outlinks"} or not all(
        isinstance(start_page_gate[name], str if name == "html" else int)
        for name in start_page_gate
    ):
        return "start-page transient gate is invalid; audit is unavailable"
    return None


def _has_saved_audit(scan) -> bool:
    return scan.con.execute("SELECT 1 FROM audit WHERE singleton=1").fetchone() is not None


# NativeScan.note_audit_unavailable rejects a reason over this many chars, and
# neither the exception's own message nor a caller-chosen --scan-out path was ever
# bounded with that column in mind, so the assembled reason is truncated to fit
# after both are interpolated in, not before.
_AUDIT_FAILURE_REASON_MAX = 500


def _audit_failure_reason(exc: BaseException, run) -> str:
    """Name the audit phase and the surviving artifact instead of a bare exception.

    A ``KeyError``'s ``str()`` is just the key (see #627) -- everything an
    operator needs (that collection finished, how much of it, and where to
    re-analyse it) has to come from this message, because nothing else in
    the failure carries it.
    """
    reason = (
        f"audit phase failed unexpectedly ({type(exc).__name__}: {exc}); "
        f"collection finished with {run.pages} pages retained in {run.path} -- "
        "re-analyse it with `seohead scan-reanalyze`"
    )
    if len(reason) > _AUDIT_FAILURE_REASON_MAX:
        reason = reason[: _AUDIT_FAILURE_REASON_MAX - 1] + "…"
    return reason


def resume_inputs(scan_path: str) -> dict[str, Any]:
    """Read back what a resume must repeat: the scan's own start URL and settings.

    A resume is one crawl continuing, not a second crawl aimed at the same file.
    The frontier, scope and limits already in the artifact decide what is left to
    fetch, so they are read from it rather than restated on a command line, where
    a single changed value would either be refused by the writer's configuration
    fingerprint or -- worse -- continue one crawl under another crawl's rules.

    Read-only and before any request: every refusal below is decided from the
    file alone, so a mismatched artifact costs the target site nothing.
    """
    from seohead.storage.native_scan import NativeScan

    if not isinstance(scan_path, str) or not scan_path:
        raise ValueError("resume requires the path of a SQLite scan artifact")
    if not Path(scan_path).is_file():
        raise ValueError(f"scan to resume does not exist: {scan_path}")
    header = NativeScan.inspect(scan_path)["scan"]
    if header["source_kind"] != "native":
        raise ValueError(
            f"{scan_path} is a derived {header['source_kind']} artifact, not a crawl that can "
            "be resumed"
        )
    if header["lifecycle"] in {"finished", "failed"}:
        raise ValueError(
            f"{scan_path} is already {header['lifecycle']} "
            f"(finish reason: {header['finish_reason']}); there is nothing left to resume"
        )
    settings = json.loads(header["config_json"])
    if settings.get("http", {}).get("credential_headers"):
        # The artifact stores credential references redacted, by design, so the
        # settings read back from it are not the settings the interrupted run
        # used. Continuing under them would crawl the same site unauthenticated
        # and report the result as a continuation of an authenticated crawl.
        raise ValueError(
            f"{scan_path} was crawled with credential headers, which it stores only in redacted "
            "form; a resume cannot restore them. Continue it with the original --config and "
            "--scan-out instead of --resume"
        )
    return {
        "start_url": header["start_url"],
        "settings": settings,
        "writer_revision": header["writer_revision"],
    }


def resume_scan(
    scan_path: str,
    *,
    url: str | None = None,
    producer_build: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Continue an interrupted native scan from its stored frontier and throttle state.

    Refuses, by name and before the first request, a scan written by a different
    producing build or for a different start URL: continuing evidence collected
    under one build's parser or from one start URL into another produces a single
    artifact that describes neither run.
    """
    inputs = resume_inputs(scan_path)
    _version, revision, _runtime = _producer_provenance(producer_build)
    if inputs["writer_revision"] != revision:
        raise ValueError(
            f"{scan_path} was written by build {inputs['writer_revision']}, and this is build "
            f"{revision}; refusing a mixed-build resume. Check out the build that wrote it, or "
            "pass producer_build with that SHA if this source tree is that build"
        )
    if url is not None:
        from seohead.recon.net import normalize_url

        if normalize_url(url) != inputs["start_url"]:
            raise ValueError(
                f"{scan_path} was crawled from {inputs['start_url']}, not {url}; refusing to "
                "resume one crawl as another"
            )
    return crawl_site_scan(
        inputs["start_url"],
        scan_out=scan_path,
        settings=inputs["settings"],
        producer_build=revision,
        progress=progress,
    )


def crawl_site_scan(
    url: str,
    *,
    scan_out: str,
    settings: dict[str, Any],
    sitemap: str | None = None,
    producer_build: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    """Collect a native scan, then audit its SQL graph with finite page/output bounds.

    ``progress`` is forwarded to the collector untouched; see
    ``seohead.crawl.sqlite_adapter.crawl_to_scan``. It covers collection only --
    the audit that follows reads an artifact that has stopped growing, so a
    progress line over it would report a crawl that is already over.
    """
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

    def seed_loader(scan, emit_seeds, *, request_gate):
        load_sitemaps(
            scan,
            emit_seeds,
            settings=settings,
            result=sitemap_seed,
            request_gate=request_gate,
        )

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
        progress=progress,
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
        result = _rebuild_page_result(scan)
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
        from dataclasses import replace

        from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
        from seohead.servers.handlers import _audit_crawl_result
        from seohead.storage.native_audit import AuditSizeError

        # Collection already committed its rows and closed successfully by this point
        # (``run`` above), so anything raised from here on is the *audit* misbehaving,
        # never the crawl -- and the 600-page artifact behind #627 proves collection's
        # own evidence survives an audit crash untouched. An operator reading a bare
        # exception (a KeyError's str() is just the key) has no way to tell that apart
        # from a lost crawl, so an unexpected failure here is named by phase and points
        # at the retained, re-analysable artifact instead of propagating as-is.
        try:
            with prepare_sitemap_reconciliation(scan.con, start_url=url) as reconciliation:
                _response_data, audit = _audit_crawl_result(
                    result,
                    settings=settings,
                    url=url,
                    sitemap_seed=sitemap_seed,
                    discovery=discovery,
                    out_dir=None,
                    pages_resume_path=None,
                    stored_scan=scan,
                    stored_sitemap=reconciliation,
                    dispatch_gate=run.dispatch_gate,
                )

            if settings.get("rendering", {}).get("mode", "raw") != "raw":
                from seohead.storage.corpus import rendered_body_retention

                current = scan.resume_snapshot(include_edges=True)
                run = replace(
                    run,
                    partial=bool(current["scan"]["crawl_partial"]),
                    links=current["counts"]["links"],
                    forms=current["counts"]["forms"],
                    limitations=tuple(json.loads(current["scan"]["limitations_json"])),
                    corpus_partial=bool(current["scan"]["corpus_partial"]),
                    capabilities=json.loads(current["scan"]["capabilities_json"]),
                    # Rendered documents are written by the escalation that just
                    # ran, not by the collector, so this is the first point where
                    # the corpus can be asked how many DOMs it kept (#656).
                    rendered_bodies=rendered_body_retention(scan.con),
                )

            scan.save_audit(audit)
        except AuditSizeError as exc:
            reason = str(exc)
            scan.note_audit_unavailable(reason)
            finalized = scan.finish_capture(reason=run.finish_reason)
            return _response(run, audit_available=False, audit_reason=reason, finalized=finalized)
        except Exception as exc:
            reason = _audit_failure_reason(exc, run)
            scan.note_audit_unavailable(reason)
            finalized = scan.finish_capture(reason=run.finish_reason)
            return _response(run, audit_available=False, audit_reason=reason, finalized=finalized)
        finalized = scan.finish_capture(reason=run.finish_reason)
    _response_data.update(
        _response(run, audit_available=True, audit_reason="", finalized=finalized)
    )
    return _response_data


def _sha256_file(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reanalyze_scan(
    scan_in: str,
    *,
    producer_build: str | None = None,
) -> dict[str, Any]:
    """Re-run today's parser/check registry over a scan.v1 already on disk.

    Offline by construction: the shared audit assembly receives the retained
    start URL so it can replay graph and form checks, but ``offline=True``
    prevents render escalation and names the sitemap/robots checks that have
    no retained source document. No HTTP, DNS, or browser request is
    dispatched on this path.

    ``scan_in`` is opened only through a private working copy -- the
    original file is hashed up front and never reopened for writing, so its
    bytes (and that hash) are provably unchanged by this call.
    """
    if not isinstance(scan_in, str) or not scan_in:
        raise ValueError("scan_in is required")
    source_path = Path(scan_in)
    if not source_path.is_file():
        raise ValueError(f"scan_in does not exist: {scan_in}")
    producer_version, producer_revision, runtime_versions = _producer_provenance(producer_build)
    source_sha256 = _sha256_file(str(source_path))

    import shutil
    import tempfile
    import uuid as uuid_mod

    from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
    from seohead.servers.handlers import _audit_crawl_result
    from seohead.storage import ScanError, open_scan
    from seohead.storage.native_scan import NativeScan

    # The original audit (if any), read through the validated read-only path --
    # never the writer -- purely to report how this reanalysis' coverage
    # compares to what the original run measured.
    original_audit: dict[str, Any] | None = None
    original_scan_uuid: str | None = None
    try:
        con = open_scan(scan_in, require_audit=False)
        try:
            scan_row = con.execute("SELECT scan_uuid FROM scan WHERE singleton=1").fetchone()
            original_scan_uuid = scan_row["scan_uuid"] if scan_row else None
            audit_row = con.execute("SELECT document_json FROM audit WHERE singleton=1").fetchone()
            if audit_row is not None:
                original_audit = json.loads(audit_row["document_json"])
        finally:
            con.close()
    except ScanError as exc:
        return {
            "reanalysis": True,
            "source_scan": scan_in,
            "source_sha256": source_sha256,
            "audit_available": False,
            "audit_reason": f"cannot read source scan: {exc}",
        }

    with tempfile.TemporaryDirectory(prefix="seohead-reanalyze-") as tmpdir:
        work_copy = str(Path(tmpdir) / "scan.sqlite")
        shutil.copyfile(scan_in, work_copy)
        with NativeScan.open(work_copy) as scan:
            snapshot = scan.resume_snapshot()
            config = json.loads(snapshot["scan"]["config_json"])
            start_url = snapshot["scan"]["start_url"]
            result = _rebuild_page_result(scan)
            result.start_page_evidence = {}
            result.resumed = False
            with prepare_sitemap_reconciliation(
                scan.con, start_url=start_url or ""
            ) as reconciliation:
                _response_data, audit = _audit_crawl_result(
                    result,
                    settings=config,
                    url=start_url,
                    sitemap_seed={"sitemap_url": None, "sitemap_urls": [], "declared": []},
                    discovery={
                        "mode": "spider",
                        "directive_policy": config["robots"]["policy"],
                        "robots_blocked": len(result.robots_blocked),
                    },
                    stored_scan=scan,
                    stored_sitemap=reconciliation,
                    offline=True,
                    captured_render_summary=(original_audit or {})
                    .get("run", {})
                    .get("render_escalation"),
                )

    audit["run"]["input_mode"] = "reanalysis"
    audit["run"]["source"] = start_url or audit["run"].get("source")

    coverage = audit.get("summary", {}).get("check_coverage", {})
    original_coverage = (
        (original_audit or {}).get("summary", {}).get("check_coverage", {})
        if original_audit
        else None
    )

    return {
        "reanalysis": True,
        "reanalysis_uuid": str(uuid_mod.uuid4()),
        "source_scan": scan_in,
        "source_sha256": source_sha256,
        "parent_scan_uuid": original_scan_uuid,
        "analyzer_version": producer_version,
        "analyzer_revision": producer_revision,
        "runtime_versions": runtime_versions,
        "generated_at": audit["run"]["generated_at"],
        "unmeasurable_checks": list(UNMEASURABLE_OFFLINE_CHECKS),
        "unmeasurable_reason": _UNMEASURABLE_REASON,
        "coverage": coverage,
        "original_coverage": original_coverage,
        "original_audit_available": original_audit is not None,
        "audit_available": True,
        "audit": audit,
    }
