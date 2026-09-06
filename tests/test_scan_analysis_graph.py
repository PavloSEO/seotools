"""Native-scan parity fixtures for F raw anchor and composition projections."""

from __future__ import annotations

import pandas as pd

from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.inlinks import (
    _GENERIC_ANCHORS,
    _norm_anchor,
    check_anchor_text,
    check_inlink_composition,
)
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.normalize import norm_url
from seohead.storage.analysis_graph import AnalysisGraph
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _metadata, _record, _runtime


def _edge(source: str, destination: str, anchor: str, *, position: str, nofollow: bool) -> dict:
    return {
        "source": source,
        "destination": destination,
        "anchor": anchor,
        "nofollow": nofollow,
        "position": position,
        "rel": (),
        "target": "",
        "raw_href": "",
    }


def _scan(tmp_path):
    path = tmp_path / "scan.sqlite"
    scan = NativeScan.create(path, **_metadata())
    pages = ["https://example.test/", "https://example.test/a", "https://example.test/b"]
    scan.enqueue([(url, index) for index, url in enumerate(pages)])
    return scan, pages


def _commit(scan, url: str, links: list[dict]) -> None:
    lease = scan.claim(1)[0]
    assert lease.url == url
    record = _record(url)
    record["status_code"] = 200
    record["crawl_depth"] = lease.depth
    runtime = _runtime()
    runtime["max_depth_reached"] = lease.depth
    scan.commit_page(lease, record, links=links, runtime=runtime)


def _generic(anchor: str) -> bool:
    return _norm_anchor(anchor) in _GENERIC_ANCHORS


def _legacy(pages: list[str], rows: list[dict]) -> AuditContext:
    exports = LoadedExports()
    exports.frames["internal_all"] = pd.DataFrame(
        [
            {
                "Address": url,
                "Content Type": "text/html",
                "Status Code": 200,
                "Indexability": "Indexable",
            }
            for url in pages
        ]
    )
    exports.frames["all_inlinks"] = pd.DataFrame(rows)
    context = AuditContext(exports, load_config(None))
    check_anchor_text(context)
    check_inlink_composition(context)
    return context


def test_anchor_groups_keep_raw_spelling_locations_and_legacy_dedup(tmp_path):
    scan, pages = _scan(tmp_path)
    root, a, b = pages
    try:
        _commit(
            scan,
            root,
            [
                _edge(root, a, " Read more ", position="footer", nofollow=False),
                _edge(root, a, "read more", position="content", nofollow=False),
                _edge(root, b, "click here", position="nav", nofollow=True),
                _edge(root, b, "", position="footer", nofollow=False),
            ],
        )
        _commit(scan, a, [])
        _commit(scan, b, [])
        with AnalysisGraph(scan.con, normalize=norm_url, site_host="example.test") as graph:
            groups = list(graph.iter_anchor_groups(_generic, 2))
    finally:
        scan.close()

    assert len(groups) == 1
    group = groups[0]
    assert group.source_url == root
    assert group.occurrences_count == 2
    legacy = _legacy(
        pages,
        [
            {
                "Source": root,
                "Destination": a,
                "Anchor Text": " Read more ",
                "Link Position": "footer",
                "Follow": "true",
                "Type": "",
            },
            {
                "Source": root,
                "Destination": a,
                "Anchor Text": "read more",
                "Link Position": "content",
                "Follow": "true",
                "Type": "",
            },
            {
                "Source": root,
                "Destination": b,
                "Anchor Text": "click here",
                "Link Position": "nav",
                "Follow": "false",
                "Type": "",
            },
        ],
    )
    expected = next(issue for issue in legacy.issues if issue.check == "GENERIC_ANCHOR_TEXT")
    assert group.source_url == expected.target_url
    assert group.occurrences_count == expected.occurrences_count
    assert group.generic_links == expected.details["generic_links"]
    assert group.locations == expected.locations


def test_composition_defragments_destinations_preserves_first_edge_and_source_states(tmp_path):
    scan, pages = _scan(tmp_path)
    root, a, b = pages
    external = "https://outside.test/x"
    uncrawled = "https://example.test/unfetched"
    try:
        _commit(
            scan,
            root,
            [
                _edge(root, b + "#first", "x", position="footer", nofollow=True),
                _edge(root, a, "x", position="content", nofollow=True),
                _edge(root, b + "#second", "x", position="content", nofollow=False),
                _edge(root, external, "x", position="footer", nofollow=False),
                _edge(root, uncrawled, "x", position="footer", nofollow=True),
            ],
        )
        _commit(scan, a, [_edge(a, b, "x", position="content", nofollow=True)])
        _commit(scan, b, [])
        states = {root: True, a: False, b: None}
        with AnalysisGraph(scan.con, normalize=norm_url, site_host="example.test") as graph:
            rows = list(graph.iter_inlink_composition(lambda source: states.get(source), 2))
    finally:
        scan.close()

    by_destination = {row.destination_key: row for row in rows}
    key_b = norm_url(b)
    assert rows[0].destination_key == key_b
    b_row = by_destination[key_b]
    assert b_row.occurrences_count == 3
    assert b_row.all_nofollow is False
    assert b_row.has_known_source is True
    assert b_row.has_indexable_source is True
    assert b_row.source_examples == [root, a]
    assert norm_url(uncrawled) in by_destination
    assert by_destination[norm_url(uncrawled)].has_indexable_source is True
    legacy = _legacy(
        pages,
        [
            {
                "Source": root,
                "Destination": b + "#first",
                "Anchor Text": "x",
                "Link Position": "footer",
                "Follow": "false",
                "Type": "",
            },
            {
                "Source": root,
                "Destination": a,
                "Anchor Text": "x",
                "Link Position": "content",
                "Follow": "false",
                "Type": "",
            },
            {
                "Source": root,
                "Destination": b + "#second",
                "Anchor Text": "x",
                "Link Position": "content",
                "Follow": "true",
                "Type": "",
            },
            {
                "Source": root,
                "Destination": external,
                "Anchor Text": "x",
                "Link Position": "footer",
                "Follow": "true",
                "Type": "",
            },
            {
                "Source": root,
                "Destination": uncrawled,
                "Anchor Text": "x",
                "Link Position": "footer",
                "Follow": "false",
                "Type": "",
            },
            {
                "Source": a,
                "Destination": b,
                "Anchor Text": "x",
                "Link Position": "content",
                "Follow": "false",
                "Type": "",
            },
        ],
    )
    legacy_nofollow = {
        issue.target_url for issue in legacy.issues if issue.check == "ONLY_NOFOLLOW_INLINKS"
    }
    assert legacy_nofollow == {a}
    # The raw projection retains an uncrawled internal endpoint. The existing
    # emitter, like the legacy check above, withholds a page-level issue until
    # a matching crawled page fact exists.
    assert by_destination[norm_url(uncrawled)].all_nofollow is True
    assert norm_url(external) not in by_destination
