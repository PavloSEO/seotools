"""Link localization from ``*:Inlinks`` bulk exports.

This is the module that answers the core request: a broken link — *where it
sits* (source page), *where it goes* (destination), *where in the DOM*
(Link Position + Link Path/XPath), and *on how many pages*. One issue per
destination URL, with every source as a location.
"""

from __future__ import annotations

import re
import statistics
import urllib.parse
from collections import Counter, OrderedDict
from typing import Any

from seohead.graph import InlinkCompositionRow
from seohead.tools.hreflang import code_error

from .context import AuditContext
from .crawl_path import shortest_paths_from_seed
from .link_score import (
    DEFAULT_DAMPING,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_TOLERANCE,
    compute_link_scores,
)
from .models import Link
from .normalize import HREFLANG_FIELD_MAP, INLINKS_FIELD_MAP, is_true, norm_url, records_from_df

# logical export key -> (internal-destination check, external-destination check).
# The *:Inlinks exports include links to BOTH internal and external destinations;
# we split by host so an external 404 isn't mislabeled as an internal one.
INLINK_SOURCES = {
    "inlinks_4xx": ("BROKEN_INTERNAL_LINK", "BROKEN_EXTERNAL_LINK"),
    "inlinks_5xx": ("LINK_TO_5XX", "BROKEN_EXTERNAL_LINK"),
    "inlinks_3xx": ("INTERNAL_LINK_TO_REDIRECT", "EXTERNAL_LINK_TO_REDIRECT"),
}


def _site_host(ctx: AuditContext) -> str:
    counts: Counter = Counter()
    for page in ctx.pages:
        host = urllib.parse.urlparse(page.url).netloc.lower()
        if host:
            counts[host] += 1
    return counts.most_common(1)[0][0] if counts else ""


def _all_inlink_records(ctx: AuditContext) -> list[dict[str, Any]] | None:
    """The complete inlink inventory, or ``None`` when it was never exported.

    ``all_inlinks`` (Bulk Export → Links → All Inlinks) is the only export that
    carries every link on the site rather than only the broken ones, so it is
    the one input the whole-graph passes below (link score, inlink
    composition, the discovery path, and the resource inventory) all share.
    """
    df = ctx.exports.get("all_inlinks")
    if df is None or df.empty:
        return None
    return records_from_df(df, INLINKS_FIELD_MAP)


def _graph_access(ctx: AuditContext):
    """The optional native backend; export behavior remains the default."""
    return getattr(ctx, "graph_access", None)


def _link_from_record(rec: dict[str, Any]) -> Link:
    follow_raw = rec.get("follow")
    return Link(
        source_url=rec.get("source_url"),
        destination_url=rec.get("destination_url"),
        anchor=rec.get("anchor"),
        alt_text=rec.get("alt_text"),
        status_code=rec.get("status_code"),
        link_position=rec.get("link_position"),
        link_path=rec.get("link_path"),
        follow=is_true(follow_raw) if follow_raw is not None else None,
        rel=rec.get("rel"),
        target=rec.get("target"),
    )


def _process_export(
    ctx: AuditContext, key: str, internal_check: str, external_check: str, site_host: str
) -> None:
    df = ctx.exports.get(key)
    if df is None or df.empty:
        ctx.skip(internal_check, f"export {key} not available")
        ctx.skip(external_check, f"export {key} not available")
        return

    max_locs = ctx.config.get("output", {}).get("max_locations_per_issue", 200)
    by_dest: OrderedDict[str, list[Link]] = OrderedDict()
    raw_dests: dict[str, list[str]] = {}
    for rec in records_from_df(df, INLINKS_FIELD_MAP):
        link = _link_from_record(rec)
        if not link.destination_url:
            continue
        dest_key = norm_url(link.destination_url)
        by_dest.setdefault(dest_key, []).append(link)
        raws = raw_dests.setdefault(dest_key, [])
        if link.destination_url not in raws:
            raws.append(link.destination_url)

    for dest_key, links in by_dest.items():
        # Group occurrences by normalized identity, but report the actual
        # crawled spelling whenever it is known. redirect_map is keyed by the
        # Internal:All Address spelling, so first-seen raw inlink text would
        # make both target_url and final_url depend on CSV row order.
        crawled = ctx.page_by_norm.get(dest_key)
        dest = (
            crawled.url
            if crawled is not None
            else sorted(raw_dests[dest_key], key=lambda value: (value.casefold(), value))[0]
        )
        dest_host = urllib.parse.urlparse(dest).netloc.lower()
        is_internal = (not dest_host) or (dest_host == site_host)
        check_id = internal_check if is_internal else external_check
        status = next((link.status_code for link in links if link.status_code), None)
        details: dict[str, Any] = {
            "link_position_breakdown": _position_breakdown(links),
            "destination_scope": "internal" if is_internal else "external",
        }
        if check_id == "INTERNAL_LINK_TO_REDIRECT":
            final = ctx.redirect_map.get(dest)
            if final:
                details["final_url"] = final
        locations = [link.as_location() for link in links[:max_locs]]
        # Count distinct source pages, not raw link occurrences. This answers
        # "on how many pages does this link appear?" when a page repeats it.
        n_sources = len({link.source_url for link in links if link.source_url})
        ctx.add(
            check_id,
            target_url=dest,
            status_code=status,
            occurrences_count=n_sources or len(links),
            locations=locations,
            details=details,
            evidence={
                "export": ctx.exports.files.get(key),
                "raw_destinations": raw_dests[dest_key],
            },
        )


def _position_breakdown(links: list[Link]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for link in links:
        pos = link.link_position or "Unknown"
        counts[pos] = counts.get(pos, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Generic anchor text
# ---------------------------------------------------------------------------
# Curated Russian-and-English dictionary of non-descriptive anchors. Match the
# entire normalized anchor after lowercasing, collapsing whitespace, and
# trimming surrounding punctuation. This catches "Read More", localized
# equivalents, and variants such as "  click here…", but not a meaningful
# phrase such as "click here to buy" where the remaining words add context.
_GENERIC_ANCHORS = frozenset(
    {
        # Russian
        "тут",
        "здесь",
        "далее",
        "подробнее",
        "сюда",
        "ссылка",
        "читать далее",
        "читать дальше",
        "читайте далее",
        "читайте дальше",
        "по ссылке",
        "перейти по ссылке",
        "перейдите по ссылке",
        "нажмите здесь",
        "узнать больше",
        "больше",
        "смотрите тут",
        # English
        "here",
        "click here",
        "read more",
        "learn more",
        "more",
        "see more",
        "view more",
        "click",
        "link",
        "this",
        "this link",
        "continue reading",
        "check it out",
        "details",
    }
)

_GENERIC_ANCHOR_TRIM = " \t\u00a0.,;:!?«»\"'()[]{}…—–-"  # noqa: RUF001 - punctuation set


def _norm_anchor(anchor: str) -> str:
    cleaned = re.sub(r"\s+", " ", anchor.lower()).strip()
    return cleaned.strip(_GENERIC_ANCHOR_TRIM).strip()


def check_anchor_text(ctx: AuditContext) -> None:
    """Flag localized non-descriptive anchors such as "here" and "click here".

    Scans every available ``*:Inlinks`` export (``all_inlinks`` preferred; the
    status-code reports as fallback). The fix lives on the *source* page, so one
    issue is emitted per source URL, with each generic link in ``details``. If no
    inlinks export is loaded the check skips honestly rather than emit zeros.
    """
    max_locs = ctx.config.get("output", {}).get("max_locations_per_issue", 200)
    export_keys = [
        k
        for k in ("all_inlinks", "inlinks_4xx", "inlinks_5xx", "inlinks_3xx")
        if ctx.exports.has(k) and not ctx.exports.get(k).empty
    ]
    if not export_keys:
        graph = _graph_access(ctx)
        if graph is not None:
            for group in graph.iter_anchor_groups(
                lambda anchor: _norm_anchor(anchor) in _GENERIC_ANCHORS, max_locs
            ):
                ctx.add(
                    "GENERIC_ANCHOR_TEXT",
                    target_url=group.source_url,
                    occurrences_count=group.occurrences_count,
                    locations=group.locations,
                    details={"generic_links": group.generic_links},
                    evidence={"exports": ["all_inlinks"], "files": [None]},
                )
            return
        ctx.skip(
            "GENERIC_ANCHOR_TEXT",
            "no *:Inlinks export available (export 'All Inlinks' or any *:Inlinks report)",
        )
        return

    seen: set[tuple[str | None, str | None, str]] = set()
    by_source: OrderedDict[str, list[Link]] = OrderedDict()
    for key in export_keys:
        for rec in records_from_df(ctx.exports.get(key), INLINKS_FIELD_MAP):
            link = _link_from_record(rec)
            anchor = link.anchor
            if not anchor or not link.source_url:
                continue
            if _norm_anchor(anchor) not in _GENERIC_ANCHORS:
                continue
            dedup = (link.source_url, link.destination_url, anchor.strip().lower())
            if dedup in seen:
                continue
            seen.add(dedup)
            by_source.setdefault(link.source_url, []).append(link)

    for source, links in by_source.items():
        generic_links = [
            {
                "anchor": link.anchor,
                "destination": link.destination_url,
                "link_position": link.link_position,
            }
            for link in links[:max_locs]
        ]
        ctx.add(
            "GENERIC_ANCHOR_TEXT",
            target_url=source,
            occurrences_count=len(links),
            locations=[link.as_location() for link in links[:max_locs]],
            details={"generic_links": generic_links},
            evidence={
                "exports": export_keys,
                "files": [ctx.exports.files.get(k) for k in export_keys],
            },
        )


# ---------------------------------------------------------------------------
# hreflang -> broken target
# ---------------------------------------------------------------------------
def check_hreflang_targets(ctx: AuditContext) -> None:
    """HREFLANG_BROKEN_TARGET — hreflang points at a 3xx/4xx/5xx URL.

    Reads the Bulk Export → Links → ``All Hreflang`` report (one row per
    hreflang annotation: source → target URL + lang). Each target is matched
    against the crawl: a target that responds 3xx/4xx/5xx (or carries a Redirect
    URL) breaks international localization and is flagged on its source page.
    Targets not in the crawl (external cross-domain hreflang) cannot be
    classified and are skipped silently. If the export is absent the check
    skips honestly rather than emit a dead zero.
    """
    df = ctx.exports.get("all_hreflang")
    if df is None or df.empty:
        ctx.skip(
            "HREFLANG_BROKEN_TARGET",
            "no all_hreflang export (export Bulk Export → Links → All Hreflang to enable)",
        )
        return

    by_source: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    seen_pairs: set[tuple[str, str]] = set()
    for rec in records_from_df(df, HREFLANG_FIELD_MAP):
        src = rec.get("source_url")
        dest = rec.get("destination_url")
        if not src or not dest:
            continue
        # #176 audit: this reads the single 2xx-preferring representative, not every
        # variant under the key, but that stays correct here — unlike the bug in
        # check_canonical_to_redirect, a 3xx and a 4xx twin agree on the verdict this
        # check cares about (both are "broken"), so which one is picked only changes
        # which status/redirect_url gets quoted, never whether HREFLANG_BROKEN_TARGET fires.
        target = ctx.page_by_norm.get(norm_url(dest))
        if target is None:
            continue  # external / not crawled — cannot classify the target
        code = target.status_code
        redirect_url = ctx.redirect_map.get(target.url)
        is_redirect = (code is not None and 300 <= code <= 399) or bool(redirect_url)
        is_error = code is not None and code >= 400
        if not (is_redirect or is_error):
            continue
        pair = (src, dest)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        by_source.setdefault(src, []).append(
            {
                "hreflang": rec.get("hreflang"),
                "target_url": dest,
                "status_code": code,
                "redirect_url": redirect_url,
            }
        )

    for source, targets in by_source.items():
        ctx.add(
            "HREFLANG_BROKEN_TARGET",
            target_url=source,
            occurrences_count=len(targets),
            details={"broken_targets": targets},
            evidence={"export": ctx.exports.files.get("all_hreflang")},
        )


def _rec(page: Any) -> dict[str, Any]:
    return page.metrics.get("_record", {})


# ---------------------------------------------------------------------------
# hreflang -> code/self-reference/x-default/duplicate/canonical quality
# ---------------------------------------------------------------------------
_HREFLANG_QUALITY_CHECKS = (
    "HREFLANG_INVALID_CODE",
    "HREFLANG_MULTIPLE_ENTRIES",
    "HREFLANG_MISSING_SELF_REFERENCE",
    "HREFLANG_MISSING_XDEFAULT",
    "HREFLANG_NOT_CANONICAL",
)


def check_hreflang_quality(ctx: AuditContext) -> None:
    """Validate each page's own hreflang set: codes, duplicates, self, x-default, canonical.

    Reads the same Bulk Export → Links → ``All Hreflang`` report as
    :func:`check_hreflang_targets` (one row per source → destination + lang
    annotation) and reuses the ISO 639-1/3166-1 validator already shipped for
    the single-URL ``seo_hreflang_check`` tool (:func:`seohead.tools.hreflang.
    code_error`) instead of re-implementing it. Every check here groups by the
    declaring page (source), matching how a browser or crawler reads one
    page's hreflang set. If the export is absent, all five checks skip
    honestly rather than emit dead zeros.
    """
    df = ctx.exports.get("all_hreflang")
    if df is None or df.empty:
        for check_id in _HREFLANG_QUALITY_CHECKS:
            ctx.skip(
                check_id, "no all_hreflang export (export Bulk Export -> Links -> All Hreflang)"
            )
        return

    by_source: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for rec in records_from_df(df, HREFLANG_FIELD_MAP):
        src = rec.get("source_url")
        if not src:
            continue
        by_source.setdefault(src, []).append(rec)

    evidence = {"export": ctx.exports.files.get("all_hreflang")}
    for source, entries in by_source.items():
        _check_invalid_codes(ctx, source, entries, evidence)
        _check_duplicate_entries(ctx, source, entries, evidence)
        target_entries = [rec for rec in entries if rec.get("destination_url")]
        _check_self_reference(ctx, source, target_entries, evidence)
        _check_xdefault(ctx, source, target_entries, evidence)
        _check_not_canonical(ctx, source, target_entries, evidence)


def _check_invalid_codes(
    ctx: AuditContext, source: str, entries: list[dict[str, Any]], evidence: dict[str, Any]
) -> None:
    invalid = []
    for rec in entries:
        lang = rec.get("hreflang")
        if not lang:
            continue
        reason = code_error(lang)
        if reason:
            invalid.append(
                {"hreflang": lang, "destination": rec.get("destination_url"), "reason": reason}
            )
    if invalid:
        ctx.add(
            "HREFLANG_INVALID_CODE",
            target_url=source,
            occurrences_count=len(invalid),
            details={"invalid": invalid},
            evidence=evidence,
        )


def _check_duplicate_entries(
    ctx: AuditContext, source: str, entries: list[dict[str, Any]], evidence: dict[str, Any]
) -> None:
    # Language tags are case-insensitive: "en-US" declared twice (even with
    # different casing) is the same annotation twice, not two annotations.
    folded = [str(rec["hreflang"]).strip().lower() for rec in entries if rec.get("hreflang")]
    duplicates = sorted({lang for lang in folded if folded.count(lang) > 1})
    if duplicates:
        ctx.add(
            "HREFLANG_MULTIPLE_ENTRIES",
            target_url=source,
            occurrences_count=len(duplicates),
            details={"duplicate_values": duplicates},
            evidence=evidence,
        )


def _check_self_reference(
    ctx: AuditContext, source: str, entries: list[dict[str, Any]], evidence: dict[str, Any]
) -> None:
    source_norm = norm_url(source)
    destinations = {norm_url(rec.get("destination_url")) for rec in entries}
    if source_norm not in destinations:
        ctx.add(
            "HREFLANG_MISSING_SELF_REFERENCE",
            target_url=source,
            details={"declared_targets": sorted({rec["destination_url"] for rec in entries})},
            evidence=evidence,
        )


def _check_xdefault(
    ctx: AuditContext, source: str, entries: list[dict[str, Any]], evidence: dict[str, Any]
) -> None:
    folded = {str(rec["hreflang"]).strip().lower() for rec in entries if rec.get("hreflang")}
    if "x-default" not in folded:
        ctx.add("HREFLANG_MISSING_XDEFAULT", target_url=source, evidence=evidence)


def _check_not_canonical(
    ctx: AuditContext, source: str, entries: list[dict[str, Any]], evidence: dict[str, Any]
) -> None:
    offenders = []
    for rec in entries:
        dest = rec.get("destination_url")
        # #176 audit: correct by construction. A redirecting twin under this key rarely
        # carries a canonical tag of its own (SF's parser reads canonical off the fetched
        # HTML, and a 3xx has none) — the 2xx variant is the one this check needs.
        target = ctx.page_by_norm.get(norm_url(dest))
        if target is None:
            continue  # external / not crawled — cannot classify
        canonical = _rec(target).get("canonical")
        if canonical and norm_url(canonical) != norm_url(target.url):
            offenders.append(
                {"hreflang": rec.get("hreflang"), "destination": dest, "canonical": canonical}
            )
    if offenders:
        ctx.add(
            "HREFLANG_NOT_CANONICAL",
            target_url=source,
            occurrences_count=len(offenders),
            details={"non_canonical_targets": offenders},
            evidence=evidence,
        )


def check_hreflang_reciprocity(ctx: AuditContext) -> None:
    """HREFLANG_MISSING_RETURN_LINK — A names B, but B never names A back.

    Google's hreflang contract requires every annotation to be reciprocal:
    if A points to B, B must point back to A. Whether B reciprocates is a
    property of the *pair* and depends on B's own hreflang set, which is only
    on hand once B itself has been crawled — provable only once the crawl of
    both sides is complete (issue #15, item 6).
    """
    df = ctx.exports.get("all_hreflang")
    if df is None or df.empty:
        ctx.skip(
            "HREFLANG_MISSING_RETURN_LINK",
            "no all_hreflang export (export Bulk Export -> Links -> All Hreflang)",
        )
        return

    edges: set[tuple[str, str]] = set()
    for rec in records_from_df(df, HREFLANG_FIELD_MAP):
        src, dest = rec.get("source_url"), rec.get("destination_url")
        if not src or not dest:
            continue
        src_norm, dest_norm = norm_url(src), norm_url(dest)
        if src_norm == dest_norm:
            continue  # a self-reference is not a reciprocity pair
        edges.add((src_norm, dest_norm))

    missing_by_target: OrderedDict[str, list[str]] = OrderedDict()
    for src_norm, dest_norm in sorted(edges):
        if (dest_norm, src_norm) in edges:
            continue  # B already names A back
        # #176 audit: every read of page_by_norm below is "does this key exist in the
        # crawl" or a display label — the reciprocity verdict itself comes entirely from
        # the hreflang edge set above, so which variant is the representative is moot.
        target = ctx.page_by_norm.get(dest_norm)
        if target is None:
            continue  # external / not crawled — cannot fault it for not reciprocating
        missing_by_target.setdefault(dest_norm, []).append(src_norm)

    for dest_norm, source_norms in missing_by_target.items():
        target = ctx.page_by_norm[dest_norm]
        expected_from = sorted(
            ctx.page_by_norm[n].url if n in ctx.page_by_norm else n for n in source_norms
        )
        ctx.add(
            "HREFLANG_MISSING_RETURN_LINK",
            target_url=target.url,
            occurrences_count=len(expected_from),
            details={"expected_return_to": expected_from},
            evidence={"export": ctx.exports.files.get("all_hreflang")},
        )


# "x-default" names a fallback for unmatched users, not a language and region, so
# it is never a locale claim either side of a pair can be measured against: a page
# is routinely declared both as "en" by its counterparts and as "x-default" by
# itself, and reading the second as a contradiction of the first would fire on the
# single most common correct hreflang layout there is.
_XDEFAULT = "x-default"


def _hreflang_code(value: Any) -> str:
    """One hreflang value folded for comparison -- case only, nothing else.

    Language tags are case-insensitive ("en-GB" and "en-gb" are one tag), so the
    fold is safe. Nothing further is normalised on purpose: "en" and "en-GB" are
    genuinely different annotations, and quietly treating a region-less tag as
    matching a regioned one would hide exactly the inconsistency this check exists
    to find.
    """
    return str(value or "").strip().lower()


def check_hreflang_confirmation_consistency(ctx: AuditContext) -> None:
    """HREFLANG_INCONSISTENT_CONFIRMATION -- A declares B as "fr"; B says it is "de".

    :func:`check_hreflang_reciprocity` asks only whether a return link exists. A pair
    can be fully reciprocal and still be discarded by Google, because the contract is
    that both sides name the *same* language and region code: a page's counterparts
    must call it what it calls itself. That is the fact this reads (#386).

    The counterpart's own self-referencing hreflang is the authority for what it is,
    which is why a counterpart with no self-reference is passed over entirely rather
    than guessed at -- that page has no statement to disagree with, and
    HREFLANG_MISSING_SELF_REFERENCE already names it. ``x-default`` is excluded on
    both sides (see ``_XDEFAULT``), and a declaration whose target was never crawled
    is left alone for the same reason reciprocity leaves it alone: a page nobody
    fetched cannot be faulted for what it does or does not say.

    Reported against the *declaring* page, because that is where the annotation that
    disagrees was written; the details name the counterpart, the code this page
    claimed for it, and the codes the counterpart confirms for itself, so the reader
    can see which of the two is wrong without opening both.
    """
    df = ctx.exports.get("all_hreflang")
    if df is None or df.empty:
        ctx.skip(
            "HREFLANG_INCONSISTENT_CONFIRMATION",
            "no all_hreflang export (export Bulk Export -> Links -> All Hreflang)",
        )
        return

    # What each page declares about itself, and what every other page declares
    # about it, keyed the same way so the two are comparable at all.
    self_codes: dict[str, set[str]] = {}
    claims: OrderedDict[str, list[tuple[str, str]]] = OrderedDict()
    # The URL as the export wrote it, for the case where neither end is in the
    # crawl's own page table: a normalised key is a comparison aid, not an address
    # a reader can open.
    written_as: dict[str, str] = {}
    for rec in records_from_df(df, HREFLANG_FIELD_MAP):
        src, dest = rec.get("source_url"), rec.get("destination_url")
        code = _hreflang_code(rec.get("hreflang"))
        if not src or not dest or not code or code == _XDEFAULT:
            continue
        src_norm, dest_norm = norm_url(src), norm_url(dest)
        written_as.setdefault(src_norm, str(src))
        written_as.setdefault(dest_norm, str(dest))
        if src_norm == dest_norm:
            self_codes.setdefault(src_norm, set()).add(code)
        else:
            claims.setdefault(src_norm, []).append((dest_norm, code))

    evidence = {"export": ctx.exports.files.get("all_hreflang")}
    for src_norm, declared in claims.items():
        source_page = ctx.page_by_norm.get(src_norm)
        mismatched = []
        for dest_norm, code in declared:
            confirmed = self_codes.get(dest_norm)
            if not confirmed or code in confirmed:
                continue  # no self-statement to contradict, or the two agree
            target = ctx.page_by_norm.get(dest_norm)
            if target is None:
                continue  # counterpart was not crawled, so its self-row is not usable evidence
            mismatched.append(
                {
                    "counterpart": target.url,
                    "declared_here": code,
                    "confirmed_there": sorted(confirmed),
                }
            )
        if mismatched:
            ctx.add(
                "HREFLANG_INCONSISTENT_CONFIRMATION",
                target_url=(source_page.url if source_page is not None else written_as[src_norm]),
                occurrences_count=len(mismatched),
                details={"inconsistent": mismatched},
                evidence=evidence,
            )


# ---------------------------------------------------------------------------
# Whole-graph passes over the complete inlink inventory (issue #15, items
# 1, 7, 10, 11): each needs every link on the site, not only the broken ones,
# so all four honestly skip without ``all_inlinks``.
# ---------------------------------------------------------------------------
def _internal_hyperlink_edges(
    records: list[dict[str, Any]], site_host: str
) -> list[tuple[str, str]]:
    """(source, destination) pairs for internal, followed ``Hyperlink`` rows.

    Excludes external destinations (a different host) and rows Screaming Frog
    marks ``Follow: false`` — neither carries internal link equity or forms
    part of internal navigation. A row with no Type value at all (an older
    export) is kept rather than dropped, since assuming it is a hyperlink is
    the safer default for this filter.
    """
    edges: list[tuple[str, str]] = []
    for rec in records:
        source, dest = rec.get("source_url"), rec.get("destination_url")
        if not source or not dest:
            continue
        link_type = rec.get("type")
        if link_type is not None and str(link_type).strip().lower() != "hyperlink":
            continue
        follow = rec.get("follow")
        if follow is not None and not is_true(follow):
            continue
        dest_host = urllib.parse.urlparse(dest).netloc.lower()
        if dest_host and dest_host != site_host:
            continue
        edges.append((norm_url(source), norm_url(dest)))
    return edges


def _emit_link_scores(ctx: AuditContext, count: int, median: float, score_for) -> None:
    """Apply the one reviewed LOW_LINK_SCORE threshold/emission path."""
    for page in ctx.pages:
        score = score_for(norm_url(page.url))
        if score is not None:
            page.metrics["link_score_computed"] = round(score, 6)
    if count < 5 or median <= 0:
        return
    ratio = ctx.thresholds.get("link_score_low_ratio", 0.25)
    for page in ctx.indexable_html_pages():
        if _rec(page).get("crawl_depth") == 0:
            continue
        score = score_for(norm_url(page.url))
        if score is None or score >= median * ratio:
            continue
        ctx.add(
            "LOW_LINK_SCORE",
            target_url=page.url,
            details={
                "link_score": round(score, 6),
                "site_median": round(median, 6),
                "ratio_to_median": round(score / median, 3),
            },
        )


def check_link_score(ctx: AuditContext) -> None:
    """LOW_LINK_SCORE — an iterative internal-PageRank pass (issue #15, item 1).

    One new edge changes every page's score, so this can only run once the
    crawl's own edge list is complete. It reads the same ``all_inlinks``
    export the checks below share, restricted to internal, followed hyperlink
    edges, and flags indexable pages scoring far below the site's own median
    — a signal a raw inlink *count* misses, since a page can hold several
    inlinks and still be starved of link equity if every one is nofollow or
    itself comes from a poorly linked page.
    """
    records = _all_inlink_records(ctx)
    if records is None:
        graph = _graph_access(ctx)
        if graph is not None:
            stats = graph.link_score(
                damping=DEFAULT_DAMPING,
                max_iterations=DEFAULT_MAX_ITERATIONS,
                tolerance=DEFAULT_TOLERANCE,
            )
            if stats is None:
                ctx.skip("LOW_LINK_SCORE", "all_inlinks export has no internal followed hyperlinks")
                return
            _emit_link_scores(ctx, stats.count, stats.median, stats.score_for)
            return
        ctx.skip(
            "LOW_LINK_SCORE", "no all_inlinks export (needed for the complete internal edge list)"
        )
        return
    site_host = _site_host(ctx)
    edges = _internal_hyperlink_edges(records, site_host)
    if not edges:
        ctx.skip("LOW_LINK_SCORE", "all_inlinks export has no internal followed hyperlinks")
        return

    urls = {norm_url(p.url) for p in ctx.pages}
    scores = compute_link_scores(edges, urls)
    values = sorted(scores.values())
    _emit_link_scores(ctx, len(values), statistics.median(values) if values else 0.0, scores.get)


def check_inlink_composition(ctx: AuditContext) -> None:
    """ONLY_NOFOLLOW_INLINKS / ONLY_NONINDEXABLE_SOURCE_INLINKS.

    Both are aggregates over *every* inlink a URL has: "only nofollow" is
    false the moment one more, follow inlink turns up, and the same is true of
    "only from non-indexable sources" — so neither is provable before the
    crawl (and the complete ``all_inlinks`` export) is finished (issue #15,
    item 7). Crawl depth, the aggregate item 7 also names, is already answered
    by ``DEEP_CRAWL_DEPTH`` from Internal:All's own Crawl Depth column, which
    Screaming Frog only finalizes once the crawl ends.
    """
    records = _all_inlink_records(ctx)
    if records is None:
        graph = _graph_access(ctx)
        if graph is not None:

            def source_indexable(source: str) -> bool | None:
                page = ctx.page_by_norm.get(norm_url(source))
                return page.is_indexable if page is not None else None

            _emit_inlink_composition(ctx, graph.iter_inlink_composition(source_indexable, 20))
            return
        for check_id in ("ONLY_NOFOLLOW_INLINKS", "ONLY_NONINDEXABLE_SOURCE_INLINKS"):
            ctx.skip(check_id, "no all_inlinks export (needed for the complete inlink list)")
        return

    site_host = _site_host(ctx)
    # #313: group by the fragment-free page identity, not the raw destination.
    # A crawled page is one row in ctx.page_by_norm keyed by norm_url(page.url),
    # which never carries a fragment; norm_url(dest) alone still does (#202), so
    # "/target" and "/target#details" grouped by raw dest formed two composition
    # buckets and the fragment-bearing one could never resolve to the crawled
    # page, silently dropping its nofollow/nonindexable-source evidence. Each
    # record keeps its own raw destination_url below — only the grouping key is
    # defragmented, and neither norm_url() nor CANONICAL_FRAGMENT are touched.
    by_dest: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for rec in records:
        source, dest = rec.get("source_url"), rec.get("destination_url")
        if not source or not dest:
            continue
        link_type = rec.get("type")
        if link_type is not None and str(link_type).strip().lower() != "hyperlink":
            continue
        dest_host = urllib.parse.urlparse(dest).netloc.lower()
        if dest_host and dest_host != site_host:
            continue  # external destination — not this page's own composition
        dest_key = urllib.parse.urldefrag(norm_url(dest))[0]
        by_dest.setdefault(dest_key, []).append(rec)

    def export_rows():
        for dest_key, links in by_dest.items():
            follows = [
                is_true(link["follow"]) if link.get("follow") is not None else True
                for link in links
            ]
            source_indexability = []
            for link in links:
                source_page = ctx.page_by_norm.get(norm_url(link.get("source_url")))
                if source_page is not None:
                    source_indexability.append(source_page.is_indexable)
            yield InlinkCompositionRow(
                destination_key=dest_key,
                occurrences_count=len(links),
                all_nofollow=not any(follows),
                has_known_source=bool(source_indexability),
                has_indexable_source=any(source_indexability),
                source_examples=sorted({link["source_url"] for link in links})[:20],
            )

    _emit_inlink_composition(ctx, export_rows())


def _emit_inlink_composition(ctx: AuditContext, rows) -> None:
    """Apply the shared target eligibility, thresholds and issue payloads."""
    for row in rows:
        destination_key = row.destination_key
        occurrences = row.occurrences_count
        all_nofollow = row.all_nofollow
        known_source = row.has_known_source
        indexable_source = row.has_indexable_source
        sources = row.source_examples
        # #176 audit: correct by construction, same reasoning as check_unlinked_canonical —
        # is_indexable is a property of the live page, so the 2xx-preferring representative
        # is the variant "is this destination's inlink composition worth flagging" means.
        # The per-source lookup at the bottom of this loop resolves each source individually,
        # not a shared-key group, so the same single-representative read is simply correct.
        # destination_key is already norm_url() with the fragment stripped (#313), and
        # ctx.page_by_norm's keys are norm_url(page.url) for URLs that never carry
        # a fragment, so it is the correct lookup key as-is.
        target = ctx.page_by_norm.get(destination_key)
        if target is None or not target.is_indexable:
            continue
        if _rec(target).get("crawl_depth") == 0:
            continue  # the homepage's inlink composition is not the whole story

        if all_nofollow:
            ctx.add(
                "ONLY_NOFOLLOW_INLINKS",
                target_url=target.url,
                occurrences_count=occurrences,
                details={"inlink_count": occurrences},
            )

        if known_source and not indexable_source:
            ctx.add(
                "ONLY_NONINDEXABLE_SOURCE_INLINKS",
                target_url=target.url,
                occurrences_count=occurrences,
                details={
                    "inlink_count": occurrences,
                    "sources": sorted(sources)[:20],
                },
            )


def _emit_discovery_paths(ctx: AuditContext, path_for) -> None:
    """Emit the one reviewed DEEP_DISCOVERY_PATH threshold and issue shape."""
    max_depth = ctx.thresholds.get("crawl_depth_max", 4)
    for page in ctx.indexable_html_pages():
        path_norm = path_for(norm_url(page.url))
        if path_norm is None or len(path_norm) - 1 <= max_depth:
            continue
        path_urls = [
            ctx.page_by_norm[item].url if item in ctx.page_by_norm else item for item in path_norm
        ]
        ctx.add(
            "DEEP_DISCOVERY_PATH",
            target_url=page.url,
            details={"path": path_urls, "hops": len(path_norm) - 1},
        )


def check_discovery_path(ctx: AuditContext) -> None:
    """DEEP_DISCOVERY_PATH — the concrete shortest click path from the seed.

    Internal:All's Crawl Depth column already reports each page's distance
    from the start URL, but not the route that produces it, and only a
    breadth-first walk over the *finished* internal hyperlink graph can
    reconstruct one (issue #15, item 10). Flags indexable pages whose
    shortest route exceeds the same ``crawl_depth_max`` threshold
    ``DEEP_CRAWL_DEPTH`` already uses, this time with the actual path attached.
    """
    records = _all_inlink_records(ctx)
    if records is None:
        graph = _graph_access(ctx)
        if graph is not None:
            if not graph.has_internal_hyperlinks:
                ctx.skip("DEEP_DISCOVERY_PATH", "all_inlinks export has no internal hyperlinks")
                return
            seed = next((page for page in ctx.pages if _rec(page).get("crawl_depth") == 0), None)
            if seed is None:
                ctx.skip("DEEP_DISCOVERY_PATH", "no page at Crawl Depth 0 to use as the seed")
                return
            paths = graph.begin_paths(norm_url(seed.url))
            if paths is None:
                ctx.skip("DEEP_DISCOVERY_PATH", "all_inlinks export has no internal hyperlinks")
                return
            _emit_discovery_paths(ctx, paths.path_to)
            return
        ctx.skip(
            "DEEP_DISCOVERY_PATH",
            "no all_inlinks export (needed for the complete internal edge list)",
        )
        return
    site_host = _site_host(ctx)
    edges = _internal_hyperlink_edges(records, site_host)
    if not edges:
        ctx.skip("DEEP_DISCOVERY_PATH", "all_inlinks export has no internal hyperlinks")
        return

    seed = next((p for p in ctx.pages if _rec(p).get("crawl_depth") == 0), None)
    if seed is None:
        ctx.skip("DEEP_DISCOVERY_PATH", "no page at Crawl Depth 0 to use as the seed")
        return

    paths = shortest_paths_from_seed(edges, norm_url(seed.url))
    _emit_discovery_paths(ctx, paths.get)


# Rows Screaming Frog's All Inlinks export uses for a page's own directives
# rather than an actual fetched resource — never "insecure subresources".
_NON_RESOURCE_LINK_TYPES = frozenset(
    {"hyperlink", "canonical", "rel next", "rel prev", "meta refresh", "amphtml"}
)


def check_insecure_subresources(ctx: AuditContext) -> None:
    """INSECURE_SUBRESOURCE — an HTTPS page loads a subresource over HTTP.

    Needs the completed inventory of every page cross-joined with the
    resources it loads (issue #15, item 11): this reads the non-hyperlink
    rows (Image, JavaScript, CSS, ...) of the same ``all_inlinks`` export the
    checks above already use. A native Security:Mixed Content export answers
    the same question and is preferred when present (``check_native_exports``
    → ``MIXED_CONTENT``); this only fills the gap when that report was not
    exported, and skips rather than double-report when it was.
    """
    if ctx.exports.has("security_mixed"):
        ctx.skip("INSECURE_SUBRESOURCE", "native Security:Mixed Content export already covers this")
        return
    records = _all_inlink_records(ctx)
    if records is None:
        graph = _graph_access(ctx)
        if graph is not None:
            if graph.has_resource_type:
                by_source: OrderedDict[str, list[str]] = OrderedDict()
                for source, dest, link_type in graph.iter_resources():
                    if not source.lower().startswith("https://"):
                        continue
                    if str(link_type).strip().lower() in _NON_RESOURCE_LINK_TYPES:
                        continue
                    if dest.lower().startswith("http://"):
                        by_source.setdefault(source, []).append(dest)
                for source, resources in by_source.items():
                    ctx.add(
                        "INSECURE_SUBRESOURCE",
                        target_url=source,
                        occurrences_count=len(resources),
                        details={"resources": sorted(set(resources))[:20]},
                    )
                return
            ctx.skip(
                "INSECURE_SUBRESOURCE",
                "all_inlinks export has no Type column (needed to tell a resource from a hyperlink)",
            )
            return
        ctx.skip(
            "INSECURE_SUBRESOURCE", "no all_inlinks export (needed for the resource inventory)"
        )
        return
    if not any(rec.get("type") for rec in records):
        ctx.skip(
            "INSECURE_SUBRESOURCE",
            "all_inlinks export has no Type column (needed to tell a resource from a hyperlink)",
        )
        return

    by_source: OrderedDict[str, list[str]] = OrderedDict()
    for rec in records:
        source, dest = rec.get("source_url"), rec.get("destination_url")
        if not source or not dest or not source.lower().startswith("https://"):
            continue
        link_type = str(rec.get("type") or "").strip().lower()
        if link_type in _NON_RESOURCE_LINK_TYPES:
            continue
        if dest.lower().startswith("http://"):
            by_source.setdefault(source, []).append(dest)

    for source, resources in by_source.items():
        ctx.add(
            "INSECURE_SUBRESOURCE",
            target_url=source,
            occurrences_count=len(resources),
            details={"resources": sorted(set(resources))[:20]},
        )


# Screaming Frog types every All Inlinks row, and a page's own rel="next" /
# rel="prev" declarations are rows in it like any other link (the same Type
# values _NON_RESOURCE_LINK_TYPES above already names). That export is
# therefore the only place a *complete* per-page declaration list exists:
# Internal:All keeps the first of each in its rel="next" 1 / rel="prev" 1
# columns and drops the rest, so "how many did this page declare" cannot be
# answered there at all (#385).
_PAGINATION_LINK_TYPES = {"rel next": 'rel="next"', "rel prev": 'rel="prev"'}

_PAGINATION_DECLARATION_CHECKS = ("PAGINATION_MULTIPLE", "PAGINATION_URL_NOT_IN_ANCHOR")


def _pagination_declarations(
    records: list[dict[str, Any]],
) -> OrderedDict[str, OrderedDict[str, list[str]]]:
    """source URL -> relation -> declared destinations, in export order.

    Destinations are de-duplicated per relation: the same URL declared twice is
    one successor written twice, which is untidy markup and not an ambiguous
    series, and the row this feeds is about *which page comes next*.
    """
    out: OrderedDict[str, OrderedDict[str, list[str]]] = OrderedDict()
    for rec in records:
        source, dest = rec.get("source_url"), rec.get("destination_url")
        if not source or not dest:
            continue
        relation = _PAGINATION_LINK_TYPES.get(str(rec.get("type") or "").strip().lower())
        if relation is None:
            continue
        targets = out.setdefault(source, OrderedDict()).setdefault(relation, [])
        if dest not in targets:
            targets.append(dest)
    return out


def _anchor_destinations(records: list[dict[str, Any]]) -> dict[str, set[str]]:
    """source (normalized) -> every destination it reaches with a ``Hyperlink`` row.

    Unlike ``_internal_hyperlink_edges`` this keeps nofollow rows: the question
    here is whether an ``<a href>`` to the paginated URL exists on the page at
    all, and a nofollow anchor is still an anchor.
    """
    out: dict[str, set[str]] = {}
    for rec in records:
        if str(rec.get("type") or "").strip().lower() != "hyperlink":
            continue
        source, dest = rec.get("source_url"), rec.get("destination_url")
        if not source or not dest:
            continue
        out.setdefault(norm_url(source), set()).add(norm_url(dest))
    return out


def check_pagination_declarations(ctx: AuditContext) -> None:
    """PAGINATION_MULTIPLE / PAGINATION_URL_NOT_IN_ANCHOR — the declarations themselves.

    Both read the one export that carries every rel="next"/rel="prev"
    declaration beside every anchor on the same page, so both answer from the
    page's own row set rather than from a whole-site negative: unlike
    UNLINKED_PAGINATION_SERIES, "this page does not link its own successor" is
    provable on a partial crawl, because the page in question was crawled and
    its links are all here.

    Each precondition the pair can fail is declared by name rather than passed
    over: no export, no Type column to tell a declaration from an anchor, no
    declarations in it at all.
    """
    records = _all_inlink_records(ctx)
    if records is None and _graph_access(ctx) is None:
        for check_id in _PAGINATION_DECLARATION_CHECKS:
            ctx.skip(
                check_id,
                'no all_inlinks export (needed for every rel="next"/rel="prev" '
                "declaration and the anchors beside them)",
            )
        return
    # A native crawl arrives here by either route and neither carries a link
    # type: its All Inlinks projection leaves Type unset because the spider
    # records only <a href> hyperlinks (see crawl/evidence._inlinks_frame), and
    # the SQL-backed graph declares has_resource_type False. Both therefore
    # reach the same verdict as an export written without the column, which is
    # also what the two routes' parity contract requires of every check.
    if records is None or not any(rec.get("type") for rec in records):
        for check_id in _PAGINATION_DECLARATION_CHECKS:
            ctx.skip(
                check_id,
                "the link inventory carries no link type (needed to tell a "
                'rel="next" declaration from an anchor)',
            )
        return
    declarations = _pagination_declarations(records)
    if not declarations:
        for check_id in _PAGINATION_DECLARATION_CHECKS:
            ctx.skip(check_id, 'all_inlinks export contains no rel="next"/rel="prev" rows')
        return

    anchors = _anchor_destinations(records)
    if not anchors:
        # Declarations but not one Hyperlink row anywhere is a filtered export,
        # not a site with no links on it. Reporting every declaration as
        # un-anchored off that would be a whole crawl of wrong findings.
        ctx.skip(
            "PAGINATION_URL_NOT_IN_ANCHOR",
            "all_inlinks export contains no Hyperlink rows (needed for the page's anchors)",
        )

    for source, by_relation in declarations.items():
        for relation, targets in by_relation.items():
            if len(targets) > 1:
                ctx.add(
                    "PAGINATION_MULTIPLE",
                    target_url=source,
                    details={"relation": relation, "urls": targets},
                )
        if not anchors:
            continue
        linked = anchors.get(norm_url(source), set())
        missing = [
            {"relation": relation, "url": target}
            for relation, targets in by_relation.items()
            for target in targets
            if norm_url(target) not in linked
        ]
        if missing:
            ctx.add(
                "PAGINATION_URL_NOT_IN_ANCHOR",
                target_url=source,
                occurrences_count=len(missing),
                details={"declared_without_an_anchor": missing},
            )


def run_inlinks(ctx: AuditContext) -> None:
    site_host = _site_host(ctx)
    for key, (internal_check, external_check) in INLINK_SOURCES.items():
        _process_export(ctx, key, internal_check, external_check, site_host)
    check_anchor_text(ctx)
    check_hreflang_targets(ctx)
    check_hreflang_quality(ctx)
    check_hreflang_reciprocity(ctx)
    check_hreflang_confirmation_consistency(ctx)
    check_link_score(ctx)
    check_inlink_composition(ctx)
    check_discovery_path(ctx)
    check_insecure_subresources(ctx)
    check_pagination_declarations(ctx)
