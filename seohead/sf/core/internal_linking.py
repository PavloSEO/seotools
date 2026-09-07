"""The shape of a site's internal link graph, as numbers rather than findings (#634).

The registry reports link defects one edge or one page at a time. None of them
answers the question an operator actually asks -- *is this site linked well, and
where is it linked badly* -- because that question is about the graph, not about
any edge in it. Three measurements answer most of it, and each is one pass over
evidence a finished crawl already holds:

**Click depth from the start URL.** How many clicks from where the crawl began
each page is, as a histogram and a maximum. This is the headline number: a site
can have almost no orphans and still bury nine tenths of itself behind a chain of
"next post" links, and nothing that counts inlinks can see that. It is *not*
``pages.crawl_depth``, which records the depth at which the crawler happened to
reach a URL -- on a sitemap-seeded crawl that column is 0 for most of the site,
and reading it as click depth reports a linked-list archive as a flat one.

**Edges by position.** Where the links are: body copy, navigation, footer. A site
whose content links are a few percent of all edges is held together entirely by
chrome. Positions are reported exactly as recorded, and edges with no recorded
position are counted under their own name -- never folded into ``content``, and
never counted as a defect either. A crawl run without ``link_position.classify``
has that on every edge, and must read as "nobody looked", not as "all body copy".

**Repeated edges.** How much of the graph is a copy of itself: the same source
page linking to the same destination with the same anchor more than once. On the
40 920-page site this module was measured against, 44.9% of all edges were
repeats of another edge, and they were one duplicated layout block.

Everything here is pure: dictionaries in, dictionaries out. The two backends that
produce the input -- a Screaming Frog ``All Inlinks`` export and a stored native
graph -- live in :mod:`seohead.sf.core.inlinks`, which is also where the checks
that read these numbers are dispatched from.
"""

from __future__ import annotations

from typing import Any

# The key an edge with no recorded position is counted under. Deliberately not one
# of ``link_position.POSITIONS``: it is not a region of the page, it is the absence
# of a measurement, and the two must never share a bucket.
UNCLASSIFIED = "unclassified"

# Cumulative depth marks the summary states outright. Everything here is derivable
# from the histogram, but these three are the ones a reader compares against, and a
# number the method needs should come from the toolkit rather than from arithmetic
# the reader has to do correctly.
DEPTH_MARKS: tuple[int, ...] = (3, 5, 10)


def summarize_positions(totals: dict[str, int]) -> dict[str, Any]:
    """Edge counts by recorded position, with unrecorded ones kept separate.

    ``totals`` maps a position string to a count, exactly as the producer recorded
    it; the empty string means the edge was never classified. Positions that do not
    appear are simply absent from the result -- a zero would claim the site has no
    such region, and it usually means an earlier rule matched first (a menu inside
    ``<header>`` classifies as ``nav``, because the first matching rule wins).
    """
    by_position = {
        position: int(count) for position, count in sorted(totals.items()) if position and count
    }
    unclassified = int(totals.get("", 0))
    classified = sum(by_position.values())
    edges_total = classified + unclassified
    return {
        "edges_total": edges_total,
        "by_position": by_position,
        UNCLASSIFIED: unclassified,
        "classified_fraction": round(classified / edges_total, 4) if edges_total else 0.0,
        # Said in the artifact rather than left to the reader: an unclassified edge
        # is an unanswered question, and a run that answered none of them cannot be
        # asked which parts of the template hold the site together.
        "positions_note": (
            "edges with no recorded position are counted under 'unclassified', never "
            "as content; re-crawl with link_position.classify to classify them"
        ),
    }


def summarize_depth(
    depths: dict[str, int],
    page_keys: list[str],
    *,
    seed: str,
    floor: int,
) -> dict[str, Any]:
    """The click-depth histogram, maximum, and reach, over one page population.

    ``depths`` is ``{normalized url: hops from the seed}`` for every node the walk
    reached; ``page_keys`` is the population being described (the crawl's own HTML
    pages), so a destination that was linked but never fetched does not inflate the
    histogram, and a page nothing links to is counted as unreachable rather than
    quietly omitted.

    ``floor`` is recorded beside the numbers because every verdict drawn from them
    depends on it, and a threshold is a configured number rather than a fact.
    """
    histogram: dict[str, int] = {}
    reached = 0
    deepest = 0
    for key in page_keys:
        depth = depths.get(key)
        if depth is None:
            continue
        reached += 1
        deepest = max(deepest, depth)
        bucket = str(depth)
        histogram[bucket] = histogram.get(bucket, 0) + 1
    return {
        "measured": True,
        "seed": seed,
        "pages": len(page_keys),
        "reachable": reached,
        "unreachable": len(page_keys) - reached,
        "max": deepest if reached else None,
        "histogram": dict(sorted(histogram.items(), key=lambda item: int(item[0]))),
        "within": {
            str(mark): sum(count for depth, count in histogram.items() if int(depth) <= mark)
            for mark in DEPTH_MARKS
        },
        "floor_used": floor,
        # The population, stated: these are followed internal hyperlinks, which is
        # the graph every other whole-graph pass in this analyzer walks.
        "edges": "internal followed hyperlinks",
    }


def unmeasured(reason: str) -> dict[str, Any]:
    """A measurement that could not be taken, saying why.

    An absent block would read as "this site has no depth", which is the one thing
    it must never read as.
    """
    return {"measured": False, "reason": reason}
