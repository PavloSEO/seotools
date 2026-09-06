"""Shared local interfaces for explicit saved-scan history actions."""

from __future__ import annotations

import json
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


def scan_snapshot(input_path: str, out: str) -> dict[str, Any]:
    return {"snapshot": snapshot_scan(_path(input_path, "input"), _path(out, "out"))}


def scan_pin(input_path: str, *, pinned: bool = True) -> dict[str, Any]:
    path = _path(input_path, "input")
    if type(pinned) is not bool:
        raise ValueError("pinned must be a boolean")
    pin_scan(path, pinned)
    return {"input": path, "pinned": pinned}


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
    "scan_inspect",
    "scan_list",
    "scan_pin",
    "scan_prune",
    "scan_snapshot",
]
