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
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seohead.crawl.settings import load
from seohead.crawl.sqlite_adapter import crawl_to_scan

PAGES = 10_000
HOST = "example.test"


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


def source_hashes() -> dict[str, str]:
    files = (
        "seohead/crawl/sqlite_adapter.py",
        "seohead/crawl/collect.py",
        "seohead/crawl/spider.py",
        "seohead/crawl/throttle.py",
        "seohead/tools/parser.py",
        "seohead/storage/frontier.py",
        "seohead/storage/native_scan.py",
    )
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files}


def run_child(edges: int, database: Path) -> dict[str, object]:
    fetched = 0

    def fetcher(url: str) -> Response:
        nonlocal fetched
        if url.endswith("/robots.txt"):
            return Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n", "text/plain")
        fetched += 1
        if fetched % 1_000 == 0:
            print(f"progress pages={fetched} edges_per_page={edges}", file=sys.stderr, flush=True)
        page = int(url.rsplit("/", 1)[-1]) if "/p/" in url else 0
        return Response(200, page_html(page, edges))

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
        producer_revision="0" * 40,
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
        "fetched_pages": fetched,
        "collector_lifecycle": result.lifecycle,
        "collector_finish_reason": result.finish_reason,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "source_sha256": source_hashes(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--edges", type=int, choices=(30, 150))
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
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
