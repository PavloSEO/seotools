"""Read-only project-checklist snapshots for human report renderers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from seohead.recon.net import normalize_domain


def _audit_domain(document: dict[str, Any], kind: str) -> str:
    if kind == "sf-audit":
        from seohead.reports.facts import crawl_domain

        return crawl_domain(document.get("run") or {})
    url = document.get("url")
    if isinstance(url, str) and urlsplit(url).hostname:
        return normalize_domain(urlsplit(url).hostname or "")
    return normalize_domain(str(document.get("domain") or ""))


def load_snapshot(project: str, document: dict[str, Any], kind: str) -> tuple[Path, dict[str, Any]]:
    """Bind a human report to one project and return its current checklist state."""
    from seohead.projects.coverage import coverage_status
    from seohead.projects.workspace import open_project

    opened = open_project(project)
    project_document = opened["project"]
    actual = _audit_domain(document, kind)
    expected = project_document["site"]["host"]
    if not actual:
        raise ValueError("audit has no discoverable site identity for the requested project")
    if actual != expected:
        raise ValueError(
            f"project site {expected!r} does not match audit source identity {actual!r}"
        )
    snapshot = coverage_status(opened["path"])
    return Path(opened["path"]), {
        "project": {
            "uuid": project_document["project_uuid"],
            "site": project_document["site"]["target"],
        },
        "status": snapshot,
    }


def attach_snapshot(document: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Copy a normalized report document with its read-only project snapshot."""
    projected = dict(document)
    summary = dict(document.get("summary") or {})
    summary["project_coverage"] = snapshot
    projected["summary"] = summary
    return projected


def protected_project_paths(root: Path) -> set[Path]:
    """Return immutable project controls and every evidence artifact still referenced."""
    protected = {(root / "project.json").resolve(), (root / "coverage.json").resolve()}
    coverage = root / "coverage.json"
    if not coverage.is_file():
        return protected
    try:
        document = json.loads(coverage.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return protected
    items = document.get("items", {}) if isinstance(document, dict) else {}
    if not isinstance(items, dict):
        return protected
    for item in items.values():
        if not isinstance(item, dict) or not isinstance(item.get("records"), list):
            continue
        for record in item["records"]:
            artifact = record.get("artifact") if isinstance(record, dict) else None
            if not isinstance(artifact, str):
                continue
            relative = Path(artifact)
            if relative.is_absolute() or ".." in relative.parts:
                continue
            protected.add((root / relative).resolve())
    return protected


def protected_destination(root: Path, targets: list[Path]) -> str | None:
    """Explain whether a report would replace project control or saved evidence."""
    protected = protected_project_paths(root)
    for target in targets:
        if target.resolve() in protected:
            return "report output must not overwrite project control files or referenced evidence"
    return None


def scope_text(scope: Any) -> str:
    """Render the stored site/template/sample scope without deriving new scope."""
    if not isinstance(scope, dict):
        return ""
    bits = [str(scope.get("site") or "")]
    if scope.get("template"):
        bits.append(f"template={scope['template']}")
    urls = scope.get("urls")
    if isinstance(urls, list) and urls:
        bits.append("urls=" + ", ".join(str(url) for url in urls))
    return "; ".join(bit for bit in bits if bit)


def value_text(value: Any) -> str:
    """Keep structured snapshot values exact in one portable report cell."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def priority_text(item: dict[str, Any]) -> str:
    """Show the saved work priority and its recorded origin, without recalculating policy."""
    if not item.get("priority"):
        return ""
    return (
        f"{item['priority']} ({item.get('priority_origin', '')}): {item.get('priority_reason', '')}"
    )
