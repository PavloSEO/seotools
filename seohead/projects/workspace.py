"""Create and read bounded, portable project workspaces without executing project content."""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seohead.recon.net import normalize_domain, normalize_url
from seohead.storage.history import list_scans

PROJECT_FORMAT = "seohead.project.v1"
PROJECT_VERSION = 1
_MAX_JSON_BYTES = 1_048_576
_REFERENCE = re.compile(r"[a-z][a-z0-9._/-]{0,127}\Z")
UTC = timezone.utc


def _directory(path: str | Path, label: str) -> Path:
    if not isinstance(path, (str, Path)) or not str(path):
        raise ValueError(f"{label} is required")
    value = Path(path)
    if value.is_symlink() or not value.is_dir():
        raise ValueError(f"{label} must be an existing non-symlink directory")
    return value.resolve()


def _target(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("target must be a bounded absolute HTTP(S) URL")
    from urllib.parse import urlsplit, urlunsplit

    raw = urlsplit(value)
    if raw.username or raw.password or raw.query or raw.fragment:
        raise ValueError(
            "target site identity must not include credentials, a query, or a fragment"
        )
    normalized = normalize_url(value)
    if not normalized:
        raise ValueError("target must be a public HTTP(S) site identity without credentials")
    parts = urlsplit(normalized)
    if parts.query or parts.fragment or not parts.hostname:
        raise ValueError("target site identity must not include a query or fragment")
    host = normalize_domain(parts.hostname)
    if not host:
        raise ValueError("target must have a valid public hostname")
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError as exc:
        raise ValueError("target must have a valid HTTP(S) port") from exc
    return urlunsplit((parts.scheme.lower(), host + port, parts.path or "/", "", ""))


def _site(target: str, label: str | None) -> dict[str, str | None]:
    from urllib.parse import urlsplit

    host = normalize_domain(urlsplit(target).hostname or "")
    if not host:
        raise ValueError("target must have a valid public hostname")
    if label is not None and (type(label) is not str or not label.strip() or len(label) > 128):
        raise ValueError("label is a bounded human project label, not a host override")
    return {"host": host, "target": target, "label": label}


def _references(value: list[str] | None, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError(f"{label} must be a list of at most 100 references")
    if any(
        type(item) is not str or not _REFERENCE.fullmatch(item) or ".." in item for item in value
    ):
        raise ValueError(f"{label} entries must be bounded relative identifiers")
    return list(dict.fromkeys(value))


def _facts(value: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("facts must be a list of at most 100 provenance-backed values")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "value",
            "provenance",
            "observed_at",
        }:
            raise ValueError("each fact requires name, value, provenance, and observed_at")
        name, fact, provenance, observed_at = (
            item["name"],
            item["value"],
            item["provenance"],
            item["observed_at"],
        )
        if (
            type(name) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
            or type(provenance) is not str
            or not provenance
            or len(provenance) > 512
            or type(fact) not in {str, int, float, bool}
            or (type(fact) is float and not math.isfinite(fact))
            or (isinstance(fact, str) and len(fact) > 2048)
        ):
            raise ValueError("fact fields must be bounded scalar values with provenance")
        if observed_at is not None:
            if type(observed_at) is not str:
                raise ValueError("fact observed_at must be RFC3339 UTC or null when unknown")
            try:
                parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    "fact observed_at must be RFC3339 UTC or null when unknown"
                ) from exc
            if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
                raise ValueError("fact observed_at must be RFC3339 UTC or null when unknown")
        if any(
            token in name.lower()
            for token in (
                "secret",
                "token",
                "password",
                "cookie",
                "authorization",
                "api_key",
                "apikey",
                "private_key",
                "credential",
            )
        ):
            raise ValueError("facts must not store credentials or secrets")
        result.append(
            {"name": name, "value": fact, "provenance": provenance, "observed_at": observed_at}
        )
    return result


def _document(
    target: str,
    label: str | None,
    facts: list[dict[str, Any]],
    templates: list[str],
    profiles: list[str],
) -> dict[str, Any]:
    return {
        "format": PROJECT_FORMAT,
        "version": PROJECT_VERSION,
        "project_uuid": str(uuid.uuid4()),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "site": _site(target, label),
        "facts": facts,
        "template_references": templates,
        "profile_references": profiles,
        "artifact_directories": {"scans": "scans", "reports": "reports", "log": "log.md"},
    }


def _write_new(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _publish_manifest(project: Path, document: dict[str, Any]) -> None:
    """Publish the success marker last, atomically and without replacing a prior manifest."""
    staged = project / ".project.json.stage"
    _write_new(staged, json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    try:
        os.link(staged, project / "project.json", follow_symlinks=False)
        descriptor = os.open(project, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        staged.unlink(missing_ok=True)


def create_project(
    directory: str | Path,
    target: str,
    *,
    label: str | None = None,
    facts: list[dict[str, Any]] | None = None,
    template_references: list[str] | None = None,
    profile_references: list[str] | None = None,
) -> dict[str, Any]:
    """Create one new project directory; existing paths and files are never replaced."""
    root = Path(directory)
    if ".." in root.parts:
        raise ValueError("project directory must not contain parent traversal")
    parent = _directory(root.parent, "project parent")
    if not root.name or root.name in {".", ".."} or os.path.lexists(root):
        raise ValueError("project directory already exists or is not a new path")
    target = _target(target)
    document = _document(
        target,
        label,
        _facts(facts),
        _references(template_references, "template_references"),
        _references(profile_references, "profile_references"),
    )
    project = parent / root.name
    project.mkdir(mode=0o700)
    for name in ("scans", "reports"):
        (project / name).mkdir(mode=0o700)
    _write_new(project / "log.md", "# Project log\n\nProject created; no audit work has run.\n")
    _publish_manifest(project, document)
    return open_project(project)


def _load(directory: str | Path) -> tuple[Path, dict[str, Any]]:
    root = _directory(directory, "project directory")
    for name in ("scans", "reports"):
        child = root / name
        if child.is_symlink() or not child.is_dir():
            raise ValueError(f"project {name} directory is missing or unsafe")
    log = root / "log.md"
    if log.is_symlink() or not log.is_file():
        raise ValueError("project log is missing or unsafe")
    manifest = root / "project.json"
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > _MAX_JSON_BYTES:
        raise ValueError("project.json is missing, unsafe, or exceeds its byte limit")
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("project.json is not valid JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "format",
        "version",
        "project_uuid",
        "created_at",
        "site",
        "facts",
        "template_references",
        "profile_references",
        "artifact_directories",
    }:
        raise ValueError("project.json has an unsupported shape")
    if (
        document["format"] != PROJECT_FORMAT
        or type(document["version"]) is not int
        or document["version"] != PROJECT_VERSION
    ):
        raise ValueError("project.json format or version is unsupported")
    if type(document["project_uuid"]) is not str or type(document["created_at"]) is not str:
        raise ValueError("project UUID or creation time is invalid")
    try:
        uuid.UUID(document["project_uuid"])
        created_at = datetime.fromisoformat(document["created_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("project UUID or creation time is invalid") from exc
    if created_at.tzinfo is None or created_at.utcoffset() != UTC.utcoffset(created_at):
        raise ValueError("project creation time must be RFC3339 UTC")
    raw_target = document["site"].get("target") if isinstance(document["site"], dict) else ""
    if not isinstance(raw_target, str):
        raise ValueError("project site identity is invalid")
    target = _target(raw_target)
    try:
        expected_site = _site(target, document["site"]["label"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("project site identity is invalid") from exc
    if (
        not isinstance(document["site"], dict)
        or set(document["site"]) != {"host", "target", "label"}
        or document["site"] != expected_site
    ):
        raise ValueError("project site identity is invalid")
    _facts(document["facts"])
    _references(document["template_references"], "template_references")
    _references(document["profile_references"], "profile_references")
    if document["artifact_directories"] != {
        "scans": "scans",
        "reports": "reports",
        "log": "log.md",
    }:
        raise ValueError("project artifact references must remain portable relative paths")
    return root, document


def open_project(directory: str | Path, *, expected_site: str | None = None) -> dict[str, Any]:
    """Open a validated project without resolving or executing template references."""
    root, document = _load(directory)
    if expected_site is not None and normalize_domain(expected_site) != document["site"]["host"]:
        raise ValueError("project site identity conflicts with the requested site")
    from .coverage import coverage_status
    from .runtime import aggregate_coverage

    return {"ok": True, "project": document, "path": str(root), "checklist": aggregate_coverage(str(root), coverage_status(root))}


def project_status(directory: str | Path) -> dict[str, Any]:
    """Report scan history, per-site coverage and recorded preparation state."""
    from .coverage import coverage_status
    from .runtime import aggregate_coverage, preparation_status

    root, document = _load(directory)
    return {
        "ok": True,
        "project": {
            "site": document["site"],
            "template_references": document["template_references"],
            "profile_references": document["profile_references"],
        },
        "scans": list_scans(root / "scans"),
        "checklist": aggregate_coverage(str(root), coverage_status(root)),
        "preparation": preparation_status(str(root)),
    }
