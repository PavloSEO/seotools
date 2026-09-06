"""Parity of disk-backed native discovery paths with the established BFS contract."""

from __future__ import annotations

import sqlite3

import pytest

from seohead.sf.core.crawl_path import shortest_paths_from_seed
from seohead.storage import ScanError
from seohead.storage.analysis_paths import PathSession


def _session(
    edges: list[tuple[str, str]], seed: str = "seed"
) -> tuple[sqlite3.Connection, PathSession | None]:
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TEMP TABLE p_topology(seq INTEGER NOT NULL, src_key TEXT NOT NULL, dst_key TEXT NOT NULL)"
    )
    con.executemany(
        "INSERT INTO p_topology(seq, src_key, dst_key) VALUES(?, ?, ?)",
        [(index, source, destination) for index, (source, destination) in enumerate(edges)],
    )
    return con, PathSession.open(con, prefix="p", seed=seed)


@pytest.mark.parametrize(
    "edges,seed",
    [
        ([("seed", "a"), ("seed", "a"), ("a", "b"), ("b", "a")], "seed"),
        ([("seed", "right"), ("seed", "left"), ("right", "target"), ("left", "target")], "seed"),
        ([("seed", "seed"), ("x", "y")], "seed"),
        (
            [
                ("seed", "https://example.test/a#fragment"),
                ("https://example.test/a#fragment", "end"),
            ],
            "seed",
        ),
    ],
)
def test_disk_paths_match_existing_bfs_for_repeated_edges_ties_cycles_and_fragments(edges, seed):
    con, session = _session(edges, seed)
    assert session is not None
    try:
        expected = shortest_paths_from_seed(edges, seed)
        nodes = {seed, *(node for edge in edges for node in edge)}
        assert {node: session.path_to(node) for node in nodes} == {
            node: tuple(expected[node]) if node in expected else None for node in nodes
        }
    finally:
        session.close()
        con.close()


def test_seed_without_an_outgoing_edge_has_its_singleton_path_when_graph_exists():
    con, session = _session([("x", "y")])
    assert session is not None
    try:
        assert session.path_to("seed") == ("seed",)
        assert session.path_to("x") is None
    finally:
        session.close()
        con.close()


def test_empty_topology_is_an_explicit_no_session_signal():
    con, session = _session([])
    try:
        assert session is None
    finally:
        con.close()


def test_many_successive_lookups_reuse_one_fifo_session():
    edges = [("seed", "a"), ("seed", "b"), ("a", "c"), ("b", "d"), ("c", "e")]
    con, session = _session(edges)
    assert session is not None
    try:
        assert session.path_to("e") == ("seed", "a", "c", "e")
        assert session.path_to("d") == ("seed", "b", "d")
        assert session.path_to("a") == ("seed", "a")
        assert session.path_to("missing") is None
    finally:
        session.close()
        con.close()


def test_constructor_refuses_duplicate_sequence_or_missing_shape():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TEMP TABLE p_topology(seq INTEGER, src_key TEXT, dst_key TEXT)")
    con.executemany("INSERT INTO p_topology VALUES(?, ?, ?)", [(1, "seed", "a"), (1, "seed", "b")])
    with pytest.raises(ScanError, match="sequence"):
        PathSession.open(con, prefix="p", seed="seed")
    con.close()


def test_close_keeps_caller_topology_and_removes_only_session_state():
    con, session = _session([("seed", "target")])
    assert session is not None
    session.close()
    names = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_temp_master WHERE type IN ('table', 'index')"
        )
    }
    assert names == {"p_topology"}
    con.close()
