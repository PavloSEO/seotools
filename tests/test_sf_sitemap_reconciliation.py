"""run_sitemap's declared-vs-crawled reconciliation, in the shape the native
crawler's own reconcile_sitemap() also produces. Network-free: sitemap
membership comes from SF's own ``urls_in_sitemap`` export, never fetched.
"""

from __future__ import annotations

import csv

from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import load_exports
from seohead.sf.core.sitemap_coverage import run_sitemap


def _write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _ctx(tmp_path, crawled_urls, sitemap_urls):
    _write_csv(
        tmp_path / "internal_all.csv",
        ["Address", "Content Type", "Status Code", "Indexability"],
        [[u, "text/html", "200", "Indexable"] for u in crawled_urls],
    )
    _write_csv(tmp_path / "urls_in_sitemap.csv", ["Address"], [[u] for u in sitemap_urls])
    return AuditContext(load_exports(str(tmp_path)), load_config(None))


def test_three_disjoint_sets_under_the_same_keys_the_native_crawler_uses(tmp_path):
    """Written with a (possibly empty) Orphan URLs export throughout, per #368: without one
    the navigation-reachability keys are unavailable rather than guessed from crawl
    presence -- see test_without_an_orphan_export_internal_all_presence_is_not_claimed_as_linked
    below for that case. This fixture's "orphan" URL is never crawled at all, so it lands
    in in_sitemap_not_linked regardless of what the (here, empty) orphan export names."""
    ctx = _ctx_with_orphan_export(
        tmp_path,
        crawled_urls=[
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/extra",
        ],
        sitemap_urls=[
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/orphan",
        ],
        orphan_urls=[],
    )
    summary = run_sitemap(ctx)

    assert sorted(summary["in_sitemap_and_linked"]) == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert summary["in_sitemap_not_linked"] == ["https://example.com/orphan"]
    assert summary["linked_not_in_sitemap"] == ["https://example.com/extra"]

    # Counts stay consistent with the existing (SF-native) count fields.
    assert summary["in_sitemap_not_in_crawl"] == 1
    assert summary["in_crawl_not_in_sitemap"] == 1


def test_no_reconciliation_keys_without_a_sitemap_url_set(tmp_path):
    ctx = _ctx(tmp_path, crawled_urls=["https://example.com/a"], sitemap_urls=[])
    summary = run_sitemap(ctx)
    assert "in_sitemap_and_linked" not in summary
    assert "in_sitemap_not_linked" not in summary
    assert "linked_not_in_sitemap" not in summary


def _ctx_with_orphan_export(tmp_path, crawled_urls, sitemap_urls, orphan_urls):
    """Same as ``_ctx`` but also writes an SF ``Sitemaps: Orphan URLs`` export."""
    _ctx(tmp_path, crawled_urls, sitemap_urls)
    _write_csv(tmp_path / "orphan_urls.csv", ["Address"], [[u] for u in orphan_urls])
    return AuditContext(load_exports(str(tmp_path)), load_config(None))


def test_orphan_export_drives_in_sitemap_not_linked_not_crawl_presence(tmp_path):
    """#368: ``Internal: All`` proves Screaming Frog crawled a URL, not that an internal
    link reaches it -- SF crawls a sitemap-declared URL by requesting the sitemap
    directly, regardless of links. The dedicated ``Sitemaps: Orphan URLs`` export is what
    actually answers "no internal link reaches this", and the summary must agree with the
    SITEMAP_ORPHAN findings it already fires from that same export."""
    linked = "https://example.com/linked"
    orphan_indexable = "https://example.com/orphan-indexable"
    orphan_noindex = "https://example.com/orphan-noindex"
    ctx = _ctx_with_orphan_export(
        tmp_path,
        crawled_urls=[linked, orphan_indexable, orphan_noindex],
        sitemap_urls=[linked, orphan_indexable, orphan_noindex],
        orphan_urls=[orphan_indexable, orphan_noindex],
    )
    summary = run_sitemap(ctx)

    orphans = sorted(i.target_url for i in ctx.issues if i.check == "SITEMAP_ORPHAN")
    assert orphans == sorted([orphan_indexable, orphan_noindex])
    # The summary must not contradict the very findings it just fired.
    assert sorted(summary["in_sitemap_not_linked"]) == sorted([orphan_indexable, orphan_noindex])
    assert summary["in_sitemap_and_linked"] == [linked]
    # Crawl-presence is a distinct, still-available fact -- unaffected by the orphan fix.
    assert summary["in_sitemap_not_in_crawl"] == 0


def test_a_crawled_non_orphan_url_stays_in_sitemap_and_linked(tmp_path):
    """Positive control's companion: a declared URL that Internal:All has AND the orphan
    export does not name must still land in ``in_sitemap_and_linked`` -- the fix narrows
    what counts as linked, it must not empty the healthy set out too."""
    linked = "https://example.com/linked"
    orphan = "https://example.com/orphan"
    ctx = _ctx_with_orphan_export(
        tmp_path,
        crawled_urls=[linked, orphan],
        sitemap_urls=[linked, orphan],
        orphan_urls=[orphan],
    )
    summary = run_sitemap(ctx)
    assert summary["in_sitemap_and_linked"] == [linked]
    assert summary["in_sitemap_not_linked"] == [orphan]


def test_without_an_orphan_export_internal_all_presence_is_not_claimed_as_linked(tmp_path):
    """#368's second half: when no Orphan URLs export was supplied at all, the summary
    must not silently use Internal:All membership as proof of an internal link -- it must
    say the navigation-reachability lists are unavailable instead of guessing."""
    ctx = _ctx(
        tmp_path,
        crawled_urls=["https://example.com/a", "https://example.com/b"],
        sitemap_urls=["https://example.com/a", "https://example.com/b"],
    )
    summary = run_sitemap(ctx)
    assert "in_sitemap_linked_unavailable" in summary
    assert "in_sitemap_and_linked" not in summary
    assert "in_sitemap_not_linked" not in summary


def test_a_trailing_slash_only_difference_is_not_desync(tmp_path):
    """#145: SITEMAP_DESYNC compared raw URL strings, so a canonical written without a
    trailing slash never matched the crawled page that has one -- 100% desync on a site
    that reconcile_sitemap() (the native crawl path's own comparison) reports as 0% for
    the exact same input, because that path already compares on normalize_url()'s key.
    """
    ctx = _ctx_with_orphan_export(
        tmp_path,
        crawled_urls=["https://example.com/a/"],
        sitemap_urls=["https://example.com/a"],
        orphan_urls=[],
    )
    summary = run_sitemap(ctx)

    assert summary["in_sitemap_and_linked"] == ["https://example.com/a/"]
    assert summary["in_sitemap_not_linked"] == []
    assert summary["linked_not_in_sitemap"] == []
    assert not [issue for issue in ctx.issues if issue.check == "SITEMAP_DESYNC"], (
        "a trailing-slash-only mismatch must not read as the crawl and the sitemap "
        "disagreeing about every page"
    )
    page = ctx.pages[0]
    assert page.metrics["is_in_sitemap"] is True
