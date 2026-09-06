"""Offline SQL sitemap reconciliation for native scan artifacts."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from seohead.crawl.reconcile import reconcile_sitemap
from seohead.crawl.sql_sitemap import open_sitemap_reconciliation, prepare_sitemap_reconciliation
from seohead.storage import ScanError, open_scan
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _link, _metadata, _record, _runtime


def _declaration(
    scan: NativeScan,
    sitemap_url: str,
    members: list[tuple[int, str]],
    *,
    source: str = "explicit",
    root_ordinal: int = 0,
    complete: bool = True,
) -> int:
    sitemap_id = scan.declare_sitemap(sitemap_url, source, root_ordinal)
    scan.write_sitemap_members(sitemap_id, members)
    scan.finish_sitemap(sitemap_id, complete, "" if complete else "fixture incomplete")
    return sitemap_id


def _robots_blocked(scan: NativeScan, url: str) -> None:
    row = scan.con.execute("SELECT url_id FROM urls WHERE url=?", (url,)).fetchone()
    assert row is not None
    url_id = int(row[0])
    scan.write_context(
        [
            {
                "kind": "robots_blocked_url",
                "item_key": f"url:{url_id}",
                "payload_version": "scan_context.v1",
                "payload_json": (
                    f'{{"policy":"report_only","token":"SEOHEAD-Tools","url_id":{url_id}' + "}"
                ),
                "completeness": "complete",
                "reason": "fixture",
            }
        ]
    )


def _commit(
    scan: NativeScan, url: str, links: list[dict] | None = None, **record_values: object
) -> None:
    lease = scan.claim(1)[0]
    assert lease.url == url
    record = _record(url)
    record["status_code"] = 200
    for key, value in record_values.items():
        record[key] = value
    scan.commit_page(lease, record, links=links or [], runtime=_runtime())


def _scan_with_pages(tmp_path):
    path = tmp_path / "scan.sqlite"
    scan = NativeScan.create(path, **_metadata())
    urls = [
        "https://example.test/",
        "https://example.test/linked/",
        "https://example.test/extra?x=1",
        "https://example.test/noindex",
        "https://example.test/blocked",
        "https://example.test/image.jpg",
    ]
    scan.enqueue([(url, 0 if index == 0 else 1) for index, url in enumerate(urls)])
    return scan, path


def test_no_saved_declarations_is_unavailable_not_a_zero_sitemap(tmp_path):
    scan, path = _scan_with_pages(tmp_path)
    try:
        _commit(scan, "https://example.test/")
    finally:
        scan.close()

    with open_sitemap_reconciliation(path, start_url="https://example.test/") as result:
        assert result.available is False
        assert result.reason == "no saved sitemap declarations"
        assert result.counts == {}
        assert result.materialize(10) == {
            "available": False,
            "reason": "no saved sitemap declarations",
        }


def test_complete_fetched_empty_sitemap_is_available_with_zero_members(tmp_path):
    scan, path = _scan_with_pages(tmp_path)
    try:
        _declaration(scan, "https://example.test/sitemap.xml", [])
        _commit(scan, "https://example.test/")
    finally:
        scan.close()

    with open_sitemap_reconciliation(path, start_url="https://example.test/") as result:
        assert result.available is True
        assert result.counts == {
            "urls_in_sitemap": 0,
            "urls_reached_by_links": 0,
            "in_sitemap_not_in_crawl": 0,
            "in_crawl_not_in_sitemap": 0,
        }
        assert all(
            list(result.iter_bucket(name)) == []
            for name in (
                "in_sitemap_and_linked",
                "in_sitemap_not_linked",
                "linked_not_in_sitemap",
                "linked_not_comparable",
            )
        )


def test_partial_saved_membership_is_unavailable_without_zero_counts(tmp_path):
    scan, path = _scan_with_pages(tmp_path)
    try:
        _declaration(scan, "https://example.test/sitemap.xml", [], complete=False)
    finally:
        scan.close()

    with open_sitemap_reconciliation(path, start_url="https://example.test/") as result:
        assert result.available is False
        assert result.reason == "saved sitemap declaration membership is partial"
        assert result.counts == {}
        assert list(result.iter_bucket("in_sitemap_not_linked")) == []


def test_sql_reconciliation_matches_current_normalized_populations(tmp_path):
    scan, path = _scan_with_pages(tmp_path)
    start = "https://example.test/"
    linked = "https://example.test/linked/#fragment"
    extra = "https://example.test/extra?x=1"
    external = "https://other.test/docs"
    unfetched = "https://example.test/unfetched"
    non_html = "https://example.test/image.jpg"
    noindex = "https://example.test/noindex"
    blocked = "https://example.test/blocked"
    declared = ["https://example.test/linked", "https://example.test/orphan/"]
    try:
        _declaration(
            scan,
            "https://example.test/sitemap.xml",
            list(enumerate(declared)),
        )
        _commit(
            scan,
            start,
            [
                _link(start, linked),
                _link(start, extra),
                _link(start, external),
                _link(start, unfetched),
                _link(start, non_html),
                _link(start, noindex),
                _link(start, blocked),
            ],
        )
        _commit(scan, "https://example.test/linked/")
        _commit(scan, extra)
        _commit(scan, noindex, meta_robots="noindex")
        _commit(scan, blocked)
        _commit(scan, non_html, content_type="image/jpeg")
        _robots_blocked(scan, blocked)
    finally:
        scan.close()

    observed = [linked, extra, external, unfetched, non_html, noindex, blocked]
    comparable = [start, "https://example.test/linked/", extra]
    expected = reconcile_sitemap(declared, observed, comparable)
    with open_sitemap_reconciliation(path, start_url=start) as result:
        assert result.available is True
        for name in (
            "in_sitemap_and_linked",
            "in_sitemap_not_linked",
            "linked_not_in_sitemap",
            "linked_not_comparable",
        ):
            assert list(result.iter_bucket(name)) == expected[name]
        assert result.counts == {
            "urls_in_sitemap": expected["urls_in_sitemap"],
            "urls_reached_by_links": expected["urls_reached_by_links"],
            "in_sitemap_not_in_crawl": expected["in_sitemap_not_in_crawl"],
            "in_crawl_not_in_sitemap": expected["in_crawl_not_in_sitemap"],
        }
        assert external in list(result.iter_bucket("linked_not_comparable"))
        assert non_html in list(result.iter_bucket("linked_not_comparable"))
        assert noindex in list(result.iter_bucket("linked_not_comparable"))
        assert blocked in list(result.iter_bucket("linked_not_comparable"))


def test_partial_summary_survives_but_materialization_stays_bounded(tmp_path):
    scan, path = _scan_with_pages(tmp_path)
    try:
        _declaration(
            scan,
            "https://example.test/sitemap.xml",
            [
                (0, "https://example.test/orphan-one"),
                (1, "https://example.test/orphan-two"),
            ],
        )
        _commit(scan, "https://example.test/")
        scan.con.execute("UPDATE scan SET crawl_partial=1 WHERE singleton=1")
        scan.con.commit()
    finally:
        scan.close()

    with open_sitemap_reconciliation(path, start_url="https://example.test/") as result:
        assert result.available is True
        assert result.crawl_partial is True
        assert result.materialize(2)["crawl_partial"] is True
        with pytest.raises(ValueError, match="exceeds materialize limit"):
            result.materialize(1)


def test_temp_tables_do_not_make_mode_ro_main_database_writable(tmp_path):
    scan, path = _scan_with_pages(tmp_path)
    try:
        _declaration(scan, "https://example.test/sitemap.xml", [])
    finally:
        scan.close()

    with open_sitemap_reconciliation(path, start_url="https://example.test/") as result:
        assert result._con is not None
        assert result._con.execute("PRAGMA temp_store").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            result._con.execute("INSERT INTO urls(url) VALUES('https://write.test/')")


def test_multi_root_members_use_one_run_wide_source_order(tmp_path):
    scan, path = _scan_with_pages(tmp_path)
    try:
        first = _declaration(
            scan,
            "https://example.test/root-one.xml",
            [(0, "https://example.test/first/")],
            root_ordinal=0,
        )
        second = _declaration(
            scan,
            "https://example.test/root-two.xml",
            [
                (1, "https://example.test/first"),
                (2, "https://example.test/second"),
            ],
            source="robots",
            root_ordinal=1,
        )
        assert scan.sitemap_roots() == [
            {
                "sitemap_url_id": first,
                "source": "explicit",
                "ordinal": 0,
                "url": "https://example.test/root-one.xml",
            },
            {
                "sitemap_url_id": second,
                "source": "robots",
                "ordinal": 1,
                "url": "https://example.test/root-two.xml",
            },
        ]
        assert list(scan.iter_sitemap_members(first)) == [(0, "https://example.test/first/")]
        assert list(scan.iter_sitemap_members(second)) == [
            (1, "https://example.test/first"),
            (2, "https://example.test/second"),
        ]
    finally:
        scan.close()

    with open_sitemap_reconciliation(path, start_url="https://example.test/") as result:
        assert result.declared_raw_count == 3
        assert list(result.iter_bucket("in_sitemap_not_linked")) == [
            "https://example.test/first/",
            "https://example.test/second",
        ]


def test_foreign_sqlite_header_is_refused_without_mutation(tmp_path):
    path = tmp_path / "foreign.sqlite"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE foreign_data(value TEXT)")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ScanError, match=r"cannot read scan|unsupported scan|foreign"):
        open_sitemap_reconciliation(path, start_url="https://example.test/")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_valid_mode_ro_read_keeps_main_file_hash_unchanged(tmp_path):
    scan, path = _scan_with_pages(tmp_path)
    try:
        _declaration(scan, "https://example.test/sitemap.xml", [])
    finally:
        scan.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with open_sitemap_reconciliation(path, start_url="https://example.test/") as result:
        assert result.summary()["available"] is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_prepare_uses_caller_validated_snapshot_without_closing_it(tmp_path):
    scan, path = _scan_with_pages(tmp_path)
    try:
        _declaration(scan, "https://example.test/sitemap.xml", [])
    finally:
        scan.close()

    con = open_scan(path, require_audit=False)
    try:
        result = prepare_sitemap_reconciliation(con, start_url="https://example.test/")
        result.close()
        assert con.execute("SELECT COUNT(*) FROM scan").fetchone()[0] == 1
    finally:
        con.close()


def test_prepare_restores_a_native_writer_query_only_setting(tmp_path):
    scan, _path = _scan_with_pages(tmp_path)
    try:
        _declaration(scan, "https://example.test/sitemap.xml", [])
        assert scan.con.execute("PRAGMA query_only").fetchone()[0] == 0
        result = prepare_sitemap_reconciliation(scan.con, start_url="https://example.test/")
        result.close()
        assert scan.con.execute("PRAGMA query_only").fetchone()[0] == 0
        assert scan.declare_sitemap("https://example.test/second.xml", "robots", 1) > 0
    finally:
        scan.close()
