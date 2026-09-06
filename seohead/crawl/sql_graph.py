"""Streaming SQL projections over a validated native scan link graph."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from seohead.crawl import link_findings
from seohead.crawl.linkgraph import BOILERPLATE_POSITIONS
from seohead.crawl.spider import FormEdge, LinkEdge, _canonical_key
from seohead.storage.analysis_graph import selected_links_cte


@dataclass(frozen=True)
class CompositionMetadata:
    """Aggregate graph coverage without retaining a list of destinations."""

    edges_classified: int
    edges_unclassified: int
    edges_nonpage_destination: int
    pages_with_inlinks: int

    @property
    def measured(self) -> bool:
        return self.edges_classified > 0

    @property
    def classified_fraction(self) -> float:
        total = self.edges_classified + self.edges_unclassified
        return round(self.edges_classified / total, 4) if total else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "measured": self.measured,
            "edges_classified": self.edges_classified,
            "edges_unclassified": self.edges_unclassified,
            "edges_nonpage_destination": self.edges_nonpage_destination,
            "classified_fraction": self.classified_fraction,
            "pages_with_inlinks": self.pages_with_inlinks,
        }


class StoredGraph:
    """Cursor-only graph reader over a connection already validated by storage.

    Stored destination text remains the result key.  SQL joins to ``pages`` only
    choose the composition/inlink population; they never normalize or rewrite a
    LinkEdge destination.
    """

    def __init__(self, con: Any) -> None:
        self.con = con
        self._population_ready = False
        self._query_only_before: int | None = None

    def __enter__(self) -> StoredGraph:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        """Remove temporary eligibility state and restore the caller's pragma."""
        if not self._population_ready and self._query_only_before is None:
            return
        try:
            self.con.execute("DROP TABLE IF EXISTS temp.e_graph_destination_ids")
            self.con.execute("DROP TABLE IF EXISTS temp.e_graph_page_keys")
        finally:
            if self._query_only_before is not None:
                self.con.execute(f"PRAGMA query_only={self._query_only_before}")
            self._population_ready = False
            self._query_only_before = None

    def _ensure_composition_population(self) -> None:
        """Index canonical crawled-page identities without rewriting stored URLs.

        The main database can be opened URI read-only. SQLite permits these TEMP
        writes after query_only is disabled; no main-schema row or DDL is changed.
        """
        if self._population_ready:
            return
        self._query_only_before = self.con.execute("PRAGMA query_only").fetchone()[0]
        if self._query_only_before:
            self.con.execute("PRAGMA query_only=OFF")
        try:
            self.con.execute(
                "CREATE TEMP TABLE e_graph_page_keys(key TEXT PRIMARY KEY) WITHOUT ROWID"
            )
            self.con.execute(
                "CREATE TEMP TABLE e_graph_destination_ids(url_id INTEGER PRIMARY KEY) WITHOUT ROWID"
            )
            page_cursor = self.con.execute(
                "SELECT u.url FROM pages AS p JOIN urls AS u USING(url_id) ORDER BY p.page_ordinal"
            )
            for row in page_cursor:
                self.con.execute(
                    "INSERT OR IGNORE INTO e_graph_page_keys(key) VALUES(?)",
                    (_canonical_key(row["url"]),),
                )
            url_cursor = self.con.execute("SELECT url_id, url FROM urls ORDER BY url_id")
            for row in url_cursor:
                key = _canonical_key(row["url"])
                if self.con.execute(
                    "SELECT 1 FROM e_graph_page_keys WHERE key=?", (key,)
                ).fetchone():
                    self.con.execute(
                        "INSERT OR IGNORE INTO e_graph_destination_ids(url_id) VALUES(?)",
                        (row["url_id"],),
                    )
            self._population_ready = True
        except BaseException:
            self.close()
            raise

    def iter_links(self) -> Iterator[LinkEdge]:
        cursor = self.con.execute(
            selected_links_cte("l") + "SELECT l.*, src.url AS source, dst.url AS destination "
            "FROM l "
            "JOIN pages AS source_page ON source_page.url_id=l.source_url_id "
            "JOIN urls AS src ON src.url_id=l.source_url_id "
            "JOIN urls AS dst ON dst.url_id=l.destination_url_id "
            "ORDER BY source_page.page_ordinal, l.ordinal, l.link_id"
        )
        for row in cursor:
            yield LinkEdge(
                source=row["source"],
                destination=row["destination"],
                anchor=row["anchor"],
                nofollow=bool(row["nofollow"]),
                position=row["position"],
                rel=tuple(json.loads(row["rel_json"])),
                target=row["target"],
                raw_href=row["raw_href"],
            )

    def iter_forms(self) -> Iterator[FormEdge]:
        cursor = self.con.execute(
            "SELECT page_url.url AS page, f.method, f.action, f.has_password "
            "FROM forms AS f "
            "JOIN pages AS parent ON parent.url_id=f.page_url_id "
            "JOIN urls AS page_url ON page_url.url_id=f.page_url_id "
            "ORDER BY parent.page_ordinal, f.evidence_representation, f.ordinal, f.form_id"
        )
        for row in cursor:
            yield FormEdge(
                page=row["page"],
                method=row["method"],
                action=row["action"],
                has_password=bool(row["has_password"]),
            )

    def iter_inlink_counts(self) -> Iterator[dict[str, Any]]:
        """Count raw destination occurrences only where the destination is a page."""
        cursor = self.con.execute(
            selected_links_cte("l") + "SELECT dst.url AS url, COUNT(*) AS inlinks, "
            "COUNT(DISTINCT l.source_url_id) AS unique_inlinks "
            "FROM l "
            "JOIN pages AS destination_page ON destination_page.url_id=l.destination_url_id "
            "JOIN urls AS dst ON dst.url_id=l.destination_url_id "
            "GROUP BY l.destination_url_id, dst.url ORDER BY dst.url COLLATE BINARY"
        )
        for row in cursor:
            yield {
                "url": row["url"],
                "inlinks": row["inlinks"],
                "unique_inlinks": row["unique_inlinks"],
            }

    def composition_metadata(self) -> CompositionMetadata:
        self._ensure_composition_population()
        classified = self.con.execute(
            selected_links_cte("l") + ", classified AS ("
            "SELECT DISTINCT l.destination_url_id, l.source_url_id, l.position "
            "FROM l JOIN e_graph_destination_ids AS p ON p.url_id=l.destination_url_id "
            "WHERE l.position <> ''"
            ") SELECT COUNT(*) AS edges, COUNT(DISTINCT destination_url_id) AS pages FROM classified"
        ).fetchone()
        unclassified = self.con.execute(
            selected_links_cte("l") + "SELECT COUNT(*) FROM l "
            "JOIN e_graph_destination_ids AS p ON p.url_id=l.destination_url_id WHERE l.position=''"
        ).fetchone()[0]
        nonpage = self.con.execute(
            selected_links_cte("l") + "SELECT COUNT(*) FROM l "
            "LEFT JOIN e_graph_destination_ids AS p ON p.url_id=l.destination_url_id "
            "WHERE p.url_id IS NULL"
        ).fetchone()[0]
        return CompositionMetadata(
            edges_classified=classified["edges"],
            edges_unclassified=unclassified,
            edges_nonpage_destination=nonpage,
            pages_with_inlinks=classified["pages"],
        )

    def iter_composition_rows(self) -> Iterator[dict[str, Any]]:
        """Yield one raw page destination at a time with DISTINCT source/position counts."""
        self._ensure_composition_population()
        cursor = self.con.execute(
            selected_links_cte("l") + ", classified AS ("
            "SELECT DISTINCT l.destination_url_id, l.source_url_id, l.position "
            "FROM l JOIN e_graph_destination_ids AS p ON p.url_id=l.destination_url_id "
            "WHERE l.position <> ''"
            ") "
            "SELECT dst.url AS url, classified.position, COUNT(*) AS count "
            "FROM classified JOIN urls AS dst ON dst.url_id=classified.destination_url_id "
            "GROUP BY classified.destination_url_id, dst.url, classified.position "
            "ORDER BY dst.url COLLATE BINARY, classified.position COLLATE BINARY"
        )
        current_url: str | None = None
        counts: dict[str, int] = {}
        for row in cursor:
            url = row["url"]
            if current_url is not None and url != current_url:
                yield self._composition_row(current_url, counts)
                counts = {}
            current_url = url
            counts[row["position"]] = row["count"]
        if current_url is not None:
            yield self._composition_row(current_url, counts)

    @staticmethod
    def _composition_row(url: str, counts: dict[str, int]) -> dict[str, Any]:
        total = sum(counts.values())
        boilerplate = sum(counts.get(position, 0) for position in BOILERPLATE_POSITIONS)
        return {
            "url": url,
            "inlinks_total": total,
            "by_position": dict(sorted(counts.items())),
            "boilerplate_only": bool(counts) and boilerplate == total,
        }

    def iter_localhost_findings(self) -> Iterator[dict[str, Any]]:
        for edge in self.iter_links():
            yield from link_findings.outlinks_to_localhost([edge])

    def iter_follow_and_nofollow(self, host: str) -> Iterator[str]:
        """Stream one raw destination at a time; a port stays part of its raw key."""
        host = host.lower()
        cursor = self.con.execute(
            selected_links_cte("l") + "SELECT dst.url AS destination, l.nofollow FROM l "
            "JOIN urls AS dst ON dst.url_id=l.destination_url_id "
            "ORDER BY dst.url COLLATE BINARY, l.link_id"
        )
        current: str | None = None
        flags: set[bool] = set()
        for row in cursor:
            destination = row["destination"]
            if current is not None and destination != current:
                if flags == {True, False}:
                    yield current
                flags = set()
            current = destination
            if (urlsplit(destination).hostname or "").lower() == host:
                flags.add(bool(row["nofollow"]))
        if current is not None and flags == {True, False}:
            yield current

    def iter_unsafe_cross_origin(self) -> Iterator[dict[str, Any]]:
        for edge in self.iter_links():
            yield from link_findings.unsafe_cross_origin_links([edge])

    def iter_protocol_relative(self) -> Iterator[dict[str, Any]]:
        for edge in self.iter_links():
            yield from link_findings.protocol_relative_links([edge])

    def iter_insecure_forms(self) -> Iterator[dict[str, Any]]:
        for form in self.iter_forms():
            yield from link_findings.form_url_insecure([form])

    def iter_password_forms_on_http(self) -> Iterator[dict[str, Any]]:
        for form in self.iter_forms():
            yield from link_findings.forms_on_http_pages_with_password([form])
