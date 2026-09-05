"""Projection of collected evidence onto the analyzer's input contract.

This is a projection onto Screaming Frog's schema, not a neutral format: the
analyzer resolves records by literal SF column headers, so the frames built here
carry those headers. There is exactly one consumer and it has SF's vocabulary.

The important half is what is *declared absent*. A native list-mode run cannot
produce redirect chains, near-duplicate similarity, readability, pixel widths or
link score, and a check that silently reports nothing about them would be read
as a clean result.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from seohead.tools.parser import robots_directives

if TYPE_CHECKING:  # pragma: no cover - typing only
    from seohead.crawl.collect import CrawlResult

# Frames a list-mode run can never fill. Declared so the analyzer skips the
# checks that depend on them instead of reporting them clean.
UNAVAILABLE_FRAMES: tuple[str, ...] = (
    "resp_4xx",
    "resp_5xx",
    "resp_3xx",
    "resp_no_response",
    "resp_blocked",
    "inlinks_4xx",
    "inlinks_5xx",
    "inlinks_3xx",
    "all_inlinks",
    "sitemap_in",
    "sitemap_not_in",
    "sitemap_orphan",
    "sitemap_non_indexable",
    "sitemap_redirects",
    "sitemap_non_200",
    "images_missing_alt",
    "images_over_kb",
    "images_missing_size",
    "titles_duplicate",
    "titles_multiple",
    "hreflang",
    "all_hreflang",
    "desc_duplicate",
    "redirect_chains",
    "security_mixed",
    "security_hsts",
    "structured_data_missing",
)

# Evidence a list-mode run cannot measure at all. Emitting an empty column would
# let a length or similarity check read it as zero.
UNMEASURED_COLUMNS: tuple[str, ...] = (
    "Title 1 Pixel Width",
    "Meta Description 1 Pixel Width",
    "Readability",
    "Flesch Reading Ease Score",
    "Closest Similarity Match",
    "No. Near Duplicates",
    "Link Score",
    "Spelling Errors",
    "Grammar Errors",
)


def _indexability(record: Any, blocked_by_robots: bool = False) -> tuple[str, str]:
    """Derive SF's Indexability pair without inventing a verdict.

    ``blocked_by_robots`` takes priority over the fetched outcome: a
    ``report_only`` crawl fetches a disallowed URL anyway to get full
    coverage, but a compliant crawler never would have, so the status code it
    happened to get back is not what makes the page non-indexable. This is
    exactly what a Screaming Frog ``Internal:All`` export would say for a URL
    it fetched under "ignore robots.txt" while still tracking the disallow.
    """
    if blocked_by_robots:
        return "Non-Indexable", "Blocked by Robots.txt"
    if record.error and record.status_code is None:
        return "Non-Indexable", "Response unavailable"
    code = record.status_code
    if code is None:
        return "Non-Indexable", "Response unavailable"
    if 300 <= code < 400:
        return "Non-Indexable", "Redirected"
    if code >= 400:
        return "Non-Indexable", "Client Error" if code < 500 else "Server Error"
    directives = robots_directives(record.meta_robots, record.x_robots)
    if "noindex" in directives:
        return "Non-Indexable", "noindex"
    if record.canonical and record.canonical.rstrip("/") != record.url.rstrip("/"):
        return "Non-Indexable", "Canonicalised"
    return "Indexable", ""


def _row(
    record: Any,
    blocked_by_robots: bool = False,
    inlink_counts: dict[str, tuple[int, int]] | None = None,
) -> dict[str, Any]:
    indexability, reason = _indexability(record, blocked_by_robots)
    row = {
        "Address": record.url,
        "Content Type": record.content_type,
        "Status Code": record.status_code if record.status_code is not None else 0,
        "Status": record.error or ("OK" if record.status_code == 200 else ""),
        "Indexability": indexability,
        "Indexability Status": reason,
        "Title 1": record.title,
        "Title 1 Length": len(record.title),
        "Meta Description 1": record.meta_description,
        "Meta Description 1 Length": len(record.meta_description),
        "H1-1": record.h1,
        "H1-1 Length": len(record.h1),
        "H1-2": record.h1_2,
        "H2-1": record.h2,
        "Canonical Link Element 1": record.canonical,
        "Meta Robots 1": record.meta_robots,
        "X-Robots-Tag 1": record.x_robots,
        # Evidence for the static Lighthouse checks (seohead/sf/core/lighthouse.py):
        # a native Screaming Frog export carries none of these four by default,
        # so they stay blank there and the checks skip honestly (see rules.py).
        "Content-Encoding": record.content_encoding,
        "Doctype": record.doctype,
        "Viewport": record.viewport,
        "Meta Charset": record.charset,
        # Element-position evidence (issue #123): same story as the four
        # columns above — an SF export never carries this, so it stays blank
        # there and the position/skeleton checks skip honestly (see rules.py).
        "Title Outside Head": record.title_outside_head,
        "Meta Description Outside Head": record.meta_description_outside_head,
        "Canonical Outside Head": record.canonical_outside_head,
        "Directives Outside Head": record.directives_outside_head,
        "Hreflang Outside Head": record.hreflang_outside_head,
        "Head Count": record.head_count,
        "Body Count": record.body_count,
        "Head Not First": record.head_not_first,
        "Invalid Head Elements": record.invalid_head_elements,
        "OG:Title": record.og_title,
        "OG:Description": record.og_description,
        "OG:Image": record.og_image,
        "Size (bytes)": record.size_bytes,
        "Word Count": record.word_count,
        # Not SF columns: an SF export carries no iframe inventory, so these stay
        # blank there and CONTENT_IN_IFRAME skips honestly rather than reporting
        # a false clean (#360).
        "Content Frames": record.content_frames,
        "Content Frames Same-Origin": record.content_frames_same_origin,
        "Text Ratio": record.text_ratio if record.text_ratio is not None else "",
        # The collector counts every link it found; this column counts internal
        # links only, and External Outlinks is the disjoint remainder.
        "Outlinks": max(record.outlinks - record.external_outlinks, 0),
        "External Outlinks": record.external_outlinks,
        "Response Time": record.response_time if record.response_time is not None else "",
        "Redirect URL": record.redirect_url,
        "Crawl Depth": record.crawl_depth,
        "Structured Data": record.jsonld_blocks_found,
        # Not an SF column; seohead.sf.core.normalize resolves it only for
        # this collector's own frames (#18). "static" unless selective
        # rendering escalation re-fetched this page under a fuller
        # representation -- see seohead.crawl.render_escalation.
        "Representation": record.representation,
    }
    # Only set when the crawl's own link graph is known at all (see
    # build_evidence): a page absent from ``inlink_counts`` on a followed-link
    # crawl has zero inlinks for real, but on a fetched-URL-list run nobody
    # ever looked for inlinks, and the two must not read the same (#154).
    if inlink_counts is not None:
        total, unique = inlink_counts.get(record.url, (0, 0))
        row["Inlinks"] = total
        row["Unique Inlinks"] = unique
    return row


def _inlinks_frame(links: list[Any]) -> Any:
    """Project the spider's own hyperlink graph onto the *All Inlinks* shape.

    The spider records only ``<a href>`` hyperlinks — never images, scripts, or
    stylesheets (see ``crawl/spider.py:handle_links``) — so ``Type`` is left
    unset rather than stamped "Hyperlink" for every row. ``seohead.sf.core.
    inlinks`` reads a blank Type as "assume hyperlink" for the checks that only
    need the hyperlink graph (link score, discovery path, inlink composition,
    anchor text), and ``check_insecure_subresources`` reads that same blank as
    "no resource inventory available" and skips honestly instead of reporting
    a false clean.
    """
    import pandas as pd

    return pd.DataFrame(
        {
            "Source": [edge.source for edge in links],
            "Destination": [edge.destination for edge in links],
            "Anchor Text": [edge.anchor for edge in links],
            "Follow": [not edge.nofollow for edge in links],
            "Link Position": [edge.position or None for edge in links],
        }
    )


def _inlink_counts(links: list[Any]) -> dict[str, tuple[int, int]]:
    """Per-destination (Inlinks, Unique Inlinks), from the crawl's own link graph.

    Mirrors what a Screaming Frog ``Internal:All`` export calls those two
    columns: every recorded edge counts toward Inlinks, distinct source pages
    toward Unique Inlinks. A destination absent here was never linked at all.
    """
    totals: Counter[str] = Counter()
    sources: dict[str, set[str]] = {}
    for edge in links:
        totals[edge.destination] += 1
        sources.setdefault(edge.destination, set()).add(edge.source)
    return {dest: (total, len(sources[dest])) for dest, total in totals.items()}


def _hreflang_frame(pages: list[Any]) -> Any:
    """Project each page's own hreflang declarations onto the *All Hreflang* shape.

    The analyzer's three hreflang checks read ``ctx.exports["all_hreflang"]``
    and nothing else, so a native crawl that kept the alternates but did not
    project them here would still have left those checks skipping for want of a
    Screaming Frog export (#357). One row per declaration, source page to target
    URL plus the code as the document wrote it -- an alternate declaring a
    language and pointing nowhere keeps its empty destination rather than being
    dropped, because a malformed declaration is the finding, not noise.
    """
    import pandas as pd

    rows = [
        (record.url, alternate.get("url", ""), alternate.get("lang", ""))
        for record in pages
        for alternate in (record.hreflang or ())
    ]
    return pd.DataFrame(
        {
            "Source": [row[0] for row in rows],
            "Destination": [row[1] for row in rows],
            "Hreflang": [row[2] for row in rows],
        }
    )


def build_evidence(result: CrawlResult) -> dict[str, Any]:
    """Project a crawl into analyzer-shaped frames with its gaps declared.

    Returns plain data — frames, found, missing — rather than an analyzer type.
    The module boundary is the point: ``seohead.crawl`` must stay importable and
    testable without ``seohead.sf``, so assembling the contract is the caller's
    job and the two packages never import each other.
    """
    import pandas as pd

    # Only a followed-links crawl (``SpiderResult``) ever populates a link
    # graph; a fetched URL list (``CrawlResult``) never discovers links, so it
    # keeps declaring "all_inlinks" (and per-page Inlinks/Unique Inlinks,
    # which come from the same graph) absent exactly as before.
    links = getattr(result, "links", None)
    inlink_counts = _inlink_counts(links) if links else None
    # Populated even under ``robots_policy="report_only"``, where a disallowed
    # URL is still fetched and gets an ordinary page row (#154) -- that row
    # must not read as indexable just because the fetch happened to succeed.
    blocked = set(getattr(result, "robots_blocked", None) or [])

    frame = pd.DataFrame(
        [_row(record, record.url in blocked, inlink_counts) for record in result.pages]
    )
    frames: dict[str, Any] = {"internal_all": frame}
    found = ["internal_all"]
    missing = list(UNAVAILABLE_FRAMES)

    if links:
        frames["all_inlinks"] = _inlinks_frame(links)
        found.append("all_inlinks")
        missing.remove("all_inlinks")

    # Only when at least one page declared an alternate. An empty frame would
    # read as "this site has no hreflang errors" on a site that never claimed to
    # be localised at all, which is a clean bill of health nobody asked for and
    # nothing measured; absent, the checks skip and say why.
    if any(record.hreflang for record in result.pages):
        frames["all_hreflang"] = _hreflang_frame(result.pages)
        found.append("all_hreflang")
        missing.remove("all_hreflang")

    return {
        "frames": frames,
        "found": found,
        "missing": missing,
        "unmeasured_columns": list(UNMEASURED_COLUMNS),
    }
