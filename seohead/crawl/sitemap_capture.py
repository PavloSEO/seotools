"""Stream selected sitemap roots into already-declared scan context rows.

``sitemap_id`` always identifies the selected expanded root.  It does not
claim which nested XML document contained an individual ``<loc>`` entry.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from seohead.tools.sitemap import crawl

MEMBER_CHUNK_SIZE = 256


@dataclass(frozen=True)
class SourceRoot:
    sitemap_id: int
    url: str
    source: str


@dataclass(frozen=True)
class CaptureSummary:
    sitemap_id: int
    root: str
    count: int
    complete: bool
    reason: str
    errors: int
    truncated: bool


def capture_declared_roots(
    roots: Iterable[SourceRoot],
    *,
    write_sitemap_members: Callable[[int, list[tuple[int, str]]], None],
    finish_sitemap: Callable[[int, bool, str], None],
    emit_seed: Callable[[str, SourceRoot], None],
    read_sitemap_summary: Callable[[int], dict[str, Any] | None] | None = None,
    read_sitemap_members: Callable[[int], Iterable[tuple[int, str]]] | None = None,
    crawl_fn: Callable[..., dict[str, Any]] = crawl,
    concurrency: int = 3,
    request_gate: Callable[[], None] | None = None,
) -> list[CaptureSummary]:
    """Capture roots one at a time with root-local normalized deduplication.

    The small returned summary list is bounded by the existing sitemap-root
    cap. URL members move only through 256-entry writer/seed chunks.
    """
    summaries: list[CaptureSummary] = []
    global_ordinal = 0
    for root in roots:
        saved = read_sitemap_summary(root.sitemap_id) if read_sitemap_summary else None
        if saved and saved.get("complete"):
            if read_sitemap_members is None:
                raise ValueError("complete sitemap reuse requires a member reader")
            count = 0
            for ordinal, loc in read_sitemap_members(root.sitemap_id):
                global_ordinal = max(global_ordinal, ordinal + 1)
                emit_seed(loc, root)
                count += 1
            summaries.append(
                CaptureSummary(
                    sitemap_id=root.sitemap_id,
                    root=root.url,
                    count=count,
                    complete=True,
                    reason=str(saved.get("reason", "")),
                    errors=0,
                    truncated=False,
                )
            )
            continue

        previous_members = (
            iter(read_sitemap_members(root.sitemap_id))
            if saved and read_sitemap_members is not None
            else None
        )
        previous_tail = False
        # The root has been declared before publication. Mark it incomplete
        # before any network work so an interruption cannot look measured-clean.
        finish_sitemap(root.sitemap_id, False, "capture in progress")
        chunk: list[tuple[int, str]] = []

        def sink(
            entry: dict[str, Any],
            root: SourceRoot = root,
            chunk: list[tuple[int, str]] = chunk,
            previous_members: Iterator[tuple[int, str]] | None = previous_members,
        ) -> None:
            nonlocal global_ordinal
            if previous_members is not None:
                previous = next(previous_members, None)
                current = (global_ordinal, entry["loc"])
                if previous is not None and previous != current:
                    raise ValueError("sitemap replay prefix conflicts with saved membership")
            chunk.append((global_ordinal, entry["loc"]))
            global_ordinal += 1
            if len(chunk) == MEMBER_CHUNK_SIZE:
                write_sitemap_members(root.sitemap_id, chunk[:])
                for _ordinal, loc in chunk:
                    emit_seed(loc, root)
                chunk.clear()

        crawl_kwargs: dict[str, Any] = {"concurrency": concurrency, "sink": sink}
        # Existing injected crawl functions predate pacing and deliberately
        # accept only concurrency/sink.  Keep their narrow contract when no
        # shared budget is in play.
        if request_gate is not None:
            crawl_kwargs["request_gate"] = request_gate
        result = crawl_fn(root.url, **crawl_kwargs)
        if previous_members is not None and next(previous_members, None) is not None:
            previous_tail = True
        if chunk:
            write_sitemap_members(root.sitemap_id, chunk[:])
            for _ordinal, loc in chunk:
                emit_seed(loc, root)
        errors = result.get("errors") or []
        truncated = bool(result.get("truncated"))
        complete = bool(result.get("ok")) and not errors and not truncated and not previous_tail
        if errors:
            reason = str(errors[0].get("error", "sitemap capture failed"))
        elif truncated:
            reason = "sitemap URL limit reached"
        elif previous_tail:
            reason = "sitemap replay ended before saved membership prefix"
        else:
            reason = ""
        finish_sitemap(root.sitemap_id, complete, reason)
        summaries.append(
            CaptureSummary(
                sitemap_id=root.sitemap_id,
                root=str(result.get("root", root.url)),
                count=int(result.get("count", 0)),
                complete=complete,
                reason=reason,
                errors=len(errors),
                truncated=truncated,
            )
        )
        # Global member ordinals must stay stable across resume. If this root
        # is incomplete, later selected roots remain declared but unavailable;
        # fetching them would assign ordinals that collide when this root grows.
        if not complete:
            break
    return summaries
