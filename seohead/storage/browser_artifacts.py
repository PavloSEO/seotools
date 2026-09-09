"""Bounded, restricted browser sidecars referenced from native scan context."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from pathlib import Path
from typing import Any

from . import ScanError

KIND = "browser_artifacts"
VERSION = "browser_artifacts.v1"
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_SCREENSHOT_WIDTH = 4_096
MAX_SCREENSHOT_HEIGHT = 30_000
MAX_SCREENSHOT_PIXELS = 80_000_000
MAX_CONSOLE_ERRORS = 100
MAX_CONSOLE_CHARS = 1_000
_SECRET = re.compile(r"(?i)(?:authorization|token|secret|password|cookie)\s*[:=]\s*[^\s,;]+")
_URL = re.compile(r"https?://[^\s'\"]+")


def staging_dir(scan_path: str | Path) -> Path:
    return Path(scan_path).with_name(Path(scan_path).name + ".browser-staging")


def _root(scan_path: str | Path) -> Path:
    return Path(scan_path).with_name(Path(scan_path).name + ".browser-artifacts")


def _restricted_directory(scan_path: str | Path, path: Path) -> None:
    root = _root(scan_path)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _redact(value: Any) -> str:
    text = str(value or "")[:MAX_CONSOLE_CHARS]
    return _URL.sub("[url]", _SECRET.sub("[redacted]", text))


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ScanError("browser screenshot is not a PNG")
    width, height = struct.unpack(">II", header[16:24])
    if (
        not width
        or not height
        or width > MAX_SCREENSHOT_WIDTH
        or height > MAX_SCREENSHOT_HEIGHT
        or width * height > MAX_SCREENSHOT_PIXELS
    ):
        raise ScanError("browser screenshot dimensions exceed the retention bound")
    return width, height


def _move_screenshot(scan_path: str | Path, source: str | None) -> dict[str, Any]:
    if not source:
        return {"state": "unavailable", "reason": "renderer produced no screenshot", "ref": None}
    staged = staging_dir(scan_path).resolve()
    path = Path(source)
    try:
        resolved = path.resolve(strict=True)
        if path.is_symlink() or staged not in resolved.parents or not resolved.is_file():
            raise ScanError("browser screenshot is outside the restricted staging directory")
        size = resolved.stat().st_size
        if not 1 <= size <= MAX_SCREENSHOT_BYTES:
            raise ScanError("browser screenshot exceeds the byte retention bound")
        width, height = _png_dimensions(resolved)
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        target = _root(scan_path) / "screenshots" / f"{digest}.png"
        _restricted_directory(scan_path, target.parent)
        if target.exists():
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ScanError("browser screenshot destination conflicts with different bytes")
            resolved.unlink()
        else:
            os.replace(resolved, target)
            os.chmod(target, 0o600)
        return {
            "state": "stored",
            "reason": "",
            "ref": {"sha256": digest, "bytes": size, "width": width, "height": height},
        }
    except (OSError, ScanError) as exc:
        return {"state": "unavailable", "reason": _redact(exc), "ref": None}


def _save_console(scan_path: str | Path, errors: Any, omitted: Any) -> dict[str, Any]:
    values = [_redact(value) for value in errors] if isinstance(errors, list) else []
    values = values[:MAX_CONSOLE_ERRORS]
    omitted_count = int(omitted) if type(omitted) is int and omitted >= 0 else 0
    body = json.dumps({"schema_version": "browser_console.v1", "errors": values}, separators=(",", ":"))
    digest = hashlib.sha256(body.encode()).hexdigest()
    target = _root(scan_path) / "console" / f"{digest}.json"
    try:
        _restricted_directory(scan_path, target.parent)
        if target.exists():
            if target.is_symlink() or target.read_text(encoding="utf-8") != body:
                raise ScanError("browser console destination conflicts with different content")
        else:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
        return {
            "state": "partial" if omitted_count else "stored",
            "reason": "console error count exceeded the retention bound" if omitted_count else "",
            "ref": {"sha256": digest, "captured": len(values), "omitted": omitted_count},
        }
    except (OSError, ScanError) as exc:
        return {"state": "unavailable", "reason": _redact(exc), "ref": None}


def save(
    scan_path: str | Path,
    page_url_id: int,
    rendered: dict[str, Any],
    *,
    screenshots: bool,
    console_errors: bool,
) -> dict[str, Any]:
    """Persist opt-in browser sidecars and return one portable native context item."""
    if type(page_url_id) is not int or page_url_id < 1 or not isinstance(rendered, dict):
        raise ScanError("browser artifact input is invalid")
    screenshot = (
        _move_screenshot(scan_path, rendered.get("screenshot_path"))
        if screenshots
        else {"state": "disabled", "reason": "screenshot retention disabled", "ref": None}
    )
    console = (
        _save_console(scan_path, rendered.get("console_errors"), rendered.get("console_errors_omitted"))
        if console_errors
        else {"state": "disabled", "reason": "console retention disabled", "ref": None}
    )
    states = {screenshot["state"], console["state"]}
    completeness = "complete" if states <= {"stored", "disabled"} else "partial" if states & {"stored", "partial"} else "unavailable"
    reasons = "; ".join(item["reason"] for item in (screenshot, console) if item["reason"])
    payload = {"schema_version": VERSION, "page_url_id": page_url_id, "screenshot": screenshot, "console": console}
    return {
        "kind": KIND,
        "item_key": f"page:{page_url_id}",
        "payload_version": "scan_context.v1",
        "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        "completeness": completeness,
        "reason": reasons,
    }


def validate_context(con: Any, item: dict[str, Any], payload: Any) -> None:
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "page_url_id", "screenshot", "console"}
        or payload["schema_version"] != VERSION
        or type(payload["page_url_id"]) is not int
        or payload["page_url_id"] < 1
        or item["item_key"] != f"page:{payload['page_url_id']}"
        or not con.execute("SELECT 1 FROM pages WHERE url_id=?", (payload["page_url_id"],)).fetchone()
    ):
        raise ScanError("browser artifact context is invalid")
    for value in (payload["screenshot"], payload["console"]):
        if (
            not isinstance(value, dict)
            or set(value) != {"state", "reason", "ref"}
            or value["state"] not in {"disabled", "stored", "partial", "unavailable"}
            or not isinstance(value["reason"], str)
        ):
            raise ScanError("browser artifact context has an invalid retention state")
    screenshot = payload["screenshot"]
    if screenshot["state"] == "stored":
        ref = screenshot["ref"]
        if (
            not isinstance(ref, dict)
            or set(ref) != {"sha256", "bytes", "width", "height"}
            or not isinstance(ref["sha256"], str)
            or len(ref["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in ref["sha256"])
            or any(type(ref[key]) is not int or ref[key] < 1 for key in ("bytes", "width", "height"))
        ):
            raise ScanError("browser screenshot reference is invalid")
    elif screenshot["ref"] is not None:
        raise ScanError("unavailable browser screenshot must not carry a reference")
    console = payload["console"]
    if console["state"] in {"stored", "partial"}:
        ref = console["ref"]
        if (
            not isinstance(ref, dict)
            or set(ref) != {"sha256", "captured", "omitted"}
            or not isinstance(ref["sha256"], str)
            or len(ref["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in ref["sha256"])
            or any(type(ref[key]) is not int or ref[key] < 0 for key in ("captured", "omitted"))
        ):
            raise ScanError("browser console reference is invalid")
    elif console["ref"] is not None:
        raise ScanError("unavailable browser console must not carry a reference")
