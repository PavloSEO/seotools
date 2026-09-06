"""PAGINATION_SEQUENCE_ERROR — a break in an otherwise contiguous page-number run.

Issue #385 asked for this row with its own caveat attached: a series may
legitimately start at a number other than one, and may be filtered, so the
finding is a break in a run the series otherwise follows -- never a deviation
from ``1..n``. Most of what is asserted here is therefore silence: an ordered
series, a series starting at 4, a series with a stride, and a series whose URLs
do not state a page number all have to stay quiet, and the last of them has to
say out loud that it was never judged.
"""

from __future__ import annotations

import csv

from seohead.sf.core.audit import run_audit
from seohead.sf.core.rules import pagination_page_number

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


def _chain(urls):
    """Rows for a rel="next" chain over ``urls``, in order."""
    rows = []
    for index, url in enumerate(urls):
        nxt = urls[index + 1] if index + 1 < len(urls) else ""
        rows.append([url, "text/html", "200", "OK", "Indexable", nxt, "3", "1"])
    return rows


def _run(tmp_path, rows):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(rows)
    return run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)


def _fired(res):
    return {i.target_url: i for i in res.issues if i.check == "PAGINATION_SEQUENCE_ERROR"}


def _skip_reason(res):
    for skipped in res.skipped:
        if skipped.id == "PAGINATION_SEQUENCE_ERROR":
            return skipped.reason
    return None


def _pages(*numbers, prefix="https://example.com/blog/page/"):
    return [f"{prefix}{n}" for n in numbers]


def test_a_run_that_skips_pages_fires_and_names_the_break(tmp_path):
    urls = _pages(1, 2, 3, 7)
    res = _run(tmp_path, _chain(urls))
    fired = _fired(res)
    assert set(fired) == {urls[0]}
    details = fired[urls[0]].details
    assert details["page_numbers"] == [1, 2, 3, 7]
    assert details["breaks"] == [{"from": urls[2], "to": urls[3], "from_page": 3, "to_page": 7}]


def test_a_query_parameter_series_is_read_the_same_way(tmp_path):
    urls = [f"https://example.com/catalog?page={n}" for n in (1, 2, 5)]
    res = _run(tmp_path, _chain(urls))
    assert set(_fired(res)) == {urls[0]}


def test_an_ordered_run_stays_silent(tmp_path):
    res = _run(tmp_path, _chain(_pages(1, 2, 3, 4)))
    assert not _fired(res)
    assert _skip_reason(res) is None


def test_a_series_that_starts_at_four_is_not_an_error(tmp_path):
    """The issue's own caveat: a crawl of a subsection, or a series whose first
    page lives at an unnumbered URL, starts wherever it starts."""
    res = _run(tmp_path, _chain(_pages(4, 5, 6)))
    assert not _fired(res)


def test_a_series_with_a_stride_is_left_unevaluated(tmp_path):
    """0, 10, 20 is an offset scheme, not a broken run -- and nothing here can
    prove which, so it is not reported and not counted as evaluated either."""
    urls = [f"https://example.com/catalog?page={n}" for n in (0, 10, 20)]
    res = _run(tmp_path, _chain(urls))
    assert not _fired(res)
    assert "states a page number in every one of its URLs" in (_skip_reason(res) or "")


def test_a_series_whose_urls_state_no_page_number_declares_itself_unjudged(tmp_path):
    urls = ["https://example.com/catalog/2024", "https://example.com/catalog/2025"]
    urls.append("https://example.com/catalog/2026")
    res = _run(tmp_path, _chain(urls))
    assert not _fired(res)
    assert "states a page number in every one of its URLs" in (_skip_reason(res) or "")


def test_one_unnumbered_url_leaves_the_whole_series_unjudged(tmp_path):
    """A missing number is not evidence of a gap, so the series it sits in is
    not judged around it."""
    urls = [
        "https://example.com/blog/page/1",
        "https://example.com/blog/latest",
        "https://example.com/blog/page/9",
    ]
    res = _run(tmp_path, _chain(urls))
    assert not _fired(res)


def test_a_two_page_series_is_not_a_run(tmp_path):
    """One step cannot be both the run and the break in it."""
    res = _run(tmp_path, _chain(_pages(1, 6)))
    assert not _fired(res)


def test_a_cycling_series_is_left_to_pagination_loop(tmp_path):
    urls = _pages(1, 2, 7)
    rows = _chain(urls)
    rows[-1][5] = urls[1]  # page 7 points back at page 2
    res = _run(tmp_path, rows)
    assert not _fired(res)
    assert any(i.check == "PAGINATION_LOOP" for i in res.issues)


def test_without_the_column_the_check_declares_itself_absent(tmp_path):
    rows = [["https://example.com/", "text/html", "200", "OK", "Indexable", "", "3", "0"]]
    res = _run(tmp_path, rows)
    assert 'no rel="next" column in Internal:All' in (_skip_reason(res) or "")


def test_page_number_is_read_only_from_a_token_that_means_a_page():
    assert pagination_page_number("https://example.com/blog/page/2") == 2
    assert pagination_page_number("https://example.com/blog/page/2/") == 2
    assert pagination_page_number("https://example.com/c?paged=11") == 11
    assert pagination_page_number("https://example.com/c?PAGE=3") == 3
    # A bare number is a year, an id or a slug just as often as a page index.
    assert pagination_page_number("https://example.com/2024/03/a-post") is None
    assert pagination_page_number("https://example.com/product/1188") is None
    # WordPress's ?p= is a post id, not a page index.
    assert pagination_page_number("https://example.com/?p=417") is None
    # Two numbers that disagree do not tell us which page this is.
    assert pagination_page_number("https://example.com/blog/page/2?page=5") is None
