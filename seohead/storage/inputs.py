"""Resolve a saved audit without replacing it with a neighboring export."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from . import MAX_JSON_BYTES, _loads, open_scan

SQLITE_MAGIC = b"SQLite format 3\x00"


def is_sqlite_input(value: Any) -> bool:
    if not isinstance(value, (str, os.PathLike)):
        return False
    try:
        with Path(value).open("rb") as stream:
            return stream.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def _adjacent_diagnostics(path: Path, digest: str) -> list[dict[str, str]]:
    adjacent = path.with_name("audit.json")
    if not os.path.lexists(adjacent):
        return []
    try:
        if os.path.samefile(path, adjacent):
            return []
        with adjacent.open("rb") as stream:
            content = stream.read(MAX_JSON_BYTES + 1)
        if len(content) > MAX_JSON_BYTES:
            raise ValueError("adjacent audit exceeds the supported input limit")
        if hashlib.sha256(content).hexdigest() == digest:
            return []
        return [
            {
                "code": "adjacent_audit_mismatch",
                "path": "audit.json",
                "message": "Adjacent audit.json differs; used the scan's internal audit.",
            }
        ]
    except (OSError, ValueError):
        return [
            {
                "code": "adjacent_audit_unavailable",
                "path": "audit.json",
                "message": "Could not validate adjacent audit.json; used the scan's internal audit.",
            }
        ]


def resolve_audit_input(value: Any) -> tuple[Any, list[dict[str, str]]]:
    """Return the original document and separate input diagnostics, without network access."""
    if isinstance(value, dict):
        return value, []
    path = Path(str(value))
    if not is_sqlite_input(path):
        return json.loads(path.read_text(encoding="utf-8")), []
    con = open_scan(path)
    try:
        row = con.execute("SELECT document_json, sha256 FROM audit WHERE singleton=1").fetchone()
        return _loads(row["document_json"], "audit"), _adjacent_diagnostics(path, row["sha256"])
    finally:
        con.close()


def load_audit_document(
    value: Any, label: str, diagnostics: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Accept an already-parsed audit document, or a path to one, without changing its
    contents. ``label`` names the argument in the error, so a caller taking more than one
    audit (a before/after compare, a source/target segment diff) can say which one is bad.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, os.PathLike)):
        document, notices = resolve_audit_input(value)
        if diagnostics is not None:
            diagnostics.extend({**notice, "input": label} for notice in notices)
        return document
    raise ValueError(f"{label} required: an audit document or a path to its JSON file")


def protects_scan_input(value: Any, targets: list[Path]) -> bool:
    """A report destination cannot replace its source scan, including an alias."""
    return is_sqlite_input(value) and any(
        target.exists() and os.path.samefile(value, target) for target in targets
    )
