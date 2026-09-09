"""Validate saved completion evidence without running checks or changing artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .coverage import _text


def artifact_path(root: Path, value: Any) -> Path:
    """Accept portable regular files inside the project, excluding its control files."""
    _text(value, "artifact", 1024)
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("artifact must be a portable project-relative path")
    if relative.parts[0] not in {"scans", "reports"}:
        raise ValueError("evidence belongs under scans/ or reports/")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("evidence must not traverse symlinks")
    if not current.is_file() or not current.resolve().is_relative_to(root):
        raise ValueError("evidence artifact is missing or unsafe")
    return current


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _saved_check(root: Path, definition: dict, value: Any) -> dict:
    """Bind automatic completion to the check outcome and identity inside scan.v1."""
    from seohead.storage import open_scan

    from .catalogue import load_catalogue

    path = artifact_path(root, value)
    before = _digest(path)
    con = open_scan(path)
    try:
        scan = dict(con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
        audit = json.loads(
            con.execute("SELECT document_json FROM audit WHERE singleton=1").fetchone()[0]
        )
        operation = definition["operation"]
        check_id = operation.removeprefix("check:")
        run = audit.get("run", {})
        coverage = audit.get("summary", {}).get("check_coverage", {})
        fired = {item.get("check") for item in audit.get("issues", [])}
        silent = set(coverage.get("checks_silent_ids", []))
        unavailable = {
            item.get("id")
            for key in ("checks_skipped", "checks_disabled")
            for item in run.get(key, [])
        }
        unavailable.update(coverage.get("checks_disabled_ids", []))
        if check_id in unavailable or check_id not in fired | silent:
            raise ValueError("saved artifact does not prove this check ran successfully")
        if run.get("crawl_valid") is False:
            raise ValueError("saved crawl is invalid")
        start = scan["start_url"] or run.get("start_url") or run.get("source")
        if (
            not isinstance(start, str)
            or urlsplit(start).netloc != urlsplit(definition["scope"]["site"]).netloc
        ):
            raise ValueError("evidence site does not match item scope")
        pages = {row[0] for row in con.execute("SELECT url FROM pages JOIN urls USING(url_id)")}
        requested = set(definition["scope"]["urls"])
        if definition["scope"]["template"] and not requested:
            raise ValueError("template measurements require explicit sample URLs")
        if requested and requested != pages:
            raise ValueError("evidence population must match the requested scope URLs")
        if not pages:
            raise ValueError("empty scan cannot complete a measurement")
        limited = bool(
            scan["crawl_partial"]
            or scan["corpus_partial"]
            or definition["scope"]["template"]
            or requested
        )
        result = {
            "artifact": value,
            "sha256": before,
            "site": definition["scope"]["site"],
            "scope": definition["scope"],
            "operation": operation,
            "operation_hash": load_catalogue()[operation]["definition_hash"],
            "source": {
                key: scan[key]
                for key in (
                    "scan_uuid",
                    "source_kind",
                    "writer_version",
                    "writer_revision",
                    "config_fingerprint",
                    "config_json",
                    "finish_reason",
                    "lifecycle",
                    "evidence_revision",
                )
                if key in scan
            },
            "observed_at": run.get("generated_at") or scan.get("created_at"),
            "measurement": {
                "state": "limited" if limited else "measured",
                "reason": "source is partial or scope is an explicit sample"
                if limited
                else "measured population in the saved scan; not a census of the whole site",
                "population": len(pages),
                "requested_urls": len(requested),
                "crawl_partial": bool(scan["crawl_partial"]),
                "corpus_partial": bool(scan["corpus_partial"]),
            },
        }
    finally:
        con.close()
    if _digest(path) != before:
        raise ValueError("evidence changed while it was being recorded")
    return result


def validate_record(root: Path, definition: dict, record: Any) -> dict:
    if not isinstance(record, dict) or set(record) - {
        "status",
        "reason",
        "artifact",
        "reviewer",
        "review",
        "signoff",
    }:
        raise ValueError("record has unsupported fields")
    status = record.get("status")
    if not isinstance(status, str) or status not in {
        "running",
        "failed",
        "unavailable",
        "succeeded",
        "not_applicable",
    }:
        raise ValueError("invalid execution status")
    reason = _text(record.get("reason"), "record reason")
    result = {"status": status, "reason": reason, "measurement": None}
    if status in {"running", "failed", "unavailable"}:
        if set(record) - {"status", "reason"}:
            raise ValueError("unfinished attempts record status and reason only")
        return result
    if status == "not_applicable":
        # Applicability is an explicit specialist decision, never inferred from missing data.
        if set(record) - {"status", "reason", "reviewer"}:
            raise ValueError("applicability review requires reason and reviewer only")
        result["reviewer"] = _text(record.get("reviewer"), "reviewer", 128)
        return result
    kind = definition["execution_kind"]
    if kind == "automatic":
        if set(record) - {"status", "reason", "artifact"}:
            raise ValueError("automatic evidence cannot be replaced by manual signoff")
        result.update(_saved_check(root, definition, record.get("artifact")))
    else:
        result["reviewer"] = _text(record.get("reviewer"), "reviewer", 128)
        if "signoff" in record and type(record["signoff"]) is not bool:
            raise ValueError("signoff must be a boolean")
        if kind == "manual" and record.get("signoff") is True:
            if "artifact" in record or "review" in record:
                raise ValueError("use either explicit signoff or reviewed evidence")
            result["signoff"] = True
        else:
            if record.get("review") != "approved":
                raise ValueError("completion requires approved artifact review")
            path = artifact_path(root, record.get("artifact"))
            if path.stat().st_size == 0:
                raise ValueError("empty artifact cannot complete a review")
            result.update(artifact=record["artifact"], sha256=_digest(path), review="approved")
        result["scope"] = definition["scope"]
        result["measurement"] = {
            "state": "not_measured",
            "reason": "specialist review or signoff; no automatic measurement is implied",
        }
    return result


def evidence_stale(root: Path, record: dict, catalogue: dict, digests: dict) -> str:
    if "artifact" in record:
        try:
            artifact = record["artifact"]
            if artifact not in digests:
                digests[artifact] = _digest(artifact_path(root, artifact))
            if digests[artifact] != record["sha256"]:
                return "evidence artifact changed"
        except (ValueError, OSError):
            return "evidence artifact is missing or unsafe"
    if "operation" in record:
        current = catalogue.get(record["operation"])
        if current is None or current["definition_hash"] != record["operation_hash"]:
            return "evidence operation definition changed"
    return ""
