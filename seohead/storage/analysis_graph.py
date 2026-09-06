"""SQL graph projections with injected normalization and check-independent facts."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from urllib.parse import urldefrag, urlsplit

from seohead.graph import AnchorGroup, InlinkCompositionRow

from .analysis_paths import PathSession
from .analysis_score import ScoreView, compute_scores


class AnalysisGraph:
    """Keep raw occurrences, calculation nodes and topology in TEMP SQLite."""

    has_resource_type = False

    def __init__(
        self, con: sqlite3.Connection, *, normalize: Callable[[str], str], site_host: str
    ) -> None:
        self.con = con
        self.normalize = normalize
        self.site_host = site_host.lower()
        self._prefix = f"f_graph_{id(self):x}"
        self._nodes = self._prefix + "_nodes"
        self._edges = self._prefix + "_edges"
        self._topology = self._prefix + "_topology"
        self._tables: list[str] = []
        self._ready = False
        self._closed = False
        self._scores_ready = False
        self._scores: ScoreView | None = None
        self._paths: PathSession | None = None
        self._prior_query_only = int(con.execute("PRAGMA query_only").fetchone()[0])
        if con.execute("PRAGMA temp_store").fetchone()[0] != 1:
            raise ValueError("stored graph requires file-backed temporary storage")
        if self._prior_query_only:
            con.execute("PRAGMA query_only=OFF")

    def _table(self, suffix: str, columns: str) -> str:
        name = self._prefix + suffix
        self.con.execute(f"CREATE TEMP TABLE {name} ({columns})")
        self._tables.append(name)
        return name

    def close(self) -> None:
        if self._closed:
            return
        try:
            if self._paths is not None:
                self._paths.close()
            if self._scores is not None:
                self._scores.close()
            for name in reversed(self._tables):
                self.con.execute(f"DROP TABLE IF EXISTS temp.{name}")
        finally:
            self.con.execute(f"PRAGMA query_only={self._prior_query_only}")
            self._closed = True

    def __enter__(self) -> AnalysisGraph:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _prepare(self) -> None:
        if self._closed:
            raise ValueError("stored graph is closed")
        if self._ready:
            return
        self._table("_nodes", "node TEXT PRIMARY KEY")
        self._table(
            "_edges",
            "seq INTEGER PRIMARY KEY, raw_src TEXT NOT NULL,raw_dst TEXT NOT NULL,"
            "src_key TEXT NOT NULL,dst_key TEXT NOT NULL,comp_key TEXT NOT NULL,"
            "anchor TEXT NOT NULL,position TEXT NOT NULL,nofollow INTEGER NOT NULL,"
            "internal INTEGER NOT NULL",
        )
        self._table(
            "_topology", "seq INTEGER PRIMARY KEY,src_key TEXT NOT NULL,dst_key TEXT NOT NULL"
        )
        for (raw,) in self.con.execute(
            "SELECT u.url FROM pages p JOIN urls u USING(url_id) ORDER BY p.page_ordinal"
        ):
            self.con.execute(
                f"INSERT OR IGNORE INTO {self._nodes} VALUES(?)", (self.normalize(raw.strip()),)
            )
        cursor = self.con.execute(
            "SELECT src.url,dst.url,l.anchor,l.position,l.nofollow FROM links l "
            "JOIN pages p ON p.url_id=l.source_url_id "
            "JOIN urls src ON src.url_id=l.source_url_id "
            "JOIN urls dst ON dst.url_id=l.destination_url_id "
            "ORDER BY p.page_ordinal,l.ordinal,l.link_id"
        )
        for seq, (source, destination, anchor, position, nofollow) in enumerate(cursor):
            # records_from_df strips surrounding whitespace and maps blank
            # strings to None. Store blanks here, project None at the boundary.
            source, destination = source.strip(), destination.strip()
            anchor, position = anchor.strip(), position.strip()
            src, dst = self.normalize(source), self.normalize(destination)
            host = urlsplit(destination).netloc.lower()
            internal = bool(source and destination and (not host or host == self.site_host))
            self.con.execute(
                f"INSERT INTO {self._edges} VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    seq,
                    source,
                    destination,
                    src,
                    dst,
                    urldefrag(dst)[0],
                    anchor,
                    position,
                    nofollow,
                    int(internal),
                ),
            )
            if internal and not nofollow:
                self.con.execute(f"INSERT INTO {self._topology} VALUES(?,?,?)", (seq, src, dst))
                self.con.execute(f"INSERT OR IGNORE INTO {self._nodes} VALUES(?)", (src,))
                self.con.execute(f"INSERT OR IGNORE INTO {self._nodes} VALUES(?)", (dst,))
        self.con.execute(
            f"CREATE INDEX {self._prefix}_composition_edges ON {self._edges}(comp_key,internal,seq)"
        )
        self._ready = True

    def iter_anchor_groups(
        self, is_generic_anchor: Callable[[str], bool], max_locations: int
    ) -> Iterator[AnchorGroup]:
        self._prepare()
        seen = self._table(
            "_anchor_seen",
            "src TEXT,dst TEXT,anchor_key TEXT,seq INTEGER NOT NULL,PRIMARY KEY(src,dst,anchor_key)",
        )
        for seq, source, destination, anchor in self.con.execute(
            f"SELECT seq,raw_src,raw_dst,anchor FROM {self._edges} ORDER BY seq"
        ):
            if source and anchor and is_generic_anchor(anchor):
                self.con.execute(
                    f"INSERT OR IGNORE INTO {seen} VALUES(?,?,?,?)",
                    (source, destination, anchor.lower(), seq),
                )
        for source, count, _first in self.con.execute(
            f"SELECT src,COUNT(*),MIN(seq) first FROM {seen} GROUP BY src ORDER BY first"
        ):
            shown = []
            locations = []
            for row in self.con.execute(
                f"SELECT e.raw_dst,e.anchor,e.position,e.nofollow FROM {seen} s "
                f"JOIN {self._edges} e USING(seq) WHERE s.src=? ORDER BY e.seq LIMIT ?",
                (source, max_locations),
            ):
                destination, anchor, position, nofollow = row
                shown.append(
                    {
                        "anchor": anchor,
                        "destination": destination or None,
                        "link_position": position or None,
                    }
                )
                locations.append(
                    {
                        "source_url": source,
                        "anchor": anchor,
                        "alt_text": None,
                        "link_position": position or None,
                        "link_path": None,
                        "follow": not bool(nofollow),
                        "rel": None,
                        "target": None,
                    }
                )
            yield AnchorGroup(source, count, shown, locations)

    @property
    def has_internal_hyperlinks(self) -> bool:
        self._prepare()
        return self.con.execute(f"SELECT 1 FROM {self._topology} LIMIT 1").fetchone() is not None

    def link_score(
        self, *, damping: float, max_iterations: int, tolerance: float
    ) -> ScoreView | None:
        self._prepare()
        if not self._scores_ready:
            self._scores = compute_scores(
                self.con,
                nodes_table=self._nodes,
                topology_table=self._topology,
                prefix=self._prefix,
                damping=damping,
                max_iterations=max_iterations,
                tolerance=tolerance,
            )
            self._scores_ready = True
        return self._scores

    def iter_inlink_composition(
        self, is_indexable_source: Callable[[str], bool | None], max_source_examples: int
    ) -> Iterator[InlinkCompositionRow]:
        self._prepare()
        sources = self._table("_source_facts", "raw_src TEXT PRIMARY KEY,indexable INTEGER")
        for (source,) in self.con.execute(
            f"SELECT DISTINCT raw_src FROM {self._edges} WHERE internal=1"
        ):
            measured = is_indexable_source(source)
            self.con.execute(
                f"INSERT INTO {sources} VALUES(?,?)",
                (source, None if measured is None else int(measured)),
            )
        cursor = self.con.execute(
            f"SELECT e.comp_key,COUNT(*),MIN(e.seq) first,MAX(e.nofollow=0),"
            f"MAX(s.indexable IS NOT NULL),MAX(s.indexable=1) FROM {self._edges} e "
            f"JOIN {sources} s USING(raw_src) WHERE e.internal=1 GROUP BY e.comp_key ORDER BY first"
        )
        for destination, count, _first, followed, known, indexable in cursor:
            examples = [
                row[0]
                for row in self.con.execute(
                    f"SELECT DISTINCT raw_src FROM {self._edges} WHERE comp_key=? AND internal=1 "
                    "ORDER BY raw_src COLLATE BINARY LIMIT ?",
                    (destination, max_source_examples),
                )
            ]
            yield InlinkCompositionRow(
                destination, count, not bool(followed), bool(known), bool(indexable), examples
            )

    def begin_paths(self, seed: str) -> PathSession | None:
        self._prepare()
        if self._paths is not None:
            self._paths.close()
        self._paths = PathSession.open(self.con, prefix=self._prefix, seed=seed)
        return self._paths

    def iter_resources(self) -> Iterator[tuple[str, str, str]]:
        return iter(())
