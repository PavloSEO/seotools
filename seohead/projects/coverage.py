"""Portable checklist definitions, append-only history and offline completion views."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalogue import load_catalogue
from .workspace import _load, _target

FORMAT = "seohead.coverage.v1"
MAX_BYTES = 32 * 1024 * 1024
_ID = re.compile(
    r"(?:check:[A-Z][A-Z0-9_]*|skill:(?:workflow|general)/[a-z0-9_-]+|scenario:[a-z0-9_-]+|custom:[a-z][a-z0-9._/-]{0,127})\Z"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _text(value: Any, name: str, limit: int = 2048) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{name} must be nonempty text of at most {limit} characters")
    return value


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value) or ".." in value:
        raise ValueError("invalid checklist item identifier")
    return value


def _scope(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"site", "template", "urls"}:
        raise ValueError("scope requires site, template and urls")
    site = _target(value["site"])
    template = value["template"]
    if template is not None:
        _text(template, "template", 128)
    urls = value["urls"]
    if not isinstance(urls, list) or len(urls) > 10000:
        raise ValueError("scope urls must be a bounded list")
    from urllib.parse import urlsplit

    from seohead.recon.net import normalize_url

    for url in urls:
        if not isinstance(url, str) or len(url) > 2048 or normalize_url(url) != url:
            raise ValueError("scope urls must be normalized absolute URLs")
        if urlsplit(url).netloc != urlsplit(site).netloc:
            raise ValueError("scope URL belongs to another site")
    if len(set(urls)) != len(urls):
        raise ValueError("duplicate scope URL")
    return {"site": site, "template": template, "urls": urls}


def _definition(value: Any, catalogue: dict, *, historical: bool = False) -> dict:
    required = {
        "id",
        "title",
        "kind",
        "scope",
        "dependencies",
        "execution_kind",
        "priority",
        "enabled",
        "order",
        "operation",
        "source_hash",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("item definition has unsupported fields")
    value = copy.deepcopy(value)
    item_id = _identifier(value["id"])
    _text(value["title"], "title", 512)
    if (
        not isinstance(value["kind"], str)
        or value["kind"] not in {"check", "skill", "scenario", "custom"}
        or not item_id.startswith(value["kind"] + ":")
    ):
        raise ValueError("item kind does not match identifier")
    if not historical and value["kind"] != "custom" and item_id not in catalogue:
        raise ValueError("unknown built-in item")
    value["scope"] = _scope(value["scope"])
    if not isinstance(value["execution_kind"], str) or value["execution_kind"] not in {
        "automatic",
        "manual",
        "deliverable",
    }:
        raise ValueError("invalid execution kind")
    if not isinstance(value["priority"], str) or value["priority"] not in {"P0", "P1", "P2"}:
        raise ValueError("invalid priority")
    if (
        type(value["enabled"]) is not bool
        or type(value["order"]) is not int
        or not 0 <= value["order"] <= 1000000
    ):
        raise ValueError("invalid enabled/order value")
    dependencies = value["dependencies"]
    if not isinstance(dependencies, list) or len(dependencies) > 1000:
        raise ValueError("dependencies must be a bounded list")
    for dep in dependencies:
        _identifier(dep)
    if item_id in dependencies or len(set(dependencies)) != len(dependencies):
        raise ValueError("duplicate or self dependency")
    operation = value["operation"]
    if value["execution_kind"] == "automatic":
        if (
            not isinstance(operation, str)
            or not operation.startswith("check:")
            or (
                not historical
                and (operation not in catalogue or catalogue[operation]["kind"] != "check")
            )
        ):
            raise ValueError("automatic items bind to a registered check")
    elif operation is not None:
        raise ValueError("manual/deliverable items do not execute operations")
    if value["source_hash"] is not None and (
        not isinstance(value["source_hash"], str)
        or not re.fullmatch("[a-f0-9]{64}", value["source_hash"])
    ):
        raise ValueError("invalid source definition hash")
    return value


def _dependencies(items: dict) -> None:
    done, active = set(), set()

    def visit(item_id: str) -> None:
        if item_id in active:
            raise ValueError("cyclic checklist dependency")
        if item_id in done:
            return
        if item_id not in items:
            raise ValueError(f"unknown dependency {item_id}")
        if len(active) >= 100:
            raise ValueError("dependency chain exceeds supported depth")
        active.add(item_id)
        for dep in items[item_id]["definition"]["dependencies"]:
            visit(dep)
        active.remove(item_id)
        done.add(item_id)

    for item_id in items:
        visit(item_id)


def _read(root: Path, project: dict) -> dict | None:
    path = root / "coverage.json"
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("coverage.json is unsafe or exceeds its byte limit")
    try:
        document = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise ValueError("coverage.json is not valid JSON") from exc
    if not isinstance(document, dict) or set(document) != {
        "format",
        "version",
        "project_uuid",
        "revision",
        "items",
    }:
        raise ValueError("unsupported coverage document shape")
    if (
        document["format"] != FORMAT
        or type(document["version"]) is not int
        or document["version"] != 1
    ):
        raise ValueError("unsupported coverage version")
    if document["project_uuid"] != project["project_uuid"]:
        raise ValueError("coverage project UUID mismatch")
    if (
        type(document["revision"]) is not int
        or document["revision"] < 1
        or not isinstance(document["items"], dict)
    ):
        raise ValueError("invalid coverage revision/items")
    if len(document["items"]) > 10000:
        raise ValueError("too many checklist items")
    for item_id, item in document["items"].items():
        _identifier(item_id)
        if not isinstance(item, dict) or set(item) != {"definition", "definitions", "records"}:
            raise ValueError("invalid checklist history")
        if not isinstance(item["definition"], dict) or item["definition"].get("id") != item_id:
            raise ValueError("item identity mismatch")
        if not isinstance(item["definitions"], list) or not isinstance(item["records"], list):
            raise ValueError("invalid checklist history lists")
        if (
            not item["definitions"]
            or item["definitions"][-1].get("definition") != item["definition"]
        ):
            raise ValueError("current definition is absent from history")
        _definition(item["definition"], {}, historical=True)
        hashes = {}
        for version in item["definitions"]:
            if not isinstance(version, dict) or set(version) != {"observed_at", "definition"}:
                raise ValueError("invalid definition history entry")
            _text(version["observed_at"], "definition observation time", 128)
            _definition(version["definition"], {}, historical=True)
            if version["definition"]["id"] != item_id:
                raise ValueError("historical item identity mismatch")
            hashes[_hash(version["definition"])] = version["definition"]
        for record in item["records"]:
            _record_shape(record, hashes)
    _dependencies(document["items"])
    return document


def _record_shape(record: Any, definition_hashes: dict) -> None:
    allowed = {
        "status",
        "reason",
        "measurement",
        "reviewer",
        "signoff",
        "artifact",
        "sha256",
        "review",
        "scope",
        "site",
        "operation",
        "operation_hash",
        "source",
        "observed_at",
        "definition_hash",
        "recorded_at",
    }
    if (
        not isinstance(record, dict)
        or set(record) - allowed
        or not {"status", "reason", "measurement", "definition_hash", "recorded_at"} <= set(record)
    ):
        raise ValueError("invalid execution history entry")
    if not isinstance(record["status"], str) or record["status"] not in {
        "running",
        "failed",
        "unavailable",
        "succeeded",
        "not_applicable",
    }:
        raise ValueError("invalid execution history status")
    _text(record["reason"], "record reason")
    _text(record["recorded_at"], "record timestamp", 128)
    if (
        not isinstance(record["definition_hash"], str)
        or record["definition_hash"] not in definition_hashes
    ):
        raise ValueError("execution refers to an unknown definition")
    if record["measurement"] is not None and (
        not isinstance(record["measurement"], dict)
        or not isinstance(record["measurement"].get("state"), str)
        or record["measurement"].get("state") not in {"limited", "measured", "not_measured"}
    ):
        raise ValueError("invalid measurement record")
    if "artifact" in record:
        _text(record["artifact"], "artifact", 1024)
        if not isinstance(record.get("sha256"), str) or not re.fullmatch(
            "[a-f0-9]{64}", record["sha256"]
        ):
            raise ValueError("invalid evidence digest")
    if "operation" in record:
        _identifier(record["operation"])
        if not isinstance(record.get("operation_hash"), str) or not re.fullmatch(
            "[a-f0-9]{64}", record["operation_hash"]
        ):
            raise ValueError("invalid operation digest")
    if record["status"] in {"not_applicable", "succeeded"} and "operation" not in record:
        _text(record.get("reviewer"), "reviewer", 128)
    if record["status"] == "succeeded" and not (
        "artifact" in record or record.get("signoff") is True
    ):
        raise ValueError("successful history requires evidence or signoff")
    if record["status"] == "succeeded":
        definition = definition_hashes[record["definition_hash"]]
        if definition["execution_kind"] == "automatic":
            if (
                record.get("operation") != definition["operation"]
                or "artifact" not in record
                or record["measurement"] is None
            ):
                raise ValueError("automatic history is missing bound measurement evidence")
        elif (
            not (definition["execution_kind"] == "manual" and record.get("signoff") is True)
            and record.get("review") != "approved"
        ):
            raise ValueError("review history is missing approval")


@contextmanager
def _transaction(directory: str | Path, expected_revision: int | None):
    root, project = _load(directory)
    lock = root / ".coverage.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(
            "coverage writer is busy; inspect a leftover lock after an interrupted process"
        ) from exc
    try:
        os.close(fd)
        document = _read(root, project)
        revision = document["revision"] if document else 0
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision != revision
        ):
            raise ValueError(f"coverage revision conflict: current revision is {revision}")
        if document is None:
            document = {
                "format": FORMAT,
                "version": 1,
                "project_uuid": project["project_uuid"],
                "revision": 0,
                "items": {},
            }
        yield root, project, document
        _dependencies(document["items"])
        document["revision"] += 1
        content = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if len(content.encode()) > MAX_BYTES:
            raise ValueError("coverage exceeds its byte limit")
        stage_fd, stage_name = tempfile.mkstemp(prefix=".coverage-", dir=root)
        try:
            with os.fdopen(stage_fd, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(stage_name, root / "coverage.json")
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(stage_name).unlink(missing_ok=True)
    finally:
        lock.unlink(missing_ok=True)


def _set(items: dict, definition: dict) -> None:
    item_id = definition["id"]
    if item_id not in items:
        items[item_id] = {"definition": definition, "definitions": [], "records": []}
    item = items[item_id]
    if not item["definitions"] or item["definition"] != definition:
        item["definitions"].append({"observed_at": _now(), "definition": copy.deepcopy(definition)})
        item["definition"] = definition


def _custom(value: Any, site: str, catalogue: dict, previous: dict | None = None) -> dict:
    if not isinstance(value, dict):
        raise ValueError("item must be an object")
    item_id = _identifier(value.get("id"))
    if not item_id.startswith("custom:") and previous is None:
        raise ValueError("new operator items require the custom namespace")
    base = previous or {
        "id": item_id,
        "title": item_id,
        "kind": "custom",
        "scope": {"site": site, "template": None, "urls": []},
        "dependencies": [],
        "execution_kind": "manual",
        "priority": "P1",
        "enabled": True,
        "order": 0,
        "operation": None,
        "source_hash": None,
    }
    if set(value) - set(base):
        raise ValueError("unknown item definition fields")
    if previous and any(
        value.get(k, previous[k]) != previous[k] for k in ("id", "kind", "source_hash")
    ):
        raise ValueError("item identity/source is immutable")
    removed = previous is not None and previous["kind"] != "custom" and item_id not in catalogue
    if removed and set(value) - {"id", "enabled", "order", "priority", "title"}:
        raise ValueError("removed built-ins can only be disabled or relabeled")
    return _definition({**base, **value}, catalogue, historical=removed)


def _builtin(item_id: str, entry: dict, order: int, site: str) -> dict:
    return {
        "id": item_id,
        "title": entry["title"],
        "kind": entry["kind"],
        "scope": {"site": site, "template": None, "urls": []},
        "dependencies": [],
        "execution_kind": "automatic" if entry["kind"] == "check" else "manual",
        "priority": "P1",
        "enabled": True,
        "order": order,
        "operation": item_id if entry["kind"] == "check" else None,
        "source_hash": entry["definition_hash"],
    }


def initialize_coverage(
    directory: str | Path, template: dict | None = None, expected_revision: int | None = None
) -> dict:
    """Initialize or reconcile built-ins and optionally apply a reusable data-only template."""
    catalogue = load_catalogue()
    if template is not None and (
        not isinstance(template, dict)
        or set(template) != {"format", "items"}
        or template["format"] != "seohead.checklist-template.v1"
        or not isinstance(template["items"], list)
    ):
        raise ValueError("unsupported checklist template")
    with _transaction(directory, expected_revision) as (_, project, document):
        items = document["items"]
        for order, (item_id, entry) in enumerate(catalogue.items()):
            if item_id in items:
                definition = {
                    **items[item_id]["definition"],
                    "source_hash": entry["definition_hash"],
                    "title": entry["title"],
                }
            else:
                definition = _builtin(item_id, entry, order, project["site"]["target"])
            _set(items, _definition(definition, catalogue))
        seen = set()
        for value in (template or {}).get("items", []):
            if not isinstance(value, dict):
                raise ValueError("invalid template item")
            _identifier(value.get("id"))
            if value.get("id") in seen:
                raise ValueError("invalid or duplicate template item")
            seen.add(value.get("id"))
            previous = items.get(value.get("id"), {}).get("definition")
            _set(items, _custom(value, project["site"]["target"], catalogue, previous))
    return coverage_status(directory)


def update_item(directory: str | Path, item: dict, expected_revision: int) -> dict:
    """Add or edit one item, preserving definitions and result history."""
    if type(expected_revision) is not int:
        raise ValueError("expected_revision must be an integer")
    if not isinstance(item, dict):
        raise ValueError("item must be an object")
    _identifier(item.get("id"))
    catalogue = load_catalogue()
    with _transaction(directory, expected_revision) as (_, project, document):
        if document["revision"] == 0:
            raise ValueError("initialize the checklist first")
        previous = (
            document["items"].get(item.get("id"), {}).get("definition")
            if isinstance(item, dict)
            else None
        )
        _set(document["items"], _custom(item, project["site"]["target"], catalogue, previous))
    return coverage_status(directory)


def record_execution(
    directory: str | Path, item_id: str, record: dict, expected_revision: int
) -> dict:
    """Record an attempt or explicit applicability review; never execute an operation."""
    from .evidence import validate_record

    _identifier(item_id)
    if type(expected_revision) is not int:
        raise ValueError("expected_revision must be an integer")
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    with _transaction(directory, expected_revision) as (root, _, document):
        if item_id not in document["items"]:
            raise ValueError("unknown checklist item")
        item = document["items"][item_id]
        view = _status(root, document, load_catalogue())
        current = next(row for row in view["items"] if row["id"] == item_id)
        if record.get("status") == "succeeded" and current["blocked_by"]:
            raise ValueError("dependencies are not complete")
        if not item["definition"]["enabled"]:
            raise ValueError("item is disabled")
        if record.get("status") == "succeeded":
            _definition(item["definition"], load_catalogue())
            if (
                item["definition"]["kind"] != "custom"
                and item["definition"]["source_hash"]
                != load_catalogue()[item_id]["definition_hash"]
            ):
                raise ValueError("source definition changed; reconcile checklist first")
        validated = validate_record(root, item["definition"], record)
        item["records"].append(
            {**validated, "definition_hash": _hash(item["definition"]), "recorded_at": _now()}
        )
    return coverage_status(directory)


def _status(root: Path, document: dict, catalogue: dict) -> dict:
    from .evidence import evidence_stale

    document = copy.deepcopy(document)
    _, project = _load(root)
    for order, (item_id, entry) in enumerate(catalogue.items()):
        if item_id not in document["items"]:
            _set(document["items"], _builtin(item_id, entry, order, project["site"]["target"]))
    rows = {}
    digests = {}
    for item_id, item in document["items"].items():
        definition = item["definition"]
        record = item["records"][-1] if item["records"] else {}
        source_stale = definition["kind"] != "custom" and (
            item_id not in catalogue
            or definition["source_hash"] != catalogue[item_id]["definition_hash"]
        )
        stale_reason = "source definition changed or was removed" if source_stale else ""
        if record and record["definition_hash"] != _hash(definition):
            stale_reason = "item definition changed"
        if record and not stale_reason:
            stale_reason = evidence_stale(root, record, catalogue, digests)
        state = "not_run"
        if record and not stale_reason:
            if record["status"] == "not_applicable":
                state = "not_applicable"
            elif record["status"] == "succeeded":
                state = "run"
        rows[item_id] = {
            "id": item_id,
            "title": definition["title"],
            "kind": definition["kind"],
            "scope": definition["scope"],
            "priority": definition["priority"],
            "order": definition["order"],
            "enabled": definition["enabled"],
            "execution_kind": definition["execution_kind"],
            "state": state,
            "stale": bool(stale_reason),
            "reason": stale_reason or record.get("reason") or "not attempted",
            "attempt_status": record.get("status", "not_run"),
            "measurement": record.get("measurement"),
            "blocked_by": [],
            "complete": state in {"run", "not_applicable"} and definition["enabled"],
            "definition_versions": len(item["definitions"]),
            "attempts": len(item["records"]),
        }
    visited = set()

    def resolve(item_id):
        if item_id in visited:
            return
        for dep in document["items"][item_id]["definition"]["dependencies"]:
            resolve(dep)
            if not rows[dep]["complete"]:
                rows[item_id]["blocked_by"].append(dep)
        if rows[item_id]["blocked_by"]:
            rows[item_id]["complete"] = False
        visited.add(item_id)

    for item_id in rows:
        resolve(item_id)
    ordered = sorted(rows.values(), key=lambda row: (row["order"], row["id"]))
    active = [row for row in ordered if row["enabled"]]
    counts = {
        name: sum(row["state"] == name for row in active)
        for name in ("run", "not_applicable", "not_run")
    }
    counts.update(
        total=len(active),
        disabled=len(ordered) - len(active),
        complete=sum(row["complete"] for row in active),
        stale=sum(row["stale"] for row in active),
    )
    counts["remaining"] = counts["total"] - counts["complete"]
    views = {
        "running": [
            row["id"] for row in active if row["attempt_status"] == "running" and not row["stale"]
        ],
        "blocked": [
            row["id"]
            for row in active
            if row["blocked_by"] or row["attempt_status"] in {"failed", "unavailable"}
        ],
        "waiting_for_manual_review": [
            row["id"]
            for row in active
            if not row["complete"] and row["execution_kind"] in {"manual", "deliverable"}
        ],
        "deliverable_ready": [
            row["id"]
            for row in active
            if row["complete"] and row["state"] == "run" and row["execution_kind"] == "deliverable"
        ],
        "remaining": [row["id"] for row in active if not row["complete"]],
    }
    by_kind = {
        kind: {
            "total": sum(row["kind"] == kind for row in active),
            "complete": sum(row["kind"] == kind and row["complete"] for row in active),
        }
        for kind in ("check", "skill", "scenario", "custom")
    }
    return {
        "state": "initialized",
        "revision": document["revision"],
        "project_uuid": document["project_uuid"],
        "counts": counts,
        "by_kind": by_kind,
        "views": views,
        "items": ordered,
        "complete": bool(active) and counts["remaining"] == 0,
    }


def coverage_status(directory: str | Path) -> dict:
    """Read definitions and evidence without writes or network requests."""
    root, project = _load(directory)
    document = _read(root, project)
    if document is None:
        return {
            "state": "not_initialized",
            "reason": "coverage checklist is initialized by project checklist setup, not project creation",
        }
    return _status(root, document, load_catalogue())
