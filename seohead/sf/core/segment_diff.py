"""Cross-segment counterpart diff (#358): which pages in a source segment have a
counterpart in a target segment, and which do not.

Pure -- no I/O, no network, and it never imports ``seohead.crawl``. The
analyzer/collector boundary this toolkit otherwise holds (``seohead.crawl``
gathers evidence, ``seohead.sf`` judges it, only the interface layer sees
both) applies here too: everything this module needs from the crawl's own
scope machinery -- which segment a URL belongs to, and why a URL would be
rejected from the crawl -- is handed in as plain callables built by the
caller from ``seohead.crawl.spider.Scope``, not imported.

The trap this module exists to avoid: a properly translated slug
(``/fr/a-propos`` for ``/en/about``) has no mirrored path, so a path-based
diff calls it missing -- a confident wrong finding landing on exactly the
sites that localised correctly. The site's own hreflang declaration (kept
since #357) is the authority; a mirrored path is only a labelled fallback,
and only once the mirror rate on *this* site has been measured high enough
to trust it (see ``MIRROR_RATE_THRESHOLD``).

Every eligible page in the source segment lands in exactly one of five
classes, so the five counts always sum to the eligible count -- the same
"never silently drop something" convention ``sf.core.compare`` and
``sf.core.segments`` already hold:

    declared               hreflang names the counterpart, and it was crawled
    declared_not_crawled   hreflang names a counterpart the crawl never reached
    inferred                no hreflang, but the mirrored path exists (and inference is on)
    absent                  no counterpart found, by either method
    undetermined            no method could safely answer -- named reason attached

"absent" is a negative claim and is refused whenever it cannot be supported:
a target segment the crawl only partially reached, or one excluded by
``segments_only``, yields no "absent" pages at all -- they become
"undetermined" instead, naming why.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from seohead.tools.parser import robots_directives

SCHEMA_VERSION = "segment_diff.v1"

# Below this share of declared pairs whose paths actually match, path
# inference is switched off entirely rather than trusted on partial evidence
# (#358's acceptance criterion). Chosen high on purpose: the whole point of
# measuring is to only infer on a site that is overwhelmingly path-mirrored,
# not merely path-mirrored more often than not.
MIRROR_RATE_THRESHOLD = 0.90

CLASSES = (
    "declared",
    "declared_not_crawled",
    "inferred",
    "absent",
    "undetermined",
)


class SegmentDiffError(ValueError):
    """The diff cannot be computed as asked, rather than answered wrong."""


@dataclass(frozen=True)
class SegmentDef:
    """The matching rule for one segment, as plain data -- just enough to build a
    mirrored-path candidate, not the crawl's own ``Scope.segment_for`` decision
    (that stays a callable the caller passes in, see module docstring)."""

    name: str
    prefix: str = ""
    host: str = ""
    pattern: str = ""


def _coerce_def(raw: Mapping[str, Any] | SegmentDef) -> SegmentDef:
    if isinstance(raw, SegmentDef):
        return raw
    return SegmentDef(
        name=raw["name"],
        prefix=raw.get("prefix") or "",
        host=(raw.get("host") or "").lower(),
        pattern=raw.get("pattern") or "",
    )


def _content_type(page: Mapping[str, Any]) -> str:
    metrics = page.get("metrics")
    return str(
        page.get("content_type")
        or (metrics.get("content_type") if isinstance(metrics, Mapping) else None)
        or ""
    )


def _metrics_get(page: Mapping[str, Any], field: str) -> Any:
    if field in page:
        return page[field]
    metrics = page.get("metrics")
    if isinstance(metrics, Mapping):
        return metrics.get(field)
    return None


def _has_hreflang_field(page: Mapping[str, Any]) -> bool:
    if "hreflang" in page:
        return True
    metrics = page.get("metrics")
    return isinstance(metrics, Mapping) and "hreflang" in metrics


def _hreflang_of(page: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(_metrics_get(page, "hreflang") or [])


def _is_eligible(page: Mapping[str, Any]) -> bool:
    """HTML, fetched successfully, and not withheld from indexing -- the same
    population ``AuditContext.indexable_html_pages`` counts, computed here from
    the raw fields directly so this module works on any page schema that
    carries them, not only one that already precomputed ``indexability``."""
    if "html" not in _content_type(page).lower():
        return False
    status = page.get("status_code")
    if status is None or not (200 <= int(status) < 300):
        return False
    directives = robots_directives(
        _metrics_get(page, "meta_robots"), _metrics_get(page, "x_robots")
    )
    return "noindex" not in directives


def _declared_alternate(
    page: Mapping[str, Any], target: str, segment_for: Callable[[str], str]
) -> str | None:
    for entry in _hreflang_of(page):
        url = (entry.get("url") or entry.get("raw_href") or "").strip() if entry else ""
        if url and segment_for(url) == target:
            return url
    return None


def _mirror(url: str, source_def: SegmentDef, target_def: SegmentDef) -> str | None:
    """The candidate counterpart URL under a naive path- or host-swap, or ``None``
    when neither segment is defined in a way that makes a mirror computable (a
    regex-only segment names a shape, not a substitution)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if source_def.prefix and target_def.prefix and parts.path.startswith(source_def.prefix):
        new_path = target_def.prefix + parts.path[len(source_def.prefix) :]
        netloc = target_def.host or parts.netloc
        return urlunsplit((parts.scheme, netloc, new_path, parts.query, parts.fragment))
    if source_def.host and target_def.host and host == source_def.host:
        netloc = target_def.host if not parts.port else f"{target_def.host}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return None


def _measure_mirror_rate(
    source_pages: Iterable[Mapping[str, Any]],
    target: str,
    segment_for: Callable[[str], str],
    source_def: SegmentDef,
    target_def: SegmentDef,
) -> tuple[float | None, int]:
    """The share of declared source->target pairs whose declared counterpart
    sits exactly at the naive mirrored path -- measured on this site's own
    declarations, not assumed. ``None`` means there were no declared pairs to
    measure from at all, which is treated the same as "measured low": no
    evidence is not evidence that inference is safe."""
    total = 0
    matching = 0
    for page in source_pages:
        alt = _declared_alternate(page, target, segment_for)
        if alt is None:
            continue
        total += 1
        if _mirror(page.get("url", ""), source_def, target_def) == alt:
            matching += 1
    if total == 0:
        return None, 0
    return matching / total, total


def diff_segments(
    pages: Iterable[Mapping[str, Any]],
    *,
    source: str,
    target: str,
    segments: Iterable[Mapping[str, Any] | SegmentDef],
    segment_for: Callable[[str], str],
    rejection: Callable[[str], str] | None = None,
    segments_only: Collection[str] = (),
    crawl_partial: bool = False,
) -> dict[str, Any]:
    """Classify every eligible page of ``source`` against ``target``.

    ``segment_for`` and ``rejection`` are ``Scope.segment_for`` /
    ``Scope.rejection`` (see ``seohead.crawl.spider``), handed in by the
    caller rather than imported here. ``segments`` is the plain rule data
    behind those segments (name/prefix/host/pattern) -- enough to build a
    mirrored-path candidate without needing the crawl module itself.
    """
    seg_defs = [_coerce_def(s) for s in segments]
    by_name = {s.name: s for s in seg_defs}
    if not by_name:
        raise SegmentDiffError("no segments declared anywhere; scope.segments is empty")
    if source == target:
        raise SegmentDiffError(f"source and target segment are both {source!r}")
    if source not in by_name:
        raise SegmentDiffError(f"unknown source segment {source!r}; declared: {sorted(by_name)}")
    if target not in by_name:
        raise SegmentDiffError(f"unknown target segment {target!r}; declared: {sorted(by_name)}")

    all_pages = list(pages)
    if all_pages and not any(_has_hreflang_field(p) for p in all_pages):
        raise SegmentDiffError(
            "page schema carries no hreflang field at all; the cross-segment diff depends "
            "on #357's declared alternates and cannot classify anything without them"
        )

    by_url = {p["url"]: p for p in all_pages if p.get("url")}
    source_pages = [p for p in all_pages if segment_for(p.get("url", "")) == source]
    source_eligible = [p for p in source_pages if _is_eligible(p)]

    source_def, target_def = by_name[source], by_name[target]
    mirror_rate, mirror_pairs = _measure_mirror_rate(
        source_pages, target, segment_for, source_def, target_def
    )
    if mirror_rate is None:
        inference_enabled = False
        mirror_reason = (
            "no declared hreflang pairs from "
            f"{source!r} to {target!r} to measure a mirror rate from; path inference stays off"
        )
    elif mirror_rate < MIRROR_RATE_THRESHOLD:
        inference_enabled = False
        mirror_reason = (
            f"measured mirror rate {mirror_rate:.0%} over {mirror_pairs} declared pair(s), "
            f"below the {MIRROR_RATE_THRESHOLD:.0%} threshold; path inference is switched off"
        )
    else:
        inference_enabled = True
        mirror_reason = (
            f"measured mirror rate {mirror_rate:.0%} over {mirror_pairs} declared pair(s); "
            "path inference is enabled"
        )

    target_unreachable_reason: str | None = None
    if segments_only and target not in set(segments_only):
        target_unreachable_reason = (
            f"target segment {target!r} is excluded from this crawl by scope.segments_only; "
            "absence cannot be confirmed"
        )
    elif crawl_partial:
        target_unreachable_reason = (
            "the crawl is partial and may not have fully reached the target segment; "
            "absence cannot be confirmed"
        )

    counts = dict.fromkeys(CLASSES, 0)
    rows: list[dict[str, Any]] = []
    for page in source_eligible:
        url = page.get("url", "")
        alt = _declared_alternate(page, target, segment_for)
        if alt is not None:
            if alt in by_url:
                cls, method, counterpart, reason = "declared", "hreflang", alt, None
            else:
                cls, method, counterpart = "declared_not_crawled", "hreflang", alt
                rej = rejection(alt) if rejection else ""
                reason = f"hreflang names {alt!r} but it was not found among crawled pages" + (
                    f" (would be rejected: {rej})" if rej else ""
                )
        elif not inference_enabled:
            cls, method, counterpart, reason = (
                "undetermined",
                "mirror_disabled",
                None,
                mirror_reason,
            )
        else:
            candidate = _mirror(url, source_def, target_def)
            if candidate is None:
                cls, method, counterpart = "undetermined", "mirror_unavailable", None
                reason = (
                    f"cannot construct a mirrored-path candidate between {source!r} and "
                    f"{target!r} (at least one is defined only by a regex)"
                )
            elif candidate in by_url and segment_for(candidate) == target:
                cls, method, counterpart, reason = "inferred", "mirrored_path", candidate, None
            elif target_unreachable_reason:
                cls, method, counterpart = "undetermined", "target_unreachable", candidate
                reason = target_unreachable_reason
            else:
                cls, method, counterpart, reason = "absent", "mirrored_path", candidate, None

        counts[cls] += 1
        rows.append(
            {
                "url": url,
                "class": cls,
                "method": method,
                "counterpart": counterpart,
                "reason": reason,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "target": target,
        "eligible_pages": len(source_eligible),
        "mirror_rate": mirror_rate,
        "mirror_pairs": mirror_pairs,
        "inference_enabled": inference_enabled,
        "mirror_rate_reason": mirror_reason,
        "counts": counts,
        "pages": rows,
    }
