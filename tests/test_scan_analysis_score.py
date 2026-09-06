"""Parity tests for the disk-backed PageRank calculation."""

from __future__ import annotations

import sqlite3
import statistics

import pytest

from seohead.sf.core.link_score import compute_link_scores
from seohead.storage.analysis_score import compute_scores


def _tables(nodes: set[str], edges: list[tuple[str, str]]):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TEMP TABLE nodes(node TEXT PRIMARY KEY) WITHOUT ROWID")
    con.execute(
        "CREATE TEMP TABLE topology(seq INTEGER PRIMARY KEY,src_key TEXT NOT NULL,dst_key TEXT NOT NULL)"
    )
    con.executemany("INSERT INTO nodes VALUES(?)", [(node,) for node in sorted(nodes)])
    con.executemany(
        "INSERT INTO topology(seq,src_key,dst_key) VALUES(?,?,?)",
        [(index, source, destination) for index, (source, destination) in enumerate(edges)],
    )
    return con


@pytest.mark.parametrize(
    ("nodes", "edges"),
    [
        ({"a", "b", "c", "lonely"}, [("a", "b"), ("a", "b"), ("b", "c")]),
        ({"https://x/", "https://x/uncrawled"}, [("https://x/", "https://x/uncrawled")]),
        ({"https://x/a", "https://x/b#part", "https://x/b"}, [("https://x/a", "https://x/b#part")]),
        ({"self"}, [("self", "self")]),
    ],
)
def test_scores_match_existing_algorithm_rounded_to_report_precision(nodes, edges):
    con = _tables(nodes, edges)
    try:
        expected = compute_link_scores(edges, urls=nodes)
        actual = compute_scores(
            con,
            nodes_table="nodes",
            topology_table="topology",
            prefix="score_test",
            damping=0.85,
            max_iterations=200,
            tolerance=1e-10,
        )
        assert actual is not None
        assert actual.count == len(expected)
        assert round(actual.median, 6) == round(statistics.median(expected.values()), 6)
        assert {key: round(actual.score_for(key) or 0.0, 6) for key in nodes} == {
            key: round(value, 6) for key, value in expected.items()
        }
        actual.close()
    finally:
        con.close()


def test_external_edges_are_prefiltered_and_uncrawled_internal_nodes_remain_in_the_graph():
    internal_nodes = {"https://example.test/", "https://example.test/unseen"}
    internal_edges = [("https://example.test/", "https://example.test/unseen")]
    con = _tables(internal_nodes, internal_edges)
    try:
        actual = compute_scores(
            con,
            nodes_table="nodes",
            topology_table="topology",
            prefix="filtered",
            damping=0.85,
            max_iterations=200,
            tolerance=1e-10,
        )
        expected = compute_link_scores(internal_edges, urls=internal_nodes)
        assert actual is not None and actual.count == 2
        assert round(actual.score_for("https://example.test/unseen") or 0.0, 6) == round(
            expected["https://example.test/unseen"], 6
        )
        actual.close()
    finally:
        con.close()


def test_empty_topology_is_unavailable_even_when_current_pages_exist():
    con = _tables({"page-a", "page-b"}, [])
    try:
        assert (
            compute_scores(
                con,
                nodes_table="nodes",
                topology_table="topology",
                prefix="empty",
                damping=0.85,
                max_iterations=200,
                tolerance=1e-10,
            )
            is None
        )
    finally:
        con.close()


def test_low_score_threshold_members_match_existing_scores():
    nodes = {"home", "hub", "target", "lonely"}
    edges = [("home", "hub"), ("hub", "home"), ("hub", "target")]
    con = _tables(nodes, edges)
    try:
        expected = compute_link_scores(edges, urls=nodes)
        actual = compute_scores(
            con,
            nodes_table="nodes",
            topology_table="topology",
            prefix="threshold",
            damping=0.85,
            max_iterations=200,
            tolerance=1e-10,
        )
        assert actual is not None
        ratio = 0.25
        expected_low = {
            key
            for key, value in expected.items()
            if value < statistics.median(expected.values()) * ratio
        }
        actual_low = {
            key for key in nodes if (actual.score_for(key) or 0.0) < actual.median * ratio
        }
        assert actual_low == expected_low
        actual.close()
    finally:
        con.close()


def test_rejects_non_temp_or_inconsistent_caller_tables():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE permanent(node TEXT PRIMARY KEY)")
    con.execute("CREATE TEMP TABLE topology(seq INTEGER PRIMARY KEY,src_key TEXT,dst_key TEXT)")
    with pytest.raises(ValueError, match="TEMP"):
        compute_scores(
            con,
            nodes_table="permanent",
            topology_table="topology",
            prefix="bad",
            damping=0.85,
            max_iterations=1,
            tolerance=1e-10,
        )
    con.close()
