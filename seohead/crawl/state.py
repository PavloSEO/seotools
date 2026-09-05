"""Durable crawl checkpoint: the frontier, seen-set and depth an interrupted
crawl needs to resume instead of restarting.

Plain JSON only, deliberately. A queue deserialised with pickle, marshal, or a
YAML loader with object tags is arbitrary code execution the moment the state
directory is writable by anything else — which a resumable crawl's state
directory usually is, sooner or later. A corrupt or hostile file here must
fail into "start fresh", never into "run whatever this file says": ``load``
only ever calls ``json.loads`` and never raises, so there is nothing in this
module a crafted file could make execute.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from dataclasses import dataclass, field
from typing import Any

# v2 adds forms and start_page_evidence (issue #188). v3 adds robots_blocked
# (issue #349). v4 adds accepted_seed_urls (issue #348 extension). A file from
# an older schema is rejected rather than read with the new field empty: a
# resumed crawl that silently drops a finding it had already produced is
# exactly the failure this bump prevents.
SCHEMA_VERSION = "crawl_state.v4"


@dataclass
class CrawlState:
    start_url: str
    queue: list[tuple[str, int]] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    max_depth_reached: int = 0
    # Fingerprint of the settings that change what the crawl fetches. A mismatch
    # means the frontier on disk was built under different rules than this
    # invocation is about to apply, so resuming would silently mix them.
    config_fingerprint: str = ""
    # Exclusion tally and per-path query-variant budget, checkpointed for the same
    # reason queue/seen are: both are accumulators the spider builds up while
    # walking the frontier, and both are cheap (a handful of counters and short
    # string sets) compared to the link graph, so there is no size reason to leave
    # them out the way links.jsonl is kept as a sidecar instead of inline here.
    # Losing the query budget specifically defeats it as a safety cap: it exists
    # to stop faceted/filter parameters from exploding a crawl, and a cap that
    # reopens on every resume stops capping anything.
    excluded: dict[str, int] = field(default_factory=dict)
    query_budget: dict[str, list[str]] = field(default_factory=dict)
    # Forms found before the checkpoint, and the start page's own evidence.
    # Both are produced only for pages fetched in the current invocation, so
    # without them here a resumed run reports completion while silently losing
    # a form finding it had already made and reversing the start page's
    # rendering verdict (issue #188). They are small next to the link graph --
    # a handful of forms, and one page's HTML -- so they stay inline rather
    # than becoming another sidecar the way links.jsonl had to.
    forms: list[dict[str, Any]] = field(default_factory=list)
    start_page_evidence: dict[str, Any] = field(default_factory=dict)
    # The report-only robots-block inventory (issue #349). It is crawl-wide
    # evidence, not per-invocation data: a completed report-only audit needs
    # every blocked URL ever seen, including ones fetched before this
    # checkpoint, or the resumed audit silently loses BLOCKED_BY_ROBOTS
    # findings the uninterrupted crawl would have kept.
    robots_blocked: list[str] = field(default_factory=list)
    # Sitemap (or other caller-supplied) seeds this crawl accepted into the
    # frontier, across the whole logical crawl, not just this invocation
    # (issue #348 extension). Without it, a resumed run re-evaluates an
    # already-accepted seed against ``seen``, finds it already there, and
    # skips appending it to ``result.seed_urls`` -- so a completed resumed
    # audit reports fewer (or zero) sitemap-seeded URLs than the identical
    # uninterrupted crawl, even though it fetched them.
    accepted_seed_urls: list[str] = field(default_factory=list)


def ensure_safe_dir(directory: str) -> None:
    """Refuse a world-writable state directory.

    A state directory only stays trustworthy if nothing else on the machine can
    write to it. World-writable turns "resume my crawl" into "load whatever the
    next process to touch this directory left behind".
    """
    os.makedirs(directory, exist_ok=True)
    mode = os.stat(directory).st_mode
    if mode & stat.S_IWOTH:
        raise PermissionError(f"refusing a world-writable crawl state directory: {directory}")


def load(path: str, start_url: str, config_fingerprint: str = "") -> tuple[CrawlState | None, str]:
    """Load a checkpoint, or say why not. Never raises.

    A missing, corrupt or hostile file, a schema mismatch, a different start
    URL, and a changed configuration all mean the same thing to a caller:
    start fresh. Only the note attached explains which.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return None, "no checkpoint found; starting fresh"
    except (OSError, ValueError):
        # ValueError also catches UnicodeDecodeError, which is what a file full
        # of binary garbage (or a pickle payload) produces here.
        return None, "checkpoint file is unreadable; starting fresh"
    if not isinstance(raw, dict):
        return None, "checkpoint file is not a JSON object; starting fresh"
    if raw.get("schema_version") != SCHEMA_VERSION:
        return None, (
            f"checkpoint schema is {raw.get('schema_version')!r}, "
            f"this build expects {SCHEMA_VERSION!r}; starting fresh"
        )
    if raw.get("start_url") != start_url:
        return None, "checkpoint is for a different start URL; starting fresh"
    if config_fingerprint and raw.get("config_fingerprint") != config_fingerprint:
        return None, "crawl scope or limits changed since the checkpoint; starting fresh"
    try:
        queue = [(str(u), int(d)) for u, d in raw.get("queue") or []]
        seen = [str(u) for u in raw.get("seen") or []]
        depth = int(raw.get("max_depth_reached") or 0)
        excluded = {str(k): int(v) for k, v in (raw.get("excluded") or {}).items()}
        query_budget = {
            str(path): [str(q) for q in variants]
            for path, variants in (raw.get("query_budget") or {}).items()
        }
        forms = [dict(entry) for entry in raw.get("forms") or []]
        start_page_evidence = dict(raw.get("start_page_evidence") or {})
        robots_blocked = [str(u) for u in raw.get("robots_blocked") or []]
        accepted_seed_urls = [str(u) for u in raw.get("accepted_seed_urls") or []]
    except (TypeError, ValueError, AttributeError):
        # AttributeError: excluded/query_budget present but not JSON objects
        # (e.g. a list), so ``.items()`` itself fails.
        return None, "checkpoint contents are malformed; starting fresh"
    state = CrawlState(
        start_url=start_url,
        queue=queue,
        seen=seen,
        max_depth_reached=depth,
        config_fingerprint=raw.get("config_fingerprint") or "",
        excluded=excluded,
        query_budget=query_budget,
        forms=forms,
        start_page_evidence=start_page_evidence,
        robots_blocked=robots_blocked,
        accepted_seed_urls=accepted_seed_urls,
    )
    return state, f"resuming from checkpoint: {len(queue)} URL(s) queued, {len(seen)} seen"


def save(path: str, state: CrawlState) -> None:
    """Write the checkpoint atomically so a crash mid-write cannot corrupt it."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "start_url": state.start_url,
        "queue": [[u, d] for u, d in state.queue],
        "seen": state.seen,
        "max_depth_reached": state.max_depth_reached,
        "config_fingerprint": state.config_fingerprint,
        "excluded": state.excluded,
        "query_budget": state.query_budget,
        "forms": state.forms,
        "start_page_evidence": state.start_page_evidence,
        "robots_blocked": state.robots_blocked,
        "accepted_seed_urls": state.accepted_seed_urls,
    }
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp_path, path)


def clear(path: str) -> None:
    """Remove a checkpoint once the crawl it describes is genuinely finished."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)
