"""PAGINATION_LOOP — the default hop cap must not outrun the series it walks.

Issue #204: the pagination walker reused ``redirect_hop_cap`` (default 20)
verbatim, so a fully-captured 21-page rel="next" cycle silently read as
clean — the walk ran out of budget one hop short of ever revisiting a node.
``next_map`` is a functional graph (each key has at most one outgoing edge),
so bounding the walk by the graph's own size instead removes the false floor
without weakening detection for anything smaller.
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


def _run(tmp_path, rows, config=None):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(rows)
    return run_audit(
        input_mode="parse-exports", exports_dir=str(d), config=config, log=lambda m: None
    )


def _fired(res, check):
    return {i.target_url: i for i in res.issues if i.check == check}


def _cycle_rows(length):
    rows = []
    for n in range(1, length + 1):
        nxt = n + 1 if n < length else 1
        rows.append(
            [
                f"https://example.com/page/{n}",
                "text/html",
                "200",
                "OK",
                "Indexable",
                f"https://example.com/page/{nxt}",
                "1",
                "1",
            ]
        )
    return rows


def test_a_21_page_cycle_fires_under_the_default_hop_cap(tmp_path):
    res = _run(tmp_path, _cycle_rows(21))
    loops = _fired(res, "PAGINATION_LOOP")
    assert set(loops) == {"https://example.com/page/1"}
    assert len(loops["https://example.com/page/1"].details["series"]) == 21


def test_a_short_loop_still_fires_unchanged(tmp_path):
    res = _run(tmp_path, _cycle_rows(2))
    loops = _fired(res, "PAGINATION_LOOP")
    assert set(loops) == {"https://example.com/page/1"}


def test_a_terminating_series_longer_than_the_old_cap_stays_silent(tmp_path):
    # a legitimate 25-page series that ends cleanly (no cycle) must not be
    # reported as a loop just because the walk now goes deeper to check.
    rows = []
    for n in range(1, 26):
        nxt = f"https://example.com/page/{n + 1}" if n < 25 else ""
        rows.append(
            [
                f"https://example.com/page/{n}",
                "text/html",
                "200",
                "OK",
                "Indexable",
                nxt,
                "1",
                "1",
            ]
        )
    res = _run(tmp_path, rows)
    assert _fired(res, "PAGINATION_LOOP") == {}


def test_the_walk_visits_each_page_once_across_the_whole_pass():
    """The bound on the walk is the graph's structure, not a hop cap -- and the
    cost of getting that wrong is not a wrong answer but a quadratic one.

    Bounding each walk by ``len(next_map)`` is correct and re-walks the tail from
    every node: one terminating 16 000-page series took 8.29 s that way against
    0.65 s here. A catalogue or news site with thousands of pages in one series
    is the ordinary case, so this pins the property rather than the timing --
    a timing assertion would be flaky and would not say what went wrong.
    """
    from seohead.sf.core.rules import pagination_loops

    class CountingMap(dict):
        lookups = 0

        def get(self, key, default=None):
            CountingMap.lookups += 1
            return super().get(key, default)

    n = 2_000
    chain = CountingMap(
        {f"https://example.com/p{i}": f"https://example.com/p{i + 1}" for i in range(n)}
    )
    loops, in_loop = pagination_loops(chain)

    assert loops == [] and in_loop == set()
    # One lookup per node to walk it, plus one that finds the end of the chain.
    # Quadratic would be ~n**2/2 == two million.
    assert CountingMap.lookups <= n + 1


def test_a_cycle_is_still_found_when_a_long_tail_leads_into_it():
    """Visiting each node once must not cost loop detection: a walk that enters a
    cycle always revisits its own path, so the first walk through reports it."""
    from seohead.sf.core.rules import pagination_loops

    tail = {f"https://example.com/t{i}": f"https://example.com/t{i + 1}" for i in range(50)}
    tail["https://example.com/t50"] = "https://example.com/c0"
    cycle = {f"https://example.com/c{i}": f"https://example.com/c{(i + 1) % 30}" for i in range(30)}
    loops, in_loop = pagination_loops({**tail, **cycle})

    assert len(loops) == 1
    start, path, loops_to = loops[0]
    assert start == "https://example.com/t0"
    assert loops_to == "https://example.com/c0"
    assert len(path) == 81  # 51 tail pages plus the 30 in the cycle
    assert "https://example.com/c0" in in_loop
