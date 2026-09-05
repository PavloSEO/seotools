"""PAGINATION_NONINDEXABLE — the crawled rel="next"/rel="prev" target's own status.

Issue #203: the original check only ever read the *source* page's own
indexability, so a perfectly indexable page 1 declaring rel="next" to a 404
page 2 emitted nothing — the rule claimed coverage for exactly the case it
could not see. This resolves the relation to whatever the crawl says about
the target and judges that, while leaving an uncrawled target alone (nothing
here knows its status).
"""

from __future__ import annotations

import csv

from seohead.sf.core.audit import run_audit

COLS = [
    "Address",
    "Content Type",
    "Status Code",
    "Status",
    "Indexability",
    'rel="next" 1',
    "Inlinks",
    "Crawl Depth",
]


def _run(tmp_path, rows):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(rows)
    return run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)


def _fired(res, check):
    return {i.target_url: i for i in res.issues if i.check == check}


def test_a_crawled_404_pagination_target_fires_even_though_the_source_is_indexable(tmp_path):
    rows = [
        [
            "https://example.com/page/1",
            "text/html",
            "200",
            "OK",
            "Indexable",
            "https://example.com/page/2",
            "2",
            "0",
        ],
        [
            "https://example.com/page/2",
            "text/html",
            "404",
            "Not Found",
            "Non-Indexable",
            "",
            "1",
            "1",
        ],
    ]
    res = _run(tmp_path, rows)
    fired = _fired(res, "PAGINATION_NONINDEXABLE")
    assert set(fired) == {"https://example.com/page/2"}
    details = fired["https://example.com/page/2"].details
    assert details["status_code"] == 404
    assert details["relation"] == 'rel="next"'
    assert details["source_url"] == "https://example.com/page/1"
    # the indexable source itself must not also be reported
    assert "https://example.com/page/1" not in fired


def test_a_crawled_301_pagination_target_also_fires(tmp_path):
    rows = [
        [
            "https://example.com/page/1",
            "text/html",
            "200",
            "OK",
            "Indexable",
            "https://example.com/page/2",
            "2",
            "0",
        ],
        ["https://example.com/page/2", "text/html", "301", "Moved", "Non-Indexable", "", "1", "1"],
    ]
    res = _run(tmp_path, rows)
    fired = _fired(res, "PAGINATION_NONINDEXABLE")
    assert set(fired) == {"https://example.com/page/2"}


def test_an_uncrawled_pagination_target_is_not_asserted_to_be_non_200(tmp_path):
    # page/1 names a rel="next" target the crawl never reached — nothing here
    # knows its status, so it must not be flagged (negative control for #203).
    rows = [
        [
            "https://example.com/page/1",
            "text/html",
            "200",
            "OK",
            "Indexable",
            "https://example.com/page/2",
            "2",
            "0",
        ],
    ]
    res = _run(tmp_path, rows)
    assert _fired(res, "PAGINATION_NONINDEXABLE") == {}


def test_a_healthy_indexable_pagination_target_stays_silent(tmp_path):
    # both source and target are 200/indexable — the ordinary, correct case.
    rows = [
        [
            "https://example.com/page/1",
            "text/html",
            "200",
            "OK",
            "Indexable",
            "https://example.com/page/2",
            "2",
            "0",
        ],
        ["https://example.com/page/2", "text/html", "200", "OK", "Indexable", "", "1", "1"],
    ]
    res = _run(tmp_path, rows)
    assert _fired(res, "PAGINATION_NONINDEXABLE") == {}


def test_the_original_self_indexability_check_still_fires(tmp_path):
    # a page that itself declares rel="next" while being non-indexable, with
    # no other page pointing at it — the pre-existing behavior must survive.
    rows = [
        [
            "https://example.com/page/2",
            "text/html",
            "200",
            "OK",
            "Non-Indexable",
            "https://example.com/page/3",
            "1",
            "1",
        ],
    ]
    res = _run(tmp_path, rows)
    fired = _fired(res, "PAGINATION_NONINDEXABLE")
    assert set(fired) == {"https://example.com/page/2"}
