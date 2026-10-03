"""Shared local interfaces for explicit saved-scan history actions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from seohead.storage import open_scan
from seohead.storage.body_diff import body_diff
from seohead.storage.history import (
    inspect_scan,
    list_scans,
    pin_scan,
    prune_apply,
    prune_preview,
    snapshot_scan,
)
from seohead.storage.status import scan_status as _scan_status


def scan_link_inspect(
    input_path: str,
    view: str = "path",
    seed: str | None = None,
    target: str | None = None,
    representation: str = "all",
    cursor: str | None = None,
    link_id: int | None = None,
    document_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    max_bytes: int = 1_048_576,
    max_body_bytes: int = 5 * 1024 * 1024,
    max_nodes: int = 10_000,
    max_edges: int = 200_000,
    max_depth: int = 20,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    """One bounded saved-scan link query, shared by the CLI and local MCP."""
    if not isinstance(view, str) or view not in {"path", "inlinks", "context"}:
        return {"ok": False, "view": "invalid", "error": "view must be path, inlinks, or context"}
    try:
        path = _path(input_path, "scan")
        if type(max_bytes) is not int or not 4096 <= max_bytes <= 8 * 1024 * 1024:
            raise ValueError("max_bytes must be 4096..8388608")
        if view == "path":
            if not seed or not target:
                raise ValueError("path view requires seed and target URLs")
            if (
                any(value is not None for value in (cursor, link_id, document_id))
                or offset
                or limit != 100
            ):
                raise ValueError(
                    "path view does not accept cursor, link/document ID, offset or limit"
                )
            if max_body_bytes != 5 * 1024 * 1024:
                raise ValueError("path view does not read a body")
            from seohead.storage.link_queries import shortest_observed_path

            result = shortest_observed_path(
                path,
                seed,
                target,
                representation=representation,
                max_nodes=max_nodes,
                max_edges=max_edges,
                max_depth=max_depth,
                timeout_seconds=timeout_seconds,
            )
        elif view == "inlinks":
            if not target:
                raise ValueError("inlinks view requires a target URL")
            if seed is not None or link_id is not None or document_id is not None or offset:
                raise ValueError("inlinks view does not accept seed, link/document ID or offset")
            if (max_nodes, max_edges, max_depth, max_body_bytes) != (
                10_000,
                200_000,
                20,
                5 * 1024 * 1024,
            ):
                raise ValueError("inlinks view does not accept path or body budgets")
            from seohead.storage.link_queries import reverse_inlinks

            result = reverse_inlinks(
                path,
                target,
                representation=representation,
                cursor=cursor,
                limit=limit,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
            )
        elif view == "context":
            if (link_id is None) == (document_id is None):
                raise ValueError("context view requires exactly one of link_id or document_id")
            if seed is not None or target is not None or cursor is not None:
                raise ValueError("context view does not accept seed, target or cursor")
            if (max_nodes, max_edges, max_depth, timeout_seconds) != (10_000, 200_000, 20, 15.0):
                raise ValueError("context view does not accept path budgets")
            from seohead.storage.link_context import context_for_link, contexts_for_document

            if link_id is not None:
                if offset or limit != 100:
                    raise ValueError("single-link context does not accept offset or limit")
                result = context_for_link(
                    path, link_id, max_body_bytes=max_body_bytes, max_result_bytes=max_bytes
                )
            else:
                result = contexts_for_document(
                    path,
                    document_id,
                    offset=offset,
                    limit=limit,
                    max_body_bytes=max_body_bytes,
                    max_result_bytes=max_bytes,
                )
            if representation != "all" and result["representation"] != representation:
                raise ValueError("context representation differs from the requested filter")
        answer = {"ok": True, "view": view, **result}
        size = len(json.dumps(answer, ensure_ascii=False, default=str).encode("utf-8"))
        if size > max_bytes:
            return {
                "ok": False,
                "view": view,
                "state": "limit_reached",
                "reason": "output_byte_limit_exceeded",
                "scan_uuid": result.get("scan_uuid"),
                "evidence_revision": result.get("evidence_revision"),
                "max_bytes": max_bytes,
                "bytes_required": size,
            }
        return answer
    except (ValueError, OSError, sqlite3.Error, TypeError) as exc:
        return {"ok": False, "view": view, "error": str(exc)}


def _path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    return value


def scan_list(directory: str, *, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    return list_scans(_path(directory, "directory"), offset=offset, limit=limit)


def scan_inspect(
    input_path: str,
    *,
    table: str = "pages",
    offset: int = 0,
    limit: int = 100,
    max_bytes: int = 1_048_576,
) -> dict[str, Any]:
    return inspect_scan(
        _path(input_path, "input"),
        table=table,
        offset=offset,
        limit=limit,
        max_bytes=max_bytes,
    )


def scan_status(input_path: str) -> dict[str, Any]:
    """Summarize saved frontier work and committed page outcomes offline."""
    return _scan_status(_path(input_path, "input"))


def scan_rendered_routes(input_path: str) -> dict[str, Any]:
    """Read stored route observations without crawling or rendering."""
    from seohead.storage.rendered_routes import read

    con = open_scan(_path(input_path, "input"), require_audit=False)
    try:
        return read(con)
    finally:
        con.close()


def scan_snapshot(input_path: str, out: str) -> dict[str, Any]:
    return {"snapshot": snapshot_scan(_path(input_path, "input"), _path(out, "out"))}


def scan_pin(input_path: str, *, pinned: bool = True) -> dict[str, Any]:
    path = _path(input_path, "input")
    if type(pinned) is not bool:
        raise ValueError("pinned must be a boolean")
    pin_scan(path, pinned)
    return {"input": path, "pinned": pinned}


def scan_requeue(
    input_path: str,
    *,
    where: str,
    backup_path: str,
    from_scan: str | None = None,
) -> dict[str, Any]:
    """Explicitly upgrade one scan for a selected retry, retaining a verified backup."""
    from seohead.storage.retry import requeue_scan

    return requeue_scan(
        _path(input_path, "input"),
        where=_path(where, "where"),
        backup_path=_path(backup_path, "backup_path"),
        from_scan=_path(from_scan, "from_scan") if from_scan is not None else None,
    )


def scan_import_urls(input_path: str, *, urls_file: str, backup_path: str) -> dict[str, Any]:
    """Add an external URL list through a scan's stored admission policy."""
    from seohead.storage.retry import scan_import_urls as core

    return core(
        _path(input_path, "input"),
        urls_file=_path(urls_file, "urls_file"),
        backup_path=_path(backup_path, "backup_path"),
    )


def _plan(value: dict[str, Any] | str | None) -> dict[str, Any]:
    if isinstance(value, str) and value:
        with open(value, "rb") as handle:
            raw = handle.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024:
            raise ValueError("prune plan exceeds 64 MiB")
        value = json.loads(raw)
    if isinstance(value, dict):
        # Accept the exact preview envelope emitted by CLI/MCP, so redirecting
        # stdout to a file produces the same reviewable plan passed on apply.
        if set(value) == {"applied", "plan"} and value["applied"] is False:
            value = value["plan"]
        if isinstance(value, dict):
            return value
    raise ValueError("prune --apply requires a reviewed plan object or JSON file")


def scan_prune(
    directory: str,
    *,
    older_than_days: int = 30,
    keep_newest: int = 5,
    plan: dict[str, Any] | str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    root = _path(directory, "directory")
    if type(apply) is not bool:
        raise ValueError("apply must be a boolean")
    if not apply:
        if plan is not None:
            raise ValueError("a prune plan is only accepted with apply=true")
        return {
            "applied": False,
            "plan": prune_preview(root, older_than_days=older_than_days, keep_newest=keep_newest),
        }
    removed = prune_apply(root, _plan(plan))
    return {"applied": True, "removed": removed}


def scan_body_diff(
    left: str,
    right: str,
    url: str,
    *,
    variant_key: str | None = None,
    representation: str = "static",
    text: bool = False,
    max_bytes: int = 5 * 1024 * 1024,
    max_lines: int = 10_000,
) -> dict[str, Any]:
    left_con = open_scan(_path(left, "left"), require_audit=False)
    try:
        right_con = open_scan(_path(right, "right"), require_audit=False)
        try:
            return body_diff(
                left_con,
                right_con,
                _path(url, "url"),
                variant_key=variant_key,
                representation=representation,
                text=text,
                max_bytes=max_bytes,
                max_lines=max_lines,
            )
        finally:
            right_con.close()
    finally:
        left_con.close()


__all__ = [
    "scan_body_diff",
    "scan_import_urls",
    "scan_inspect",
    "scan_list",
    "scan_pin",
    "scan_prune",
    "scan_rendered_routes",
    "scan_requeue",
    "scan_snapshot",
]
