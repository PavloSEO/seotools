"""PAGINATION_SEQUENCE_ERROR — a break in an otherwise contiguous page-number run.

Issue #385 asked for this row with its own caveat attached: a series may
legitimately start at a number other than one, and may be filtered, so the
finding is a break in a run the series otherwise follows -- never a deviation
from ``1..n``. Most of what is asserted here is therefore silence: an ordered
series, a series starting at 4, a series with a stride, and a series whose URLs
do not state a page number all have to stay quiet.

Silence is not the same as clean, so each of those has to say out loud that it
was never judged, and say it with the reason that is true of *it*. The five
causes -- a crawl whose every chain cycles, a series that cycles from its head,
a series too short to hold a run, a URL that states no number, and a stride --
are not interchangeable, and three of them describe series in which every URL
does state its number. They are also counted per series rather than per run:
the WordPress shape (page one at an unnumbered ``/blog/``, the rest at
``/blog/page/N/``) is always unjudgeable, so on a real site the unjudged series
is the normal case sitting beside judgeable ones, not the whole crawl.
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
    prove which, so it is not reported and is named as unjudged instead.

    The reason has to be about the stride. Every one of these URLs states its
    page number, so a reason saying otherwise is a false statement about the
    evidence, and it sends the operator to fix a numbering scheme that is not
    the problem.
    """
    urls = [f"https://example.com/catalog?page={n}" for n in (0, 10, 20)]
    res = _run(tmp_path, _chain(urls))
    assert not _fired(res)
    reason = _skip_reason(res) or ""
    assert "never step by one" in reason
    assert "state no page number" not in reason
    assert "states no page number" not in reason


def test_a_series_whose_urls_state_no_page_number_declares_itself_unjudged(tmp_path):
    urls = ["https://example.com/catalog/2024", "https://example.com/catalog/2025"]
    urls.append("https://example.com/catalog/2026")
    res = _run(tmp_path, _chain(urls))
    assert not _fired(res)
    assert "3 of its 3 URLs state no page number" in (_skip_reason(res) or "")


def test_one_unnumbered_url_leaves_the_whole_series_unjudged(tmp_path):
    """A missing number is not evidence of a gap, so the series it sits in is
    not judged around it -- and the count says how much of it was unreadable."""
    urls = [
        "https://example.com/blog/page/1",
        "https://example.com/blog/latest",
        "https://example.com/blog/page/9",
    ]
    res = _run(tmp_path, _chain(urls))
    assert not _fired(res)
    assert "1 of its 3 URLs states no page number" in (_skip_reason(res) or "")


def test_a_two_page_series_is_not_a_run(tmp_path):
    """One step cannot be both the run and the break in it -- and both of these
    URLs state their number, so the reason must not claim otherwise."""
    res = _run(tmp_path, _chain(_pages(1, 6)))
    assert not _fired(res)
    reason = _skip_reason(res) or ""
    assert "only 2 pages long" in reason
    assert "no page number" not in reason


def test_a_cycling_series_is_left_to_pagination_loop(tmp_path):
    urls = _pages(1, 2, 7)
    rows = _chain(urls)
    rows[-1][5] = urls[1]  # page 7 points back at page 2
    res = _run(tmp_path, rows)
    assert not _fired(res)
    assert any(i.check == "PAGINATION_LOOP" for i in res.issues)
    reason = _skip_reason(res) or ""
    assert "cycles back on itself" in reason and "PAGINATION_LOOP" in reason
    assert "no page number" not in reason


def test_a_crawl_whose_every_chain_cycles_has_no_series_to_walk(tmp_path):
    """Two pages pointing at each other leave no head to start from, so the
    loop over heads never runs at all. That is its own cause and its own
    sentence, not the same one a missing page number gets."""
    a, b = _pages(1, 2)
    res = _run(tmp_path, [_chain([a, b])[0], _chain([b, a])[0]])
    assert not _fired(res)
    reason = _skip_reason(res) or ""
    assert "cycles back on itself" in reason and "first page to walk from" in reason
    assert "no page number" not in reason


def test_an_unjudged_series_is_named_even_when_another_one_was_judged(tmp_path):
    """The guard is per series, not per run.

    A crawl holding one judgeable series and one unjudgeable one used to report
    the second as neither a finding nor a skip, so a series that may hold
    exactly the gap this check exists to find read as clean. The judgeable
    series here is deliberately in order: with it silent too, the whole check
    would otherwise have nothing to say about a crawl it only half read.
    """
    judgeable = _pages(1, 2, 3, prefix="https://example.com/news/page/")
    wordpress = [
        "https://example.com/blog/",
        "https://example.com/blog/page/2/",
        "https://example.com/blog/page/3/",
    ]
    res = _run(tmp_path, _chain(judgeable) + _chain(wordpress))
    assert not _fired(res)
    reason = _skip_reason(res) or ""
    assert reason.startswith('1 of 2 rel="next" series could not be judged')
    assert "https://example.com/blog/" in reason


def test_each_unjudged_cause_is_counted_and_named_on_its_own(tmp_path):
    """Three series, three different causes, one reason that keeps them apart."""
    stride = [f"https://example.com/catalog?page={n}" for n in (0, 10, 20)]
    short = _pages(1, 6, prefix="https://example.com/tags/page/")
    unnumbered = [
        "https://example.com/blog/",
        "https://example.com/blog/page/2/",
        "https://example.com/blog/page/3/",
    ]
    res = _run(tmp_path, _chain(stride) + _chain(short) + _chain(unnumbered))
    reason = _skip_reason(res) or ""
    assert reason.startswith('3 of 3 rel="next" series could not be judged')
    for clause, example in (
        ("never step by one", stride[0]),
        ("only 2 pages long", short[0]),
        ("1 of its 3 URLs states no page number", unnumbered[0]),
    ):
        assert clause in reason, reason
        assert example in reason, reason


def test_a_finding_and_an_unjudged_series_report_as_a_finding(tmp_path):
    """Where the audit contract puts a check that both fired and fell short.

    ``AuditContext.skip`` refuses a check that already has findings, and that is
    the contract, not an oversight: a check with evidence is not a skipped
    check. So the crawl below carries the break and no skip beside it. The rule
    this exists to serve still holds -- the check does not read as clean -- but
    the unjudged series only gets its own sentence once nothing is firing over
    it, which is why the per-series count above matters most on the crawls where
    the check finds nothing.
    """
    broken = _pages(1, 2, 3, 7, prefix="https://example.com/news/page/")
    wordpress = [
        "https://example.com/blog/",
        "https://example.com/blog/page/2/",
        "https://example.com/blog/page/3/",
    ]
    res = _run(tmp_path, _chain(broken) + _chain(wordpress))
    assert set(_fired(res)) == {broken[0]}
    assert _skip_reason(res) is None


def test_a_crawl_whose_series_are_all_judgeable_says_nothing(tmp_path):
    """The skip is about series that could not be read, so a crawl with none of
    those must not carry one."""
    res = _run(
        tmp_path,
        _chain(_pages(1, 2, 3)) + _chain(_pages(1, 2, 3, prefix="https://example.com/n/page/")),
    )
    assert not _fired(res)
    assert _skip_reason(res) is None


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
