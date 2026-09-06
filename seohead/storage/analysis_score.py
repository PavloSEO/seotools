"""Disk-backed PageRank calculation for an already prepared native graph.

The caller owns the temporary node and topology tables.  This module only
creates short-lived calculation tables with a caller-provided private prefix;
it neither knows crawler rows nor imports SF check policy.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


@dataclass
class ScoreView:
    """Read scores without materializing the node set in Python."""

    con: sqlite3.Connection
    table: str
    count: int
    median: float

    def score_for(self, key: str) -> float | None:
        row = self.con.execute(f"SELECT score FROM {self.table} WHERE node=?", (key,)).fetchone()
        return None if row is None else float(row[0])

    def close(self) -> None:
        """Release the score table after its consumer has emitted bounded output."""
        self.con.execute(f"DROP TABLE IF EXISTS temp.{self.table}")


def _name(value: str, label: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a generated SQLite identifier")
    return value


def _temp_table(con: sqlite3.Connection, name: str, label: str) -> None:
    if (
        con.execute(
            "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is None
    ):
        raise ValueError(f"{label} must name an existing TEMP table")


def _validate_parameters(damping: float, max_iterations: int, tolerance: float) -> None:
    if (
        type(max_iterations) is not int
        or max_iterations < 1
        or not isinstance(damping, (int, float))
        or not math.isfinite(float(damping))
        or not 0 <= float(damping) <= 1
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(float(tolerance))
        or float(tolerance) < 0
    ):
        raise ValueError("invalid PageRank damping, iteration limit, or tolerance")


def compute_scores(
    con: sqlite3.Connection,
    *,
    nodes_table: str,
    topology_table: str,
    prefix: str,
    damping: float,
    max_iterations: int,
    tolerance: float,
) -> ScoreView | None:
    """Calculate the existing PageRank model using TEMP tables only.

    ``nodes_table`` has ``node TEXT PRIMARY KEY`` and includes current pages
    plus every followed internal edge endpoint.  ``topology_table`` has
    ``seq INTEGER PRIMARY KEY, src_key TEXT, dst_key TEXT`` and contains only
    followed internal links.  A nonempty self-edge topology is intentional:
    it creates a valid dangling-node graph exactly as ``compute_link_scores``
    does after it ignores self links for distribution.
    """

    nodes_table = _name(nodes_table, "nodes_table")
    topology_table = _name(topology_table, "topology_table")
    prefix = _name(prefix, "prefix")
    _validate_parameters(damping, max_iterations, tolerance)
    _temp_table(con, nodes_table, "nodes_table")
    _temp_table(con, topology_table, "topology_table")

    score = f"{prefix}_score"
    next_score = f"{prefix}_next"
    outdegree = f"{prefix}_outdegree"
    contribution = f"{prefix}_contribution"
    index_source = f"{prefix}_topology_source"
    index_dest = f"{prefix}_topology_dest"
    owned = (score, next_score, outdegree, contribution, index_source, index_dest)
    if any(
        con.execute("SELECT 1 FROM sqlite_temp_master WHERE name=?", (name,)).fetchone() is not None
        for name in owned
    ):
        raise ValueError("score prefix already has temporary state")

    try:
        if con.execute(f"SELECT 1 FROM {topology_table} LIMIT 1").fetchone() is None:
            return None
        if (
            con.execute(
                f"SELECT 1 FROM {topology_table} AS edge "
                f"LEFT JOIN {nodes_table} AS source ON source.node=edge.src_key "
                f"LEFT JOIN {nodes_table} AS destination ON destination.node=edge.dst_key "
                "WHERE source.node IS NULL OR destination.node IS NULL LIMIT 1"
            ).fetchone()
            is not None
        ):
            raise ValueError("topology endpoint is absent from nodes_table")
        count = int(con.execute(f"SELECT COUNT(*) FROM {nodes_table}").fetchone()[0])
        if not count:
            return None

        con.execute(
            f"CREATE TEMP TABLE {score}(node TEXT PRIMARY KEY,score REAL NOT NULL) WITHOUT ROWID"
        )
        con.execute(
            f"CREATE TEMP TABLE {next_score}(node TEXT PRIMARY KEY,score REAL NOT NULL) WITHOUT ROWID"
        )
        con.execute(
            f"CREATE TEMP TABLE {outdegree}(node TEXT PRIMARY KEY,degree INTEGER NOT NULL) WITHOUT ROWID"
        )
        con.execute(
            f"CREATE TEMP TABLE {contribution}(node TEXT PRIMARY KEY,score REAL NOT NULL) WITHOUT ROWID"
        )
        con.execute(f"CREATE INDEX temp.{index_source} ON {topology_table}(src_key,seq)")
        con.execute(f"CREATE INDEX temp.{index_dest} ON {topology_table}(dst_key,seq)")
        con.execute(
            f"INSERT INTO {outdegree} "
            f"SELECT src_key,COUNT(*) FROM {topology_table} "
            "WHERE src_key<>dst_key GROUP BY src_key"
        )
        con.execute(f"INSERT INTO {score} SELECT node,? FROM {nodes_table}", (1.0 / count,))

        for _ in range(max_iterations):
            dangling = float(
                con.execute(
                    f"SELECT COALESCE(SUM(score.score),0) FROM {score} AS score "
                    f"LEFT JOIN {outdegree} AS degree ON degree.node=score.node "
                    "WHERE degree.degree IS NULL"
                ).fetchone()[0]
            )
            base = (1.0 - float(damping)) / count + float(damping) * dangling / count
            con.execute(f"DELETE FROM {next_score}")
            con.execute(f"INSERT INTO {next_score} SELECT node,? FROM {nodes_table}", (base,))
            con.execute(f"DELETE FROM {contribution}")
            con.execute(
                f"INSERT INTO {contribution} "
                f"SELECT edge.dst_key,SUM(?*score.score/degree.degree) "
                f"FROM {topology_table} AS edge "
                f"JOIN {score} AS score ON score.node=edge.src_key "
                f"JOIN {outdegree} AS degree ON degree.node=edge.src_key "
                "WHERE edge.src_key<>edge.dst_key GROUP BY edge.dst_key",
                (float(damping),),
            )
            con.execute(
                f"UPDATE {next_score} SET score=score+COALESCE("
                f"(SELECT contribution.score FROM {contribution} AS contribution "
                f"WHERE contribution.node={next_score}.node),0)"
            )
            delta = float(
                con.execute(
                    f"SELECT MAX(ABS(next.score-current.score)) FROM {next_score} AS next "
                    f"JOIN {score} AS current USING(node)"
                ).fetchone()[0]
            )
            con.execute(f"DELETE FROM {score}")
            con.execute(f"INSERT INTO {score} SELECT node,score FROM {next_score}")
            if delta < float(tolerance):
                break

        offset = (count - 1) // 2
        width = 1 if count % 2 else 2
        median = float(
            con.execute(
                f"SELECT AVG(score) FROM (SELECT score FROM {score} ORDER BY score,node LIMIT ? OFFSET ?)",
                (width, offset),
            ).fetchone()[0]
        )
        for name in (next_score, outdegree, contribution):
            con.execute(f"DROP TABLE temp.{name}")
        for name in (index_source, index_dest):
            con.execute(f"DROP INDEX temp.{name}")
        return ScoreView(con, score, count, median)
    except BaseException:
        for name in (next_score, outdegree, contribution, score):
            con.execute(f"DROP TABLE IF EXISTS temp.{name}")
        for name in (index_source, index_dest):
            con.execute(f"DROP INDEX IF EXISTS temp.{name}")
        raise
