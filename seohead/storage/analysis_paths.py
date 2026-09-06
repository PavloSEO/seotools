"""Disk-backed shortest discovery paths over a caller-provided TEMP topology."""

from __future__ import annotations

import re
import sqlite3

from . import ScanError

_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,48}\Z")


def _name(prefix: str, suffix: str) -> str:
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise ScanError("path-session prefix must be a generated SQL identifier")
    return f'"{prefix}_{suffix}"'


class PathSession:
    """A lazy FIFO BFS whose topology, queue, and predecessors stay in TEMP tables."""

    def __init__(self, con: sqlite3.Connection, *, prefix: str, seed: str) -> None:
        self._con = con
        self._prefix = prefix
        self._seed = seed
        self._topology = _name(prefix, "topology")
        self._frontier = _name(prefix, "path_frontier")
        self._predecessor = _name(prefix, "path_predecessor")
        self._topology_index = f'"{prefix}_path_topology_src_seq"'
        self._closed = False

    @classmethod
    def open(cls, con: sqlite3.Connection, *, prefix: str, seed: str) -> PathSession | None:
        """Open a session for a validated TEMP topology, or return ``None`` when it is empty."""
        if not isinstance(con, sqlite3.Connection):
            raise ScanError("path session requires a sqlite3 connection")
        if not isinstance(seed, str) or not seed:
            raise ScanError("path session seed must be a normalized nonempty URL key")

        topology = _name(prefix, "topology")
        columns = {row[1]: row for row in con.execute(f"PRAGMA table_info({topology})")}
        if not {"seq", "src_key", "dst_key"} <= set(columns):
            raise ScanError("path session requires a TEMP topology with seq, src_key, dst_key")
        if con.execute(f"SELECT COUNT(*) FROM {topology}").fetchone()[0] == 0:
            return None
        if con.execute(
            f"SELECT 1 FROM {topology} "
            "WHERE seq IS NULL OR src_key IS NULL OR dst_key IS NULL "
            "OR typeof(seq) != 'integer' OR typeof(src_key) != 'text' "
            "OR typeof(dst_key) != 'text' LIMIT 1"
        ).fetchone():
            raise ScanError("path topology contains an invalid edge row")
        if con.execute(
            f"SELECT 1 FROM {topology} GROUP BY seq HAVING COUNT(*) != 1 LIMIT 1"
        ).fetchone():
            raise ScanError("path topology edge sequence must be unique")

        session = cls(con, prefix=prefix, seed=seed)
        con.execute(
            f"CREATE TEMP TABLE {session._frontier} ("
            "key TEXT PRIMARY KEY, queue_order INTEGER NOT NULL UNIQUE, done INTEGER NOT NULL)"
        )
        con.execute(
            f"CREATE TEMP TABLE {session._predecessor} ("
            "key TEXT PRIMARY KEY, parent_key TEXT, depth INTEGER NOT NULL)"
        )
        con.execute(f"CREATE INDEX {session._topology_index} ON {topology}(src_key, seq)")
        con.execute(
            f"INSERT INTO {session._frontier}(key, queue_order, done) VALUES(?, 0, 0)",
            (seed,),
        )
        con.execute(
            f"INSERT INTO {session._predecessor}(key, parent_key, depth) VALUES(?, NULL, 0)",
            (seed,),
        )
        return session

    def path_to(self, target: str) -> tuple[str, ...] | None:
        """Return the legacy-equivalent shortest path for one target, if reachable."""
        if self._closed:
            raise ScanError("path session is closed")
        if not isinstance(target, str) or not target:
            raise ScanError("path target must be a normalized nonempty URL key")
        while not self._known(target):
            row = self._con.execute(
                f"SELECT key FROM {self._frontier} WHERE done=0 ORDER BY queue_order LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._expand(row[0])
        return self._materialize(target)

    def _known(self, key: str) -> bool:
        return (
            self._con.execute(f"SELECT 1 FROM {self._predecessor} WHERE key=?", (key,)).fetchone()
            is not None
        )

    def _expand(self, source: str) -> None:
        self._con.execute(f"UPDATE {self._frontier} SET done=1 WHERE key=?", (source,))
        depth = self._con.execute(
            f"SELECT depth FROM {self._predecessor} WHERE key=?", (source,)
        ).fetchone()[0]
        next_order = self._con.execute(
            f"SELECT COALESCE(MAX(queue_order)+1, 0) FROM {self._frontier}"
        ).fetchone()[0]
        # This cursor is the only edge materialization.  FIFO source expansion,
        # then seq order, gives the same first-parent tie rule as the legacy BFS.
        for (destination,) in self._con.execute(
            f"SELECT dst_key FROM {self._topology} WHERE src_key=? ORDER BY seq", (source,)
        ):
            inserted = self._con.execute(
                f"INSERT OR IGNORE INTO {self._predecessor}(key, parent_key, depth) VALUES(?, ?, ?)",
                (destination, source, depth + 1),
            ).rowcount
            if inserted:
                self._con.execute(
                    f"INSERT INTO {self._frontier}(key, queue_order, done) VALUES(?, ?, 0)",
                    (destination, next_order),
                )
                next_order += 1

    def _materialize(self, target: str) -> tuple[str, ...]:
        path: list[str] = []
        current: str | None = target
        while current is not None:
            path.append(current)
            row = self._con.execute(
                f"SELECT parent_key FROM {self._predecessor} WHERE key=?", (current,)
            ).fetchone()
            if row is None:
                raise ScanError("path predecessor chain is inconsistent")
            current = row[0]
        path.reverse()
        return tuple(path)

    def close(self) -> None:
        if self._closed:
            return
        self._con.execute(f"DROP TABLE {self._frontier}")
        self._con.execute(f"DROP TABLE {self._predecessor}")
        self._con.execute(f"DROP INDEX {self._topology_index}")
        self._closed = True

    def __enter__(self) -> PathSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
