#!/usr/bin/env python3
"""Offline, bounded capacity stages using the real native scan writer.

Run each stage in a fresh process to obtain a meaningful peak-RSS observation.
The crawler's configured URL ceiling is deliberately left unchanged: a refused
100k/1M request is a measured admission failure, not a storage success.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from seohead.crawl.capture import CaptureEvent
from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import ConfigError, fingerprint, load
from seohead.storage import open_scan
from seohead.storage.history import inspect_scan, snapshot_scan
from seohead.storage.native_scan import NativeScan

HOST = "example.test"
ROOT = Path(__file__).resolve().parents[1]


def _windows_memory_counters() -> tuple[int, int]:
    """Return total physical bytes and process peak working-set bytes on Windows."""
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    memory = MemoryStatus()
    memory.dwLength = ctypes.sizeof(memory)
    if not ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx(
        ctypes.byref(memory)
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    process = kernel32.GetCurrentProcess()
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    if not psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), ctypes.sizeof(counters)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(memory.ullTotalPhys), int(counters.PeakWorkingSetSize)


def _environment() -> dict[str, object]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    if sys.platform == "darwin":
        memory_bytes = int(
            subprocess.run(
                ["sysctl", "-n", "hw.memsize"], text=True, capture_output=True, check=True
            ).stdout
        )
    elif os.name == "nt":
        memory_bytes, _peak_rss_bytes = _windows_memory_counters()
    else:
        memory_bytes = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    return {
        "source_revision": revision,
        "source_dirty": bool(dirty),
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "physical_memory_bytes": memory_bytes,
        "rss_source_unit": "bytes" if sys.platform == "darwin" or os.name == "nt" else "KiB",
    }


def _rss_mib() -> float:
    if sys.platform == "darwin":
        from resource import RUSAGE_SELF, getrusage

        return round(getrusage(RUSAGE_SELF).ru_maxrss / (1024 * 1024), 2)
    if os.name == "nt":
        _memory_bytes, peak_rss_bytes = _windows_memory_counters()
        return round(peak_rss_bytes / (1024 * 1024), 2)
    from resource import RUSAGE_SELF, getrusage

    return round(getrusage(RUSAGE_SELF).ru_maxrss / 1024, 2)


def _sizes(path: Path) -> dict[str, int]:
    return {
        name: candidate.stat().st_size if candidate.exists() else 0
        for name, candidate in (
            ("database", path),
            ("wal", Path(str(path) + "-wal")),
            ("shm", Path(str(path) + "-shm")),
        )
    }


def _url(page: int) -> str:
    return f"https://{HOST}/p/{page}"


def _html(page: int, size: int) -> str:
    if size == 0:
        return ""
    prefix, suffix = "<html><body>", "</body></html>"
    if size < len(prefix) + len(suffix):
        raise ValueError("body/DOM size must be zero or at least 26 bytes")
    remaining = size - len(prefix) - len(suffix)
    chunks = []
    while remaining:
        digest = hashlib.shake_256(f"{page}:{len(chunks)}".encode()).hexdigest(
            min(remaining // 2 + 1, 256)
        )
        chunk = digest[:remaining]
        chunks.append(chunk)
        remaining -= len(chunk)
    return prefix + "".join(chunks) + suffix


def _capture(url: str, html: str) -> CaptureEvent:
    return CaptureEvent(
        method="GET",
        requested_url=url,
        effective_url=url,
        redirect_history=(),
        requested_at="2026-10-03T00:00:00Z",
        received_at="2026-10-03T00:00:01Z",
        status_code=200,
        request_headers=(),
        credentials_used=False,
        response_headers=(("content-type", "text/html; charset=utf-8"),),
        content_type="text/html; charset=utf-8",
        content_encoding="",
        entity_bytes=html.encode(),
        body_fidelity="entity_bytes",
        body_state="complete",
        body_reason="none",
        error="",
        error_kind="",
        effective_status_code=200,
        effective_headers=(("content-type", "text/html; charset=utf-8"),),
    )


def _renderer(url: str) -> dict[str, object]:
    return {
        "engine": "synthetic",
        "engine_version": "profile",
        "settings": {
            "viewport": {"width": 1280, "height": 720},
            "device_pixel_ratio": 1.0,
            "mobile_emulation": False,
            "touch_emulation": False,
            "script_timeout_seconds": 0.0,
            "resize_to_content": False,
            "resize_to_content_max_height_px": 15000,
            "persistent_profile": False,
        },
        "navigation": {
            "requested_url": url,
            "final_url": url,
            "wait_until": "load",
            "timeout_seconds": 30.0,
        },
        "transforms": {
            "flatten_shadow_dom_requested": False,
            "flatten_shadow_dom_applied": 0,
            "flatten_iframes_requested": False,
            "flatten_iframes_applied": 0,
        },
        "policy": {"credentials_used": False, "cache_control_no_store": False},
    }


def _runtime() -> dict[str, object]:
    return {
        "max_depth_reached": 0,
        "elapsed_seconds": 0.0,
        "circuit_timeout_streak": 0,
        "circuit_server_error_streak": 0,
        "crawl_delay_applied": None,
        "throttle": {"delay_seconds": 0.0, "concurrency": 1, "consecutive_ok": 0},
    }


def _config(args: argparse.Namespace) -> dict[str, object]:
    overrides = {
        "speed.min_delay_seconds": 0,
        "speed.concurrency": 1,
        "limits.max_urls": args.pages,
        "limits.max_depth": 0,
        "robots.policy": "ignore",
        "storage.body_mode": "captured_entity_bytes"
        if args.body_bytes or args.dom_bytes
        else "off",
    }
    if args.experimental_synthetic:
        overrides["storage.capacity_profile"] = "experimental_synthetic"
    return load(overrides=overrides)


def _budget(args: argparse.Namespace, started: float, path: Path) -> None:
    if time.perf_counter() - started > args.max_seconds:
        raise TimeoutError("declared stage wall-time budget exceeded")
    if _rss_mib() > args.max_rss_mib:
        raise MemoryError("declared stage peak-RSS budget exceeded")
    if sum(_sizes(path).values()) > args.max_disk_mib * 1024 * 1024:
        raise OSError("declared database/WAL disk budget exceeded")
    if shutil.disk_usage(path.parent).free < args.min_free_mib * 1024 * 1024:
        raise OSError("declared minimum free-disk reserve reached")


def _build(args: argparse.Namespace) -> dict[str, object]:
    started = time.perf_counter()
    path = args.scan.absolute()
    config = _config(args)
    revision = _environment()["source_revision"]
    metadata = {
        "start_url": _url(0),
        "config": config,
        "config_fingerprint": fingerprint(config),
        "writer_version": "capacity-profile",
        "writer_revision": revision,
        "runtime_versions": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "httpx": "synthetic",
            "lxml": "synthetic",
            "beautifulsoup4": "synthetic",
        },
    }
    if path.exists():
        scan = NativeScan.open(
            path,
            expected_start_url=_url(0),
            expected_config=config,
            expected_writer_revision=revision,
        )
        resumed = True
    else:
        scan = NativeScan.create(path, **metadata)
        resumed = False
    with scan:
        peak_sizes = _sizes(path)
        if resumed:
            scan.recover_inflight()
        else:
            for start in range(0, args.pages, 256):
                scan.enqueue((_url(page), 0) for page in range(start, min(start + 256, args.pages)))
                _budget(args, started, path)
                for name, size in _sizes(path).items():
                    peak_sizes[name] = max(peak_sizes[name], size)
        before = scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        for page in range(before, args.pages):
            lease = scan.claim(1)[0]
            record = vars(
                PageRecord(url=lease.url, status_code=200, content_type="text/html", crawl_depth=0)
            )
            links = [
                {
                    "source": lease.url,
                    "destination": _url((page + offset + 1) % args.pages),
                    "anchor": "x",
                    "nofollow": False,
                    "position": "content",
                    "rel": (),
                    "target": "",
                    "raw_href": "",
                }
                for offset in range(args.links_per_page)
            ]
            body = _html(page, args.body_bytes)
            scan.commit_page(
                lease,
                record,
                links=links,
                runtime=_runtime(),
                captures=[_capture(lease.url, body)] if body else (),
            )
            if args.dom_bytes:
                rendered = vars(
                    PageRecord(
                        url=lease.url,
                        status_code=200,
                        content_type="text/html",
                        crawl_depth=0,
                        representation="rendered",
                    )
                )
                scan.commit_render(
                    lease.url,
                    rendered,
                    html=_html(page, args.dom_bytes),
                    renderer=_renderer(lease.url),
                    captured_at="2026-10-03T00:00:02Z",
                )
            if (page + 1) % 256 == 0 or page + 1 == args.pages:
                _budget(args, started, path)
                for name, size in _sizes(path).items():
                    peak_sizes[name] = max(peak_sizes[name], size)
                print(f"progress pages={page + 1}/{args.pages}", file=sys.stderr, flush=True)
            if args.interrupt_after == page + 1:
                checkpoint = {
                    "pages": page + 1,
                    "wall_seconds": round(time.perf_counter() - started, 3),
                    "peak_rss_mib": _rss_mib(),
                    "disk_bytes": _sizes(path),
                }
                path.with_suffix(".checkpoint.json").write_text(
                    json.dumps(checkpoint, sort_keys=True)
                )
                print(f"intentional_process_exit pages={page + 1}", file=sys.stderr, flush=True)
                os._exit(75)
        finished = scan.finish_capture("synthetic_profile_complete")
    return {
        "stage": "build",
        "requested_pages": args.pages,
        "pages_before": before,
        "pages_after": args.pages,
        "resumed": resumed,
        "finished": finished,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": _rss_mib(),
        "disk_bytes": _sizes(path),
        "peak_disk_bytes_sampled": peak_sizes,
    }


def _read(args: argparse.Namespace) -> dict[str, object]:
    started = time.perf_counter()
    count = 0
    digest = hashlib.sha256()
    with open_scan(args.scan, require_audit=False) as con:
        for (url,) in con.execute(
            "SELECT u.url FROM pages p JOIN urls u USING(url_id) ORDER BY p.page_ordinal"
        ):
            digest.update(url.encode())
            digest.update(b"\n")
            count += 1
    return {
        "stage": "read",
        "pages": count,
        "url_digest": digest.hexdigest(),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": _rss_mib(),
    }


def _inspect(args: argparse.Namespace) -> dict[str, object]:
    started = time.perf_counter()
    result = NativeScan.inspect(args.scan, timeout_seconds=args.max_seconds)
    window = inspect_scan(
        args.scan, table="pages", offset=max(0, result["counts"]["pages"] - 2), limit=2
    )
    return {
        "stage": "inspect",
        "pages": result["counts"]["pages"],
        "lifecycle": result["scan"]["lifecycle"],
        "window_rows": len(window["rows"]),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": _rss_mib(),
    }


def _snapshot(args: argparse.Namespace) -> dict[str, object]:
    started = time.perf_counter()
    target = args.scan.with_name(args.scan.stem + "-snapshot.sqlite")
    snapshot_scan(args.scan, target)
    return {
        "stage": "snapshot",
        "bytes": target.stat().st_size,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": _rss_mib(),
    }


def _integrity(args: argparse.Namespace) -> dict[str, object]:
    started = time.perf_counter()
    con = sqlite3.connect(f"file:{args.scan.absolute()}?mode=ro", uri=True)
    try:
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = con.execute("PRAGMA foreign_key_check").fetchone()
    finally:
        con.close()
    return {
        "stage": "integrity",
        "integrity_check": check,
        "foreign_key_check": "ok" if foreign is None else "failed",
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": _rss_mib(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("build", "read", "inspect", "snapshot", "integrity"))
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--pages", type=int, default=10_000)
    parser.add_argument("--links-per-page", type=int, default=0)
    parser.add_argument("--body-bytes", type=int, default=0)
    parser.add_argument("--dom-bytes", type=int, default=0)
    parser.add_argument("--interrupt-after", type=int, default=0)
    parser.add_argument("--experimental-synthetic", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=900)
    parser.add_argument("--max-rss-mib", type=int, default=2048)
    parser.add_argument("--max-disk-mib", type=int, default=8192)
    parser.add_argument("--min-free-mib", type=int, default=32768)
    args = parser.parse_args()
    if (
        args.pages < 1
        or args.links_per_page < 0
        or args.body_bytes < 0
        or args.dom_bytes < 0
        or (args.dom_bytes and not args.body_bytes)
        or args.interrupt_after < 0
        or args.interrupt_after >= args.pages
        or args.max_seconds <= 0
        or args.max_rss_mib <= 0
        or args.max_disk_mib <= 0
        or args.min_free_mib < 0
    ):
        parser.error("profile counts and declared resource budgets must be valid")
    started = time.perf_counter()
    try:
        result = {
            "build": _build,
            "read": _read,
            "inspect": _inspect,
            "snapshot": _snapshot,
            "integrity": _integrity,
        }[args.stage](args)
    except (ConfigError, ValueError, TimeoutError, MemoryError, OSError) as exc:
        partial: dict[str, object] = {}
        if args.stage == "build" and args.scan.exists():
            try:
                con = sqlite3.connect(f"file:{args.scan.absolute()}?mode=ro", uri=True)
                try:
                    partial["pages_committed"] = con.execute(
                        "SELECT COUNT(*) FROM pages"
                    ).fetchone()[0]
                finally:
                    con.close()
                partial["disk_bytes_at_stop"] = _sizes(args.scan)
                partial["peak_rss_mib"] = _rss_mib()
            except sqlite3.Error:
                partial["inspection"] = "unavailable"
        print(
            json.dumps(
                {
                    "stage": args.stage,
                    "status": "blocked",
                    "reason": str(exc),
                    "requested_pages": args.pages,
                    "wall_seconds": round(time.perf_counter() - started, 3),
                    **partial,
                    "environment": _environment(),
                },
                sort_keys=True,
            )
        )
        return 3
    status = "measured"
    if args.stage == "build" and not result["finished"]:
        status = "blocked"
        result["reason"] = (
            "capture finalization did not complete; retained pages are not a finished scan"
        )
    elif result["wall_seconds"] > args.max_seconds or result["peak_rss_mib"] > args.max_rss_mib:
        status = "blocked"
        result["reason"] = "declared stage time or peak-RSS budget exceeded"
    print(json.dumps({"status": status, "environment": _environment(), **result}, sort_keys=True))
    return 0 if status == "measured" else 3


if __name__ == "__main__":
    raise SystemExit(main())
