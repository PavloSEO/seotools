#!/usr/bin/env python3
# ruff: noqa: E402
"""Profile bounded SQLite collection on a synthetic, network-free site.

The parent starts one fresh child process per edge density.  Each child uses the
real adapter, parser and NativeScan writer, but its injected transport never
opens a socket.  Output is JSON so the acceptance record can retain environment
and measurement units alongside the result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seohead.crawl.settings import load
from seohead.crawl.sqlite_adapter import crawl_to_scan

PAGES = 10_000
HOST = "example.test"
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
SOURCES = (
    "scripts/profile_scan_collector.py",
    "seohead/crawl/sqlite_adapter.py",
    "seohead/crawl/collect.py",
    "seohead/crawl/spider.py",
    "seohead/crawl/throttle.py",
    "seohead/tools/parser.py",
    "seohead/storage/frontier.py",
    "seohead/storage/native_scan.py",
)


class Response:
    def __init__(self, status_code: int, text: str, content_type: str = "text/html") -> None:
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type}


def page_html(page: int, edges: int) -> str:
    links = "".join(
        f'<a href="/p/{(page + offset) % PAGES}">x</a>' for offset in range(1, edges + 1)
    )
    return f"<html><head><title>p{page}</title></head><body>{links}</body></html>"


def offline_fetcher(edges: int) -> tuple[Callable[[str], Response], Callable[[], int]]:
    """Return the bounded synthetic transport shared by collection profiles.

    The crawler receives no real client when this fetcher is present, so the
    fixture cannot make a socket, DNS, HTTP, or browser request.  Its counter
    intentionally excludes robots.txt; it records the number of synthetic page
    documents that the collector actually requested.
    """
    fetched = 0

    def fetch(url: str) -> Response:
        nonlocal fetched
        if url.endswith("/robots.txt"):
            return Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n", "text/plain")
        prefix = f"https://{HOST}/p/"
        if not url.startswith(prefix):
            return Response(404, "not found", "text/plain")
        try:
            page = int(url.removeprefix(prefix))
        except ValueError:
            return Response(404, "not found", "text/plain")
        if not 0 <= page < PAGES:
            return Response(404, "not found", "text/plain")
        fetched += 1
        if fetched % 1_000 == 0:
            print(f"progress pages={fetched} edges_per_page={edges}", file=sys.stderr, flush=True)
        return Response(200, page_html(page, edges))

    return fetch, lambda: fetched


def peak_rss() -> tuple[float, str]:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return raw / (1024 * 1024), "bytes"
    return raw / 1024, "KiB"


def link_digest(database: Path) -> str:
    digest = hashlib.sha256()
    con = sqlite3.connect(database)
    try:
        cursor = con.execute(
            "SELECT source.url, destination.url, l.anchor, l.nofollow, l.position, l.rel_json, l.target, l.raw_href "
            "FROM links AS l "
            "JOIN urls AS source ON source.url_id=l.source_url_id "
            "JOIN urls AS destination ON destination.url_id=l.destination_url_id "
            "ORDER BY l.link_id"
        )
        for row in cursor:
            digest.update(
                json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            )
            digest.update(b"\n")
    finally:
        con.close()
    return digest.hexdigest()


def validated_source_revision(value: object) -> str:
    """Require an actual Git revision before a profile creates an artifact."""
    if not isinstance(value, str) or not _REVISION.fullmatch(value):
        raise RuntimeError("profile source revision must be a full lowercase Git HEAD SHA")
    return value


def source_manifest(files: tuple[str, ...] = SOURCES) -> dict[str, object]:
    """Identify profile code without claiming a clean checkout that is dirty."""
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "profile source revision is unavailable; run from a Git checkout"
        ) from exc
    if revision.returncode or dirty.returncode:
        raise RuntimeError(
            "profile source revision is unavailable; Git could not inspect this checkout"
        )
    return {
        "source_revision": validated_source_revision(revision.stdout.strip()),
        "source_dirty": bool(dirty.stdout),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files
        },
    }


def run_child(edges: int, database: Path) -> dict[str, object]:
    fetcher, fetched_pages = offline_fetcher(edges)
    provenance = source_manifest()

    settings = load(
        overrides={
            "speed.min_delay_seconds": 0,
            "speed.concurrency": 1,
            "limits.max_urls": PAGES,
            "limits.max_depth": PAGES,
            "link_attributes.capture": False,
            "robots.policy": "ignore",
        }
    )
    started = time.perf_counter()
    result = crawl_to_scan(
        f"https://{HOST}/p/0",
        scan_out=str(database),
        settings=settings,
        producer_version="profile",
        producer_revision=validated_source_revision(provenance["source_revision"]),
        runtime_versions={
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "httpx": "profile-injected",
            "lxml": "profile-installed",
            "beautifulsoup4": "profile-installed",
        },
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )
    elapsed = time.perf_counter() - started
    con = sqlite3.connect(database)
    try:
        pages = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        links = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    finally:
        con.close()
    rss_mib, rss_unit = peak_rss()
    return {
        "pages_requested": PAGES,
        "edges_per_page": edges,
        "pages": pages,
        "links": links,
        "link_digest": link_digest(database),
        "peak_rss_mib": round(rss_mib, 2),
        "rss_source_unit": rss_unit,
        "wall_seconds": round(elapsed, 3),
        "fetched_pages": fetched_pages(),
        "collector_lifecycle": result.lifecycle,
        "collector_finish_reason": result.finish_reason,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        **provenance,
    }


def main() -> None:
    global PAGES
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--edges", type=int, choices=(30, 150))
    parser.add_argument("--database", type=Path)
    parser.add_argument("--pages", type=int, default=PAGES)
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be positive")
    PAGES = args.pages
    if args.child:
        if args.edges is None or args.database is None:
            parser.error("--child requires --edges and --database")
        print(json.dumps(run_child(args.edges, args.database), sort_keys=True))
        return

    results = []
    with tempfile.TemporaryDirectory(prefix="seohead-scan-profile-") as temporary:
        root = Path(temporary)
        for edges in (30, 150):
            database = root / f"{edges}.sqlite"
            completed = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--child",
                    "--edges",
                    str(edges),
                    "--pages",
                    str(PAGES),
                    "--database",
                    str(database),
                ],
                check=True,
                text=True,
                capture_output=True,
            )
            sys.stderr.write(completed.stderr)
            results.append(json.loads(completed.stdout))
    results.sort(key=lambda item: item["edges_per_page"])
    delta = results[1]["peak_rss_mib"] - results[0]["peak_rss_mib"]
    print(
        json.dumps(
            {
                "fixture": {
                    "host": HOST,
                    "network": "injected transport",
                    "page_html": "generated tiny HTML",
                },
                "results": results,
                "rss_delta_mib": round(delta, 2),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
