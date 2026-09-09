"""Project crawl policy, admission, bounded preparation and addressable playbooks."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import math
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .catalogue import load_catalogue
from .coverage import _now, coverage_status, initialize_coverage, update_item
from .workspace import _load, _target

POLICY_FORMAT = "seohead.project-crawl-policy.v1"
PREPARATION_FORMAT = "seohead.project-preparation.v1"
DEFAULT_POLICY = {
    "approval_thresholds": {"pages": 1000, "requests": 3000, "seconds": 600},
    "quick_crawl": {"pages": 50, "requests": 150, "seconds": 60},
    "crawl_overrides": {},
    "competitor_limit": 5,
}


def read_document(root: Path, name: str) -> dict | None:
    path = root / name
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(f"unsafe or oversized {name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{name} must contain an object")
    return data


def write_document(root: Path, name: str, data: dict, *, expected_revision: int | None = None) -> None:
    """Publish one bounded project document with a retained previous revision."""
    content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if len(content.encode()) > 16 * 1024 * 1024:
        raise ValueError("project document exceeds its byte budget")
    lock = root / ("." + name + ".lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError(f"another writer owns {name}") from exc
    os.close(fd)
    staged = None
    try:
        previous = read_document(root, name)
        revision = previous.get("revision", 0) if previous else 0
        if expected_revision is not None and (type(expected_revision) is not int or revision != expected_revision):
            raise ValueError("project document revision conflict")
        if previous is not None:
            backups = root / ".project-backups"
            if backups.is_symlink():
                raise ValueError("unsafe project backup directory")
            backups.mkdir(mode=0o700, exist_ok=True)
            backup = backups / f"{name}.{uuid.uuid4().hex}.json"
            descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write((root / name).read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        descriptor, staged = tempfile.mkstemp(prefix=".project-", dir=root)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, root / name)
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if staged:
            Path(staged).unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def _positive(value: Any, label: str) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000_000:
        raise ValueError(f"{label} must be a positive bounded integer")
    return value


def validate_policy(policy: Any) -> dict:
    from seohead.crawl.settings import load

    if not isinstance(policy, dict) or set(policy) != set(DEFAULT_POLICY):
        raise ValueError("crawl policy has unsupported fields")
    result = copy.deepcopy(policy)
    for section in ("approval_thresholds", "quick_crawl"):
        if not isinstance(result[section], dict) or set(result[section]) != {"pages", "requests", "seconds"}:
            raise ValueError(f"{section} requires pages, requests and seconds")
        for key, value in result[section].items():
            _positive(value, f"{section}.{key}")
    if not isinstance(result["crawl_overrides"], dict):
        raise ValueError("crawl_overrides must be a configuration object")
    # Validate with the collector's actual settings; policy never executes its text.
    load(overrides=result["crawl_overrides"])
    if _positive(result["competitor_limit"], "competitor_limit") > 20:
        raise ValueError("competitor_limit cannot exceed 20")
    return result


def project_policy(directory: str, policy: dict | None = None, apply: bool = False, expected_revision: int | None = None) -> dict:
    root, project = _load(directory)
    current = read_document(root, "crawl-policy.json")
    if current and (set(current) != {"format", "project_uuid", "revision", "policy"} or current["format"] != POLICY_FORMAT or current["project_uuid"] != project["project_uuid"] or type(current["revision"]) is not int):
        raise ValueError("unsupported or mismatched project crawl policy")
    selected = validate_policy(policy if policy is not None else current["policy"] if current else DEFAULT_POLICY)
    revision = current["revision"] if current else 0
    if type(apply) is not bool:
        raise ValueError("apply must be a boolean")
    if apply:
        if type(expected_revision) is not int:
            raise ValueError("expected_revision is required to change project policy")
        document = {"format": POLICY_FORMAT, "project_uuid": project["project_uuid"], "revision": revision + 1, "policy": selected}
        write_document(root, "crawl-policy.json", document, expected_revision=expected_revision)
        revision += 1
    return {"ok": True, "applied": apply, "revision": revision, "policy": selected}


def admission(directory: str, settings: dict, *, approved: bool = False) -> dict:
    """Bound requested work before the first request; missing bounds require approval."""
    if type(approved) is not bool:
        raise ValueError("approve_large_crawl must be a boolean")
    policy = project_policy(directory)["policy"]
    limits = settings["limits"]
    requested = {"pages": limits.get("max_urls"), "requests": limits.get("max_requests"), "seconds": limits.get("max_crawl_seconds")}
    exceeded = []
    for key, maximum in policy["approval_thresholds"].items():
        value = requested[key]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
            exceeded.append({"budget": key, "requested": value, "threshold": maximum, "reason": "request is not explicitly bounded"})
        elif value > maximum:
            exceeded.append({"budget": key, "requested": value, "threshold": maximum, "reason": "requested budget exceeds project threshold"})
    return {"ok": not exceeded or approved, "approved": approved, "requested": requested, "exceeded": exceeded, "reason": "explicit budget override accepted" if approved else "within project thresholds" if not exceeded else "project admission requires approve_large_crawl=true"}


def playbook_list(kind: str | None = None) -> dict:
    if kind not in {None, "skill", "scenario"}:
        raise ValueError("kind must be skill or scenario")
    entries = [{key: value for key, value in row.items() if key != "content"} for row in load_catalogue().values() if row["kind"] in ({kind} if kind else {"skill", "scenario"})]
    return {"ok": True, "items": entries}


def playbook_show(name: str, kind: str = "skill") -> dict:
    if not isinstance(name, str) or kind not in {"skill", "scenario"}:
        raise ValueError("invalid playbook request")
    matches = [row for key, row in load_catalogue().items() if row["kind"] == kind and (key == name or key.removeprefix(kind + ":") == name or key.rsplit("/", 1)[-1] == name)]
    if len(matches) != 1:
        raise ValueError("playbook name is missing or ambiguous; use its full catalogue ID")
    return {"ok": True, **matches[0]}


def preparation_status(directory: str) -> dict:
    root, project = _load(directory)
    state = read_document(root, "preparation.json")
    if state is None:
        return {"state": "pending", "reason": "bounded project preparation has not run"}
    if state.get("format") != PREPARATION_FORMAT or state.get("project_uuid") != project["project_uuid"] or not isinstance(state.get("steps"), dict) or not isinstance(state.get("competitors"), list):
        raise ValueError("invalid project preparation state")
    if state.get("state") == "running" and not (root / ".prepare.lock").exists():
        return {**state, "state": "interrupted", "reason": "preparation writer stopped before recording completion"}
    return state


def _candidate(value: Any) -> dict:
    if isinstance(value, str):
        return {"url": _target(value), "source": "operator supplied candidate", "observed_at": None}
    if not isinstance(value, dict) or set(value) != {"url", "source", "observed_at"}:
        raise ValueError("competitor candidates require url, source and observed_at")
    if not isinstance(value["source"], str) or not value["source"].strip() or len(value["source"]) > 512:
        raise ValueError("competitor source is required")
    return {**value, "url": _target(value["url"])}


def prepare_project(directory: str, *, tools: dict, template: dict | None = None, competitors: list | None = None, approve_large_crawl: bool = False, producer_build: str | None = None) -> dict:
    """Run the bounded native preparation path using explicitly injected shared tools."""
    from .priorities import project_priorities
    from .workspace import create_project, project_status

    root, project = _load(directory)
    policy = project_policy(directory)["policy"]
    if competitors is not None and (not isinstance(competitors, list) or len(competitors) > policy["competitor_limit"]):
        raise ValueError("competitor candidate list exceeds the project limit")
    candidates = [_candidate(value) for value in competitors or []]
    if len({row["url"] for row in candidates}) != len(candidates):
        raise ValueError("duplicate competitor candidates")
    if any(row["url"] == project["site"]["target"] for row in candidates):
        raise ValueError("the primary site cannot be its own competitor candidate")
    lock = root / ".prepare.lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ValueError("project preparation is already running") from exc
    os.close(fd)
    state = {"format": PREPARATION_FORMAT, "project_uuid": project["project_uuid"], "revision": 1, "started_at": _now(), "state": "running", "steps": {}, "competitors": []}
    def save():
        previous = read_document(root, "preparation.json")
        state["revision"] = (previous.get("revision", 0) if previous else 0) + 1
        write_document(root, "preparation.json", state)
    try:
        initialize_coverage(root, template=template)
        for slug, title in (("sitemap", "Review sitemap discovery and coverage"), ("crawl", "Review the bounded preparation crawl"), ("competitors", "Review competitor candidates"), ("plan", "Review the initial project plan"), ("delivery", "Review the final client deliverable")):
            item_id = "custom:preparation-" + slug
            current = coverage_status(root)
            if not any(row["id"] == item_id for row in current["items"]):
                update_item(root, {"id": item_id, "title": title, "execution_kind": "deliverable" if slug == "delivery" else "manual", "order": 0}, current["revision"])
        save()
        quick = policy["quick_crawl"]
        overrides = {**policy["crawl_overrides"], "limits.max_urls": quick["pages"], "limits.max_requests": quick["requests"], "limits.max_crawl_seconds": quick["seconds"], "sitemaps.auto_discover": True}
        state["steps"]["crawl"] = {"state": "running", "reason": "bounded native crawl requested", "budgets": quick}
        save()
        try:
            result = tools["crawl_site"](project=str(root), overrides=overrides, approve_large_crawl=approve_large_crawl, producer_build=producer_build)
            if not isinstance(result, dict) or result.get("ok") is False:
                raise ValueError(str(result.get("error", "crawl was unavailable")) if isinstance(result, dict) else "invalid crawl result")
            scan = result.get("scan") or result.get("scan_path")
            if isinstance(scan, dict):
                scan = scan.get("path")
            if not isinstance(scan, str):
                raise ValueError("crawl did not return its retained scan path")
            source = Path(scan).resolve()
            if not source.is_relative_to(root / "scans") or not source.is_file():
                raise ValueError("crawl result is not a retained project scan")
            relative = source.relative_to(root).as_posix()
            from seohead.storage import open_scan
            with open_scan(source) as con:
                header = dict(con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
                audit = json.loads(con.execute("SELECT document_json FROM audit WHERE singleton=1").fetchone()[0])
            state["steps"]["crawl"] = {"state": "run", "artifact": relative, "scan_uuid": header["scan_uuid"], "partial": bool(header["crawl_partial"]), "reason": header["finish_reason"]}
            sitemap_skips = [row for row in audit.get("run", {}).get("checks_skipped", []) if str(row.get("id", "")).startswith("SITEMAP_")]
            state["steps"]["sitemap"] = {"state": "partial" if sitemap_skips else "run", "artifact": relative, "reason": "saved sitemap coverage; unavailable checks remain explicit", "unavailable": sitemap_skips}
            from .coverage import record_execution
            executed = set(audit.get("summary", {}).get("check_coverage", {}).get("checks_silent_ids", []))
            executed.update(row.get("check") for row in audit.get("issues", []))
            unavailable = {row.get("id"): row.get("reason") for row in audit.get("run", {}).get("checks_skipped", [])}
            recording_errors = []
            for item in coverage_status(root)["items"]:
                if item["kind"] != "check" or item["complete"] or not item["enabled"]:
                    continue
                check_id = item["id"].removeprefix("check:")
                if check_id in executed:
                    entry = {"status": "succeeded", "reason": "Executed in the bounded preparation scan; see recorded measurement scope", "artifact": relative}
                elif check_id in unavailable:
                    entry = {"status": "unavailable", "reason": str(unavailable[check_id])}
                else:
                    continue
                try:
                    record_execution(root, item["id"], entry, coverage_status(root)["revision"])
                except ValueError as exc:
                    recording_errors.append({"id": item["id"], "reason": str(exc)})
            if recording_errors:
                state["steps"]["crawl"]["recording_gaps"] = recording_errors
        except (ValueError, OSError) as exc:
            state["steps"]["crawl"] = {"state": "not_run", "reason": str(exc)}
            state["steps"]["sitemap"] = {"state": "not_run", "reason": "crawl source unavailable; sitemap coverage not established"}
        save()
        if candidates:
            directory_root = root / "competitors"
            if directory_root.is_symlink():
                raise ValueError("unsafe competitor workspace directory")
            directory_root.mkdir(mode=0o700, exist_ok=True)
            for candidate in candidates:
                slug = hashlib.sha256(candidate["url"].encode()).hexdigest()[:16]
                child = directory_root / slug
                if not child.exists():
                    create_project(child, candidate["url"], facts=[{"name": "candidate_source", "value": candidate["source"], "provenance": "project preparation", "observed_at": candidate["observed_at"]}])
                _, child_project = _load(child)
                if child_project["site"]["target"] != candidate["url"]:
                    raise ValueError("competitor workspace site identity mismatch")
                initialize_coverage(child, template=None)
                state["competitors"].append({**candidate, "directory": child.relative_to(root).as_posix(), "project_uuid": child_project["project_uuid"], "state": "candidate; audit not run"})
            state["steps"]["competitors"] = {"state": "run", "reason": "operator-supplied candidate shortlist; competitiveness is not verified", "count": len(candidates)}
        else:
            state["steps"]["competitors"] = {"state": "not_run", "reason": "no supplied or authorized competitor source; no competitors invented"}
        current = coverage_status(root)
        project_priorities(str(root), apply=True, expected_revision=current["revision"])
        plan_name = "initial-plan-" + uuid.uuid4().hex[:12] + ".md"
        state["steps"]["plan"] = {"state": "run", "artifact": "reports/" + plan_name, "reason": "initial work plan created; manual and deliverable acceptance remain pending"}
        lines = ["# Initial project plan", "", f"Target: {project['site']['target']}", "", "Preparation observations:"]
        lines.extend(f"- {name}: {row['state']} — {row.get('reason', '')}" for name, row in state["steps"].items())
        lines.extend(["", "Review the preparation checklist, verify candidate competitors, and use project status for remaining work. This file does not establish a completed audit or client delivery.", ""])
        (root / "reports" / plan_name).write_text("\n".join(lines), encoding="utf-8")
        state["state"] = "prepared" if all(row["state"] == "run" for row in state["steps"].values()) else "partial"
        state["finished_at"] = _now()
        save()
        return {"ok": True, "preparation": state, "status": project_status(str(root))}
    except (Exception, KeyboardInterrupt) as exc:
        state.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", reason=str(exc) or "preparation interrupted", finished_at=_now())
        with contextlib.suppress(OSError, ValueError):
            save()
        raise
    finally:
        lock.unlink(missing_ok=True)


def aggregate_coverage(directory: str, primary: dict) -> dict:
    """Combine declared site checklists without assigning another site's evidence to a row."""
    root, project = _load(directory)
    preparation = preparation_status(directory)
    children = preparation.get("competitors", [])
    if not children:
        return primary
    sites = [{"directory": ".", "project_uuid": project["project_uuid"], "site": project["site"]["target"], "checklist": primary}]
    seen = {project["project_uuid"]}
    for child in children:
        relative = child.get("directory")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts or not relative.startswith("competitors/"):
            raise ValueError("unsafe competitor project reference")
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("unsafe competitor project reference")
        _, metadata = _load(path)
        if metadata["project_uuid"] != child.get("project_uuid") or metadata["site"]["target"] != child.get("url") or metadata["project_uuid"] in seen:
            raise ValueError("competitor project identity mismatch")
        seen.add(metadata["project_uuid"])
        sites.append({"directory": relative, "project_uuid": metadata["project_uuid"], "site": metadata["site"]["target"], "checklist": coverage_status(path)})
    result = copy.deepcopy(primary)
    result["sites"] = [{key: value for key, value in site.items() if key != "checklist"} | {"counts": site["checklist"].get("counts"), "state": site["checklist"]["state"]} for site in sites]
    if any(site["checklist"]["state"] != "initialized" for site in sites):
        result.update(complete=False, counts_known=False, reason="one or more site checklists are not initialized")
        return result
    result["counts"] = {key: sum(site["checklist"]["counts"][key] for site in sites) for key in primary["counts"]}
    result["by_kind"] = {kind: {key: sum(site["checklist"]["by_kind"][kind][key] for site in sites) for key in ("total", "complete")} for kind in primary["by_kind"]}
    result["items"] = []
    result["views"] = {key: [] for key in primary["views"]}
    for site in sites:
        prefix = "site:" + site["project_uuid"] + "/"
        for item in site["checklist"]["items"]:
            result["items"].append({**item, "id": prefix + item["id"], "item_id": item["id"], "project_directory": site["directory"], "project_uuid": site["project_uuid"]})
        for name, ids in site["checklist"]["views"].items():
            result["views"][name].extend(prefix + item_id for item_id in ids)
    result["complete"] = all(site["checklist"]["complete"] for site in sites)
    result["counts_known"] = True
    return result


def resolve_item_scope(directory: str, item_id: str) -> tuple[str, str]:
    """Resolve only declared qualified site rows; unqualified IDs keep their local scope."""
    if not isinstance(item_id, str) or not item_id.startswith("site:"):
        return directory, item_id
    prefix, separator, local_id = item_id.partition("/")
    if not separator:
        raise ValueError("qualified item ID has no local ID")
    root, project = _load(directory)
    requested = prefix.removeprefix("site:")
    if requested == project["project_uuid"]:
        return str(root), local_id
    for child in preparation_status(directory).get("competitors", []):
        if child.get("project_uuid") == requested:
            # Reuse the complete validation before resolving a writable child.
            aggregate_coverage(directory, coverage_status(root))
            return str(root / child["directory"]), local_id
    raise ValueError("item belongs to an undeclared project scope")
