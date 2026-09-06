"""PAGINATION_MULTIPLE / PAGINATION_URL_NOT_IN_ANCHOR — the declarations themselves.

Issue #385 listed both as gaps for the same reason: ``Internal:All`` keeps the
first ``rel="next"`` and ``rel="prev"`` per page and drops the rest, so neither
"how many did this page declare" nor "is the declared URL also an anchor" could
be answered from the column the older pagination checks read. Screaming Frog's
All Inlinks export carries every declaration as a typed row beside every anchor,
which is where both of these read from.

The silent half matters more than the firing half here: an ordinary paginated
page that declares one successor and links it is the common case, and a check
that fires on it is a check an operator learns to ignore.
"""

from __future__ import annotations

import csv

from seohead.sf.core.audit import run_audit

INTERNAL_COLS = [
    "Address",
    "Content Type",
    "Status Code",
    "Status",
    "Indexability",
    'rel="next" 1',
    "Inlinks",
    "Crawl Depth",
]
INLINK_COLS = ["Type", "Source", "Destination", "Anchor Text", "Status Code", "Follow"]

PAGE_1 = "https://example.com/blog/page/1"
PAGE_2 = "https://example.com/blog/page/2"
PAGE_3 = "https://example.com/blog/page/3"


def _page_row(url: str, rel_next: str = "") -> list[str]:
    return [url, "text/html", "200", "OK", "Indexable", rel_next, "3", "1"]


def _run(tmp_path, pages, inlinks=None, inlink_cols=INLINK_COLS):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(INTERNAL_COLS)
        w.writerows(pages)
    if inlinks is not None:
        with open(d / "all_inlinks.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(inlink_cols)
            w.writerows(inlinks)
    return run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)


def _fired(res, check):
    return {i.target_url: i for i in res.issues if i.check == check}


def _skip_reason(res, check):
    for skipped in res.skipped:
        if skipped.id == check:
            return skipped.reason
    return None


def test_two_different_rel_next_urls_on_one_page_fire(tmp_path):
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2), _page_row(PAGE_3)],
        [
            ["Rel Next", PAGE_1, PAGE_2, "", "200", "true"],
            ["Rel Next", PAGE_1, PAGE_3, "", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_2, "Page 2", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_3, "Page 3", "200", "true"],
        ],
    )
    fired = _fired(res, "PAGINATION_MULTIPLE")
    assert set(fired) == {PAGE_1}
    details = fired[PAGE_1].details
    assert details["relation"] == 'rel="next"'
    assert details["urls"] == [PAGE_2, PAGE_3]


def test_one_declaration_per_relation_stays_silent(tmp_path):
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2, PAGE_3), _page_row(PAGE_3)],
        [
            ["Rel Next", PAGE_1, PAGE_2, "", "200", "true"],
            ["Rel Next", PAGE_2, PAGE_3, "", "200", "true"],
            ["Rel Prev", PAGE_2, PAGE_1, "", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_2, "Page 2", "200", "true"],
            ["Hyperlink", PAGE_2, PAGE_3, "Page 3", "200", "true"],
            ["Hyperlink", PAGE_2, PAGE_1, "Page 1", "200", "true"],
        ],
    )
    assert not _fired(res, "PAGINATION_MULTIPLE")


def test_the_same_successor_declared_twice_is_one_successor(tmp_path):
    """Untidy markup with one answer is not an ambiguous series, and the row is
    about which page comes next."""
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2)],
        [
            ["Rel Next", PAGE_1, PAGE_2, "", "200", "true"],
            ["Rel Next", PAGE_1, PAGE_2, "", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_2, "Page 2", "200", "true"],
        ],
    )
    assert not _fired(res, "PAGINATION_MULTIPLE")


def test_a_declared_url_with_no_anchor_on_the_same_page_fires(tmp_path):
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2)],
        [
            ["Rel Next", PAGE_1, PAGE_2, "", "200", "true"],
            ["Hyperlink", PAGE_1, "https://example.com/about", "About", "200", "true"],
        ],
    )
    fired = _fired(res, "PAGINATION_URL_NOT_IN_ANCHOR")
    assert set(fired) == {PAGE_1}
    assert fired[PAGE_1].details["declared_without_an_anchor"] == [
        {"relation": 'rel="next"', "url": PAGE_2}
    ]


def test_an_anchor_to_the_declared_url_keeps_it_silent(tmp_path):
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2)],
        [
            ["Rel Next", PAGE_1, PAGE_2, "", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_2, "Next", "200", "true"],
        ],
    )
    assert not _fired(res, "PAGINATION_URL_NOT_IN_ANCHOR")


def test_a_nofollow_anchor_is_still_an_anchor(tmp_path):
    """The question is whether an <a href> to the paginated URL exists, not
    whether it passes equity."""
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2)],
        [
            ["Rel Next", PAGE_1, PAGE_2, "", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_2, "Next", "200", "false"],
        ],
    )
    assert not _fired(res, "PAGINATION_URL_NOT_IN_ANCHOR")


def test_a_trailing_slash_difference_is_not_a_missing_anchor(tmp_path):
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2 + "/"), _page_row(PAGE_2)],
        [
            ["Rel Next", PAGE_1, PAGE_2 + "/", "", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_2, "Next", "200", "true"],
        ],
    )
    assert not _fired(res, "PAGINATION_URL_NOT_IN_ANCHOR")


def test_without_the_export_both_declare_themselves_absent(tmp_path):
    res = _run(tmp_path, [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2)])
    for check in ("PAGINATION_MULTIPLE", "PAGINATION_URL_NOT_IN_ANCHOR"):
        assert "no all_inlinks export" in (_skip_reason(res, check) or "")


def test_an_export_with_no_type_column_declares_itself_absent(tmp_path):
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2)],
        [[PAGE_1, PAGE_2, "Next", "200", "true"]],
        inlink_cols=["Source", "Destination", "Anchor Text", "Status Code", "Follow"],
    )
    for check in ("PAGINATION_MULTIPLE", "PAGINATION_URL_NOT_IN_ANCHOR"):
        assert "no Type column" in (_skip_reason(res, check) or "")


def test_an_export_with_no_pagination_rows_declares_itself_absent(tmp_path):
    res = _run(
        tmp_path,
        [_page_row(PAGE_1), _page_row(PAGE_2)],
        [["Hyperlink", PAGE_1, PAGE_2, "Next", "200", "true"]],
    )
    for check in ("PAGINATION_MULTIPLE", "PAGINATION_URL_NOT_IN_ANCHOR"):
        assert 'no rel="next"/rel="prev" rows' in (_skip_reason(res, check) or "")


def test_an_export_with_no_hyperlink_rows_does_not_report_every_declaration(tmp_path):
    """Declarations and not one anchor row anywhere is a filtered export, not a
    site without links; reporting off it would be a crawl of wrong findings."""
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2)],
        [["Rel Next", PAGE_1, PAGE_2, "", "200", "true"]],
    )
    assert not _fired(res, "PAGINATION_URL_NOT_IN_ANCHOR")
    assert "no Hyperlink rows" in (_skip_reason(res, "PAGINATION_URL_NOT_IN_ANCHOR") or "")
