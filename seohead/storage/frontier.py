"""Ordered frontier changes; callers supply the writer's active transaction."""

from __future__ import annotations

import hashlib
import itertools
from urllib.parse import urldefrag, urlsplit, urlunsplit

from . import ScanError, _dump, _insert, _url


def matching_frontier(con, requested_url):
    """Find the legacy fragment/empty-path identity without rewriting its request."""
    found = con.execute(
        "SELECT f.* FROM frontier f JOIN urls u USING(url_id) WHERE u.url=?", (requested_url,)
    ).fetchone()
    if found is not None:
        return found
    parts = urlsplit(requested_url)
    if parts.scheme.lower() not in {"http", "https"}:
        key = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        return con.execute(
            "SELECT f.* FROM frontier f JOIN urls u USING(url_id) WHERE u.url=?", (key,)
        ).fetchone()
    # The legacy key lowercases only the scheme, preserves netloc/path/query,
    # supplies '/' for an empty path, and drops the fragment. Scheme case has
    # at most 32 aliases. Index probes avoid a Python seen set or a table scan.
    aliases = []
    for chars in itertools.product(*[(char.lower(), char.upper()) for char in parts.scheme]):
        scheme = "".join(chars)
        for path in ("", "/") if parts.path in {"", "/"} else (parts.path,):
            # urlunsplit lowercases no provided scheme; preserve each explicit alias.
            aliases.append(urlunsplit((scheme, parts.netloc, path, parts.query, "")))
    clauses, params = [], []
    for alias in aliases:
        clauses.append("url=? OR (url>=? AND url<?)")
        params.extend((alias, alias + "#", alias + "$"))
    return con.execute(
        "SELECT * FROM frontier WHERE url_id IN (SELECT url_id FROM urls WHERE "
        + " OR ".join(clauses)
        + ") ORDER BY queue_ordinal LIMIT 1",
        params,
    ).fetchone()


def _frontier_row(con, url, depth, state):
    url_id = _url(con, url)
    ordinal = con.execute("SELECT COALESCE(MAX(queue_ordinal)+1,0) FROM frontier").fetchone()[0]
    con.execute(
        "INSERT INTO frontier(url_id,queue_ordinal,depth,state) VALUES(?,?,?,?)",
        (url_id, ordinal, depth, state),
    )
    return url_id


def _decision(con, url, reason, source, depth, key):
    _insert(
        con,
        "decisions",
        {
            "url": url,
            "reason": reason,
            "source": source,
            "depth": depth,
            "occurrence_key": key,
        },
    )


def _reserve_query(con, url, limit):
    if not limit:
        return True  # Legacy zero means unlimited, without a tracking map.
    parts = urlsplit(url)
    path, query = parts.path or "/", parts.query
    if con.execute(
        "SELECT 1 FROM query_variants WHERE path_key=? AND query_key=?", (path, query)
    ).fetchone():
        return True
    if (
        con.execute("SELECT COUNT(*) FROM query_variants WHERE path_key=?", (path,)).fetchone()[0]
        >= limit
    ):
        return False
    con.execute("INSERT INTO query_variants(path_key,query_key) VALUES(?,?)", (path, query))
    return True


def apply_candidates(con, candidates, *, source, queue_ordinal, limit):
    """Apply document-order discoveries, reserving queries before the seen test."""
    for index, item in enumerate(candidates):
        if not isinstance(item, dict) or set(item) != {
            "path_key",
            "query_key",
            "requested_url",
            "frontier_url",
            "depth",
        }:
            raise ScanError("candidate input has unknown or missing fields")
        url, requested, depth = item["frontier_url"], item["requested_url"], item["depth"]
        if (
            type(url) is not str
            or not url
            or type(requested) is not str
            or not requested
            or type(depth) is not int
            or depth < 0
        ):
            raise ScanError("candidate URL/depth is invalid")
        parts = urlsplit(url)
        if (parts.path or "/", parts.query) != (
            item["path_key"],
            item["query_key"],
        ) or url != urldefrag(requested).url:
            raise ScanError("candidate query/request identity disagrees")
        if not _reserve_query(con, url, limit):
            _decision(
                con,
                requested,
                "query_variants_limit",
                source,
                depth,
                f"{queue_ordinal}:candidate:{index}",
            )
            if matching_frontier(con, url) is None:
                _frontier_row(con, url, depth, "excluded")
        elif matching_frontier(con, url) is None:
            _frontier_row(con, url, depth, "queued")


def apply_seeds(con, entries, *, limit, start_url):
    """Seed chunks mark accepted and rejected identities once, before retries."""
    counts = {"queued": 0, "excluded": 0, "already_seen": 0}
    for item in entries:
        if not isinstance(item, dict) or set(item) != {
            "requested_url",
            "frontier_url",
            "depth",
            "reason",
            "source",
            "reserve_query",
            "seed",
        }:
            raise ScanError("seed input has unknown or missing fields")
        url, requested, reason = item["frontier_url"], item["requested_url"], item["reason"]
        if (
            any(
                type(item[key]) is not str
                for key in ("requested_url", "frontier_url", "reason", "source")
            )
            or not url
            or not requested
            or not item["source"]
            or type(item["depth"]) is not int
            or item["depth"] != 0
            or type(item["reserve_query"]) is not bool
            or type(item["seed"]) is not bool
        ):
            raise ScanError("seed scalar fields are invalid")
        if url != requested:
            raise ScanError("seed request text must preserve the original legacy identity")
        if not item["seed"] and (url != start_url or item["reserve_query"] or reason):
            raise ScanError("only the scan start bypasses sitemap seed checks")
        if matching_frontier(con, url) is not None:
            counts["already_seen"] += 1
            continue
        if not reason and item["reserve_query"] and not _reserve_query(con, url, limit):
            reason = "query_variants_limit"
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"}:
            if not reason:
                raise ScanError("an accepted seed must use HTTP(S)")
            # Rejected URIs have no request/PageRecord. Retain their exact text
            # in decisions and only their legacy seen identity in the frontier.
            url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
        url_id = _frontier_row(con, url, 0, "excluded" if reason else "queued")
        if reason:
            key = hashlib.sha256(requested.encode("utf-8")).hexdigest()
            _decision(con, requested, reason, item["source"], 0, f"seed:{key}")
            counts["excluded"] += 1
        else:
            counts["queued"] += 1
            if item["seed"]:
                _insert(
                    con,
                    "context_items",
                    {
                        "kind": "seed_url",
                        "item_key": f"url:{url_id}",
                        "payload_version": "scan_context.v1",
                        "payload_json": _dump({"url_id": url_id, "depth": 0, "source": "sitemap"}),
                        "completeness": "complete",
                        "reason": "accepted sitemap seed",
                    },
                )
    return counts
