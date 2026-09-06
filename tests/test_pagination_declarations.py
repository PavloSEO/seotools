"""PAGINATION_MULTIPLE / PAGINATION_URL_NOT_IN_ANCHOR — the declarations themselves.

Issue #385 listed both as gaps. Screaming Frog's All Inlinks export carries
every declaration as its own typed row beside every anchor, which is the shape
both of these need: one row per declaration to count them, and the anchors on
the same page to compare them against. ``Internal:All`` numbers repeated head
elements per occurrence rather than dropping them (its ``canonical`` /
``canonical_2`` pair is the same idea), but this toolkit's column map carries no
``rel="next" 2``, so a count taken there would be capped by our own column list
rather than by the data.

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


def test_one_successor_spelled_two_ways_is_one_successor(tmp_path):
    """Two plugins writing the same successor with different formatting.

    Regression for a de-duplication that compared raw strings while the anchor
    half of the same function compared through ``norm_url``: ``/blog/page/2``
    and ``/blog/page/2/`` were then one successor to one half and two to the
    other, and the warning told the operator a crawler must guess between two
    different successors when there is only one to guess at.
    """
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2)],
        [
            ["Rel Next", PAGE_1, PAGE_2, "", "200", "true"],
            ["Rel Next", PAGE_1, PAGE_2 + "/", "", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_2, "Page 2", "200", "true"],
        ],
    )
    assert not _fired(res, "PAGINATION_MULTIPLE")
    # The same identity on the anchor side, so neither spelling reads as
    # un-anchored either -- the two halves agree or neither answer is worth much.
    assert not _fired(res, "PAGINATION_URL_NOT_IN_ANCHOR")


def test_the_reported_url_is_the_spelling_the_page_used(tmp_path):
    """Normalizing decides identity; it does not rewrite the evidence quoted back."""
    res = _run(
        tmp_path,
        [_page_row(PAGE_1, PAGE_2), _page_row(PAGE_2), _page_row(PAGE_3)],
        [
            ["Rel Next", PAGE_1, PAGE_2 + "/", "", "200", "true"],
            ["Rel Next", PAGE_1, PAGE_3, "", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_2, "Page 2", "200", "true"],
            ["Hyperlink", PAGE_1, PAGE_3, "Page 3", "200", "true"],
        ],
    )
    assert _fired(res, "PAGINATION_MULTIPLE")[PAGE_1].details["urls"] == [PAGE_2 + "/", PAGE_3]


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
        assert "carries no link type" in (_skip_reason(res, check) or "")


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
