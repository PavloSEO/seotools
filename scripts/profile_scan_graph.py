#!/usr/bin/env python3
# ruff: noqa: E402
"""Subprocess RSS profile for E SQL graph and sitemap cursor readers."""

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

from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import fingerprint, load
from seohead.crawl.sql_graph import StoredGraph
from seohead.crawl.sql_sitemap import open_sitemap_reconciliation
from seohead.storage import open_scan
from seohead.storage.native_scan import NativeScan

PAGES = 10_000
HOST = "example.test"
SOURCES = (
    "seohead/crawl/sql_graph.py",
    "seohead/crawl/sql_sitemap.py",
    "seohead/storage/__init__.py",
    "seohead/storage/native_scan.py",
    "seohead/storage/sitemaps.py",
    "scripts/profile_scan_graph.py",
)


def _rss() -> tuple[float, str]:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return (raw / (1024 * 1024), "bytes") if platform.system() == "Darwin" else (raw / 1024, "KiB")


def _environment() -> dict[str, object]:
    return {
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "source_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES
        },
    }


def _metadata() -> dict[str, object]:
    config = load(
        overrides={
            "speed.min_delay_seconds": 0,
            "speed.concurrency": 1,
            "limits.max_urls": PAGES,
            "limits.max_depth": PAGES,
            "link_position.classify": True,
            "robots.policy": "ignore",
        }
    )
    return {
        "start_url": f"https://{HOST}/p/0",
        "config": config,
        "config_fingerprint": fingerprint(config),
        "writer_version": "profile",
        "writer_revision": "0" * 40,
        "runtime_versions": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "httpx": "profile",
            "lxml": "profile",
            "beautifulsoup4": "profile",
        },
    }


def _runtime(depth: int) -> dict[str, object]:
    return {
        "max_depth_reached": depth,
        "elapsed_seconds": 0.0,
        "circuit_timeout_streak": 0,
        "circuit_server_error_streak": 0,
        "crawl_delay_applied": None,
        "throttle": {"delay_seconds": 0.0, "concurrency": 1, "consecutive_ok": 0},
    }


def _url(page: int) -> str:
    return f"https://{HOST}/p/{page}"


def _links(page: int, edges: int) -> list[dict[str, object]]:
    return [
        {
            "source": _url(page),
            "destination": _url((page + offset) % PAGES),
            "anchor": "x",
            "nofollow": False,
            "position": "content" if offset % 2 else "footer",
            "rel": (),
            "target": "",
            "raw_href": "",
        }
        for offset in range(1, edges + 1)
    ]


def _build(edges: int, database: Path) -> dict[str, object]:
    started = time.perf_counter()
    with NativeScan.create(
        database, **_metadata(), initial_sitemaps=[(f"https://{HOST}/sitemap.xml", "explicit")]
    ) as scan:
        scan.enqueue([(_url(page), page) for page in range(PAGES)])
        for page in range(PAGES):
            lease = scan.claim(1)[0]
            scan.commit_page(
                lease,
                vars(
                    PageRecord(
                        url=lease.url, status_code=200, content_type="text/html", crawl_depth=page
                    )
                ),
                links=_links(page, edges),
                runtime=_runtime(page),
            )
            if (page + 1) % 1_000 == 0:
                print(
                    f"progress pages={page + 1} edges_per_page={edges}", file=sys.stderr, flush=True
                )
        sid = scan.sitemap_roots()[0]["sitemap_url_id"]
        for start in range(0, PAGES, 256):
            scan.write_sitemap_members(
                sid, ((page, _url(page)) for page in range(start, min(start + 256, PAGES)))
            )
        scan.finish_sitemap(sid, True, "")
    rss, unit = _rss()
    return {
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
        **_environment(),
    }


def _expected_graph_digest(edges: int) -> str:
    digest = hashlib.sha256()
    content = edges // 2
    footer = edges - content
    for page in sorted(range(PAGES), key=lambda value: _url(value)):
        row = {
            "url": _url(page),
            "inlinks_total": edges,
            "by_position": {"content": content, "footer": footer},
            "boilerplate_only": False,
        }
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def _graph(database: Path, edges: int) -> dict[str, object]:
    started = time.perf_counter()
    digest = hashlib.sha256()
    rows = 0
    con = open_scan(database, require_audit=False)
    try:
        with StoredGraph(con) as graph:
            metadata = graph.composition_metadata().as_dict()
            for row in graph.iter_composition_rows():
                digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
                digest.update(b"\n")
                rows += 1
    finally:
        con.close()
    expected = _expected_graph_digest(edges)
    if (
        rows != PAGES
        or metadata["edges_classified"] != PAGES * edges
        or metadata["edges_unclassified"] != 0
        or digest.hexdigest() != expected
    ):
        raise RuntimeError("SQL graph result differs from the classified fixture contract")
    rss, unit = _rss()
    return {
        "composition_rows": rows,
        "composition_digest": digest.hexdigest(),
        "expected_digest": expected,
        "metadata": metadata,
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
        **_environment(),
    }


def _sitemap(database: Path) -> dict[str, object]:
    started = time.perf_counter()
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    with open_sitemap_reconciliation(database, start_url=_url(0)) as result:
        summary = result.summary()
        for name in (
            "in_sitemap_and_linked",
            "in_sitemap_not_linked",
            "linked_not_in_sitemap",
            "linked_not_comparable",
        ):
            for value in result.iter_bucket(name):
                digest.update(name.encode())
                digest.update(b"\0")
                digest.update(value.encode())
                digest.update(b"\n")
                counts[name] = counts.get(name, 0) + 1
    if (
        not summary["available"]
        or summary["urls_in_sitemap"] != PAGES
        or counts != {"in_sitemap_and_linked": PAGES}
    ):
        raise RuntimeError("SQL sitemap result differs from the complete selected-root fixture")
    rss, unit = _rss()
    return {
        "summary": summary,
        "bucket_counts": counts,
        "bucket_digest": digest.hexdigest(),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "peak_rss_mib": round(rss, 2),
        "rss_source_unit": unit,
        **_environment(),
    }


def _child(kind: str, edges: int, database: Path) -> dict[str, object]:
    return (
        {"build": _build(edges, database)}
        if kind == "build"
        else (
            {"graph": _graph(database, edges)}
            if kind == "graph"
            else {"sitemap": _sitemap(database)}
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--kind", choices=("build", "graph", "sitemap"))
    parser.add_argument("--edges", type=int, choices=(30, 150))
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    if args.kind:
        if args.edges is None or args.database is None:
            parser.error("--kind requires --edges and --database")
        print(json.dumps(_child(args.kind, args.edges, args.database), sort_keys=True))
        return
    if not args.profile:
        parser.error("--profile is required")
    results = []
    with tempfile.TemporaryDirectory(prefix="seohead-graph-profile-") as directory:
        for edges in (30, 150):
            database = Path(directory) / f"{edges}.sqlite"
            outcome: dict[str, object] = {"edges_per_page": edges}
            for kind in ("build", "graph", "sitemap"):
                child = subprocess.run(
                    [
                        sys.executable,
                        __file__,
                        "--kind",
                        kind,
                        "--edges",
                        str(edges),
                        "--database",
                        str(database),
                    ],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                )
                outcome.update(json.loads(child.stdout))
            results.append(outcome)
    graph_delta = round(
        float(results[1]["graph"]["peak_rss_mib"]) - float(results[0]["graph"]["peak_rss_mib"]),
        2,
    )
    sitemap_delta = round(
        float(results[1]["sitemap"]["peak_rss_mib"]) - float(results[0]["sitemap"]["peak_rss_mib"]),
        2,
    )
    if graph_delta > 128 or sitemap_delta > 128:
        raise RuntimeError("SQL reader RSS growth exceeds the 128 MiB fixed-page profile limit")
    if results[0]["sitemap"]["bucket_digest"] != results[1]["sitemap"]["bucket_digest"]:
        raise RuntimeError("sitemap cursor digest differs across link densities")
    print(
        json.dumps(
            {
                "fixture": {
                    "pages": PAGES,
                    "links": [PAGES * 30, PAGES * 150],
                    "sitemap_members": PAGES,
                    "positions": ["content", "footer"],
                },
                "results": results,
                "graph_reader_rss_delta_mib": graph_delta,
                "sitemap_reader_rss_delta_mib": sitemap_delta,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
