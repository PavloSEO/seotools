from __future__ import annotations

from pathlib import Path

import pytest

from seohead.crawl.sitemap_capture import SourceRoot, capture_declared_roots
from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
from seohead.storage.native_scan import NativeScan
from seohead.tools import sitemap
from tests.test_scan_native import _metadata

NATIVE_ROOT = "https://example.test/sitemap.xml"
NATIVE_URLS = ("https://example.test/a", "https://example.test/b")


def test_shorter_partial_replay_never_finishes_root_complete():
    finished = []
    members = []

    def crawl_fn(_url, *, concurrency, sink):
        sink({"loc": "https://example.test/a"})
        return {
            "ok": True,
            "root": "https://example.test/root.xml",
            "count": 1,
            "errors": [],
            "truncated": False,
        }

    summary = capture_declared_roots(
        [SourceRoot(7, "https://example.test/root.xml", "explicit")],
        write_sitemap_members=lambda sitemap_id, rows: members.append((sitemap_id, rows)),
        finish_sitemap=lambda sitemap_id, complete, reason: finished.append(
            (sitemap_id, complete, reason)
        ),
        emit_seed=lambda *_args: None,
        read_sitemap_summary=lambda _sid: {"complete": False, "reason": "interrupted"},
        read_sitemap_members=lambda _sid: iter(
            [(0, "https://example.test/a"), (1, "https://example.test/stale")]
        ),
        crawl_fn=crawl_fn,
    )
    assert summary[0].complete is False
    assert summary[0].reason == "sitemap replay ended before saved membership prefix"
    assert finished == [
        (7, False, "capture in progress"),
        (7, False, "sitemap replay ended before saved membership prefix"),
    ]
    assert members == [(7, [(0, "https://example.test/a")])]


def test_streaming_deduper_closes_temporary_store_when_sink_raises():
    deduper = sitemap._StreamingDeduper(lambda _entry: (_ for _ in ()).throw(RuntimeError("sink")))
    temporary = Path(deduper._temporary.name)
    with pytest.raises(RuntimeError, match="sink"):
        deduper.add({"loc": "https://example.test/a"}, "https://example.test/a")
    assert not temporary.exists()


def test_incomplete_root_stops_later_selected_root_capture():
    fetched = []
    finished = []

    def crawl_fn(url, *, concurrency, sink):
        fetched.append(url)
        return {
            "ok": True,
            "root": url,
            "count": 0,
            "errors": [{"error": "HTTP 500"}],
            "truncated": False,
        }

    result = capture_declared_roots(
        [
            SourceRoot(1, "https://example.test/a.xml", "explicit"),
            SourceRoot(2, "https://example.test/b.xml", "robots"),
        ],
        write_sitemap_members=lambda *_args: None,
        finish_sitemap=lambda sitemap_id, complete, reason: finished.append(
            (sitemap_id, complete, reason)
        ),
        emit_seed=lambda *_args: None,
        crawl_fn=crawl_fn,
    )
    assert fetched == ["https://example.test/a.xml"]
    assert [summary.sitemap_id for summary in result] == [1]
    assert [row[0] for row in finished] == [1, 1]


def _native_root(scan):
    roots = scan.sitemap_roots()
    assert len(roots) == 1 and roots[0]["url"] == NATIVE_ROOT
    return roots[0]


def _native_capture(scan, crawl_fn, emitted):
    root = _native_root(scan)
    return capture_declared_roots(
        [SourceRoot(root["sitemap_url_id"], root["url"], root["source"])],
        write_sitemap_members=scan.write_sitemap_members,
        finish_sitemap=scan.finish_sitemap,
        emit_seed=lambda url, _root: emitted.append(url),
        read_sitemap_summary=lambda sid: scan.read_context("sitemap_fetch_summary", f"url:{sid}"),
        read_sitemap_members=scan.iter_sitemap_members,
        crawl_fn=crawl_fn,
    )


def test_selected_native_root_is_durable_before_any_expansion(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(
        path, initial_sitemaps=((NATIVE_ROOT, "explicit"),), **_metadata()
    ) as scan:
        root = _native_root(scan)
        assert root["source"] == "explicit"
        assert scan.read_context("sitemap_fetch_summary", f"url:{root['sitemap_url_id']}") is None


def test_interrupted_native_prefix_replays_members_and_seeds_once(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(
        path, initial_sitemaps=((NATIVE_ROOT, "explicit"),), **_metadata()
    ) as scan:
        root = _native_root(scan)
        scan.finish_sitemap(root["sitemap_url_id"], False, "capture interrupted")
        scan.write_sitemap_members(root["sitemap_url_id"], [(0, NATIVE_URLS[0])])
        scan.interrupt("capture interrupted")

    emitted: list[str] = []

    def crawl_fn(_url, *, concurrency, sink):
        assert concurrency == 3
        for loc in NATIVE_URLS:
            sink({"loc": loc})
        return {
            "ok": True,
            "root": NATIVE_ROOT,
            "count": 2,
            "errors": [],
            "truncated": False,
        }

    with NativeScan.open(path) as scan:
        summaries = _native_capture(scan, crawl_fn, emitted)
        root = _native_root(scan)
        assert list(scan.iter_sitemap_members(root["sitemap_url_id"])) == list(
            enumerate(NATIVE_URLS)
        )
        assert summaries[0].complete is True
        assert emitted == list(NATIVE_URLS)
        scan.seed_frontier(
            [
                {
                    "requested_url": url,
                    "frontier_url": url,
                    "depth": 0,
                    "reason": "",
                    "source": "sitemap",
                    "reserve_query": True,
                    "seed": True,
                }
                for url in emitted
            ]
        )
        assert scan.con.execute("SELECT COUNT(*) FROM frontier").fetchone()[0] == len(NATIVE_URLS)


def test_complete_native_root_reuses_members_without_fetch(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(
        path, initial_sitemaps=((NATIVE_ROOT, "explicit"),), **_metadata()
    ) as scan:
        root = _native_root(scan)
        scan.write_sitemap_members(root["sitemap_url_id"], list(enumerate(NATIVE_URLS)))
        scan.finish_sitemap(root["sitemap_url_id"], True, "")
        emitted: list[str] = []
        summaries = _native_capture(
            scan,
            lambda *_args, **_kwargs: pytest.fail("complete root fetched again"),
            emitted,
        )
        assert emitted == list(NATIVE_URLS)
        assert summaries[0].complete is True and summaries[0].count == len(NATIVE_URLS)


def test_changed_native_prefix_is_refused_not_rewritten(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(
        path, initial_sitemaps=((NATIVE_ROOT, "explicit"),), **_metadata()
    ) as scan:
        root = _native_root(scan)
        scan.finish_sitemap(root["sitemap_url_id"], False, "capture interrupted")
        scan.write_sitemap_members(root["sitemap_url_id"], [(0, NATIVE_URLS[0])])

        def changed_prefix(_url, *, concurrency, sink):
            sink({"loc": NATIVE_URLS[1]})
            return {
                "ok": True,
                "root": NATIVE_ROOT,
                "count": 1,
                "errors": [],
                "truncated": False,
            }

        with pytest.raises(ValueError, match="prefix conflicts"):
            _native_capture(scan, changed_prefix, [])


def test_empty_complete_root_differs_from_missing_and_partial_context(tmp_path):
    complete_path = tmp_path / "complete.sqlite"
    missing_path = tmp_path / "missing.sqlite"
    partial_path = tmp_path / "partial.sqlite"
    with NativeScan.create(
        complete_path, initial_sitemaps=((NATIVE_ROOT, "explicit"),), **_metadata()
    ) as scan:
        root = _native_root(scan)
        scan.finish_sitemap(root["sitemap_url_id"], True, "")
        with prepare_sitemap_reconciliation(scan.con, start_url="https://example.test/") as result:
            assert result.available is True and result.counts["urls_in_sitemap"] == 0
    with (
        NativeScan.create(
            missing_path, initial_sitemaps=((NATIVE_ROOT, "explicit"),), **_metadata()
        ) as scan,
        prepare_sitemap_reconciliation(scan.con, start_url="https://example.test/") as result,
    ):
        assert result.available is False and "partial" in result.reason
    with NativeScan.create(
        partial_path, initial_sitemaps=((NATIVE_ROOT, "explicit"),), **_metadata()
    ) as scan:
        root = _native_root(scan)
        scan.finish_sitemap(root["sitemap_url_id"], False, "network interrupted")
        scan.begin_collection()
        summary = scan.read_context("sitemap_fetch_summary", f"url:{root['sitemap_url_id']}")
        assert summary["complete"] is False
