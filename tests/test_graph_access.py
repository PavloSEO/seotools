"""Native graph-access dispatch keeps the SF export path unchanged."""

from __future__ import annotations

import pandas as pd

from seohead.graph import AnchorGroup, InlinkCompositionRow
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.inlinks import run_inlinks
from seohead.sf.core.loader import LoadedExports


class _Scores:
    count = 5
    median = 0.5

    def score_for(self, key):
        return {
            "https://example.test/": 0.8,
            "https://example.test/source": 0.6,
            "https://example.test/target": 0.05,
        }.get(key)


class _Graph:
    has_resource_type = False
    has_internal_hyperlinks = True

    def iter_anchor_groups(self, predicate, _limit):
        assert predicate("click here") is True
        yield AnchorGroup(
            source_url="https://example.test/source",
            occurrences_count=1,
            generic_links=[
                {
                    "anchor": "click here",
                    "destination": "https://example.test/target",
                    "link_position": None,
                }
            ],
            locations=[
                {
                    "source_url": "https://example.test/source",
                    "anchor": "click here",
                    "alt_text": None,
                    "link_position": None,
                    "link_path": None,
                    "follow": None,
                    "rel": None,
                    "target": None,
                }
            ],
        )

    def link_score(self, **_constants):
        return _Scores()

    def iter_inlink_composition(self, _source_indexable, _limit):
        yield InlinkCompositionRow(
            destination_key="https://example.test/target",
            occurrences_count=2,
            all_nofollow=True,
            has_known_source=True,
            has_indexable_source=False,
            source_examples=["https://example.test/source"],
        )

    def begin_paths(self, seed):
        class _Paths:
            def path_to(self, target):
                if target == "https://example.test/target":
                    return (seed, "a", "b", "c", "d", "https://example.test/source", target)
                return None

        return _Paths()

    def iter_resources(self):
        return iter(())


def _context(graph_access=None):
    exports = LoadedExports()
    exports.frames["internal_all"] = pd.DataFrame(
        [
            {
                "Address": "https://example.test/",
                "Content Type": "text/html",
                "Status Code": 200,
                "Status": "OK",
                "Indexability": "Indexable",
                "Crawl Depth": 0,
            },
            {
                "Address": "https://example.test/source",
                "Content Type": "text/html",
                "Status Code": 200,
                "Status": "OK",
                "Indexability": "Non-Indexable",
                "Crawl Depth": 1,
            },
            {
                "Address": "https://example.test/target",
                "Content Type": "text/html",
                "Status Code": 200,
                "Status": "OK",
                "Indexability": "Indexable",
                "Crawl Depth": 6,
            },
        ]
    )
    return AuditContext(exports, load_config(None), graph_access=graph_access)


def test_native_graph_access_runs_existing_inlink_emissions_without_all_inlinks_frame():
    ctx = _context(_Graph())
    run_inlinks(ctx)
    checks = {issue.check for issue in ctx.issues}
    assert {
        "GENERIC_ANCHOR_TEXT",
        "LOW_LINK_SCORE",
        "ONLY_NOFOLLOW_INLINKS",
        "ONLY_NONINDEXABLE_SOURCE_INLINKS",
        "DEEP_DISCOVERY_PATH",
    } <= checks
    skipped = {item.id: item.reason for item in ctx.skipped}
    assert "Type column" in skipped["INSECURE_SUBRESOURCE"]
    anchor = next(issue for issue in ctx.issues if issue.check == "GENERIC_ANCHOR_TEXT")
    assert anchor.evidence == {"exports": ["all_inlinks"], "files": [None]}
    target = next(issue for issue in ctx.issues if issue.check == "LOW_LINK_SCORE")
    assert target.details == {"link_score": 0.05, "site_median": 0.5, "ratio_to_median": 0.1}


def test_export_context_without_graph_access_keeps_existing_all_inlinks_skips():
    ctx = _context()
    run_inlinks(ctx)
    skipped = {item.id: item.reason for item in ctx.skipped}
    assert "all_inlinks" in skipped["LOW_LINK_SCORE"]
    assert "all_inlinks" in skipped["DEEP_DISCOVERY_PATH"]


def test_native_graph_with_no_internal_edges_skips_before_seed_lookup():
    graph = _Graph()
    graph.has_internal_hyperlinks = False
    ctx = _context(graph)
    # The existing export path checks edge availability before looking for depth zero.
    ctx.pages = [page for page in ctx.pages if page.url != "https://example.test/"]
    run_inlinks(ctx)
    skipped = {item.id: item.reason for item in ctx.skipped}
    assert skipped["DEEP_DISCOVERY_PATH"] == "all_inlinks export has no internal hyperlinks"
