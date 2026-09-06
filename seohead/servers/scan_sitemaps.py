"""Persist selected sitemap expansions before native collection starts."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from seohead.crawl.sitemap_capture import SourceRoot, capture_declared_roots
from seohead.storage import ScanError
from seohead.tools.sitemap import MAX_SITEMAPS, normalize_url


def initial_sitemaps(explicit: str | None) -> tuple[tuple[str, str], ...]:
    return ((normalize_url(explicit), "explicit"),) if explicit else ()


def load_sitemaps(
    scan: Any,
    emit_seeds: Callable[[Iterable[str]], None],
    *,
    settings: dict[str, Any],
    result: dict[str, Any],
) -> None:
    selected = scan.sitemap_roots()
    if settings["sitemaps"]["auto_discover"] and not any(
        root["source"] == "explicit" for root in selected
    ):
        robots = scan.read_context("robots_summary")
        if not robots or robots["fetch_state"] != "fetched":
            scan.note_audit_unavailable("robots sitemap discovery was unavailable")
            return
        # Replay the complete saved declaration sequence after an interruption
        # between root transactions. Exact retries are idempotent.
        # This set is bounded by the selected-root cap, never the graph or its URL members.
        selected_keys: set[str] = set()
        for url in robots["parsed"]["sitemaps"]:
            key = normalize_url(url)
            if key in selected_keys:
                continue
            if len(selected_keys) >= MAX_SITEMAPS:
                raise ScanError("selected sitemap root limit reached")
            selected_keys.add(key)
            scan.declare_sitemap(key, "robots", len(selected_keys) - 1)
        selected = scan.sitemap_roots()
    result.update(
        sitemap_url=selected[0]["url"] if selected else None,
        sitemap_urls=[root["url"] for root in selected],
        declared=[],  # Membership is in SQLite; there is no second URL-list owner.
    )
    pending: list[str] = []

    def seed(url: str, _root: SourceRoot) -> None:
        pending.append(url)
        if len(pending) >= 256:
            emit_seeds(pending)
            pending.clear()

    outcomes = capture_declared_roots(
        [SourceRoot(root["sitemap_url_id"], root["url"], root["source"]) for root in selected],
        write_sitemap_members=scan.write_sitemap_members,
        finish_sitemap=scan.finish_sitemap,
        emit_seed=seed,
        read_sitemap_summary=lambda sid: scan.read_context("sitemap_fetch_summary", f"url:{sid}"),
        read_sitemap_members=scan.iter_sitemap_members,
    )
    if pending:
        emit_seeds(pending)
    if any(not outcome.complete for outcome in outcomes):
        scan.interrupt("sitemap expansion is incomplete")
