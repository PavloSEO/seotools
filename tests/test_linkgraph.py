"""Offline tests for site-wide inlink composition (issue #20, part 3)."""

from seohead.crawl.linkgraph import inlink_composition
from seohead.crawl.spider import LinkEdge


def edge(source, destination, position=""):
    return LinkEdge(
        source=source, destination=destination, anchor="", nofollow=False, position=position
    )


def test_page_linked_only_from_boilerplate_is_flagged():
    """Acceptance criterion: an inlink-composition finding distinguishes
    boilerplate-only pages from pages linked from content."""
    links = [
        edge("https://example.com/", "https://example.com/orphan-ish", "nav"),
        edge("https://example.com/other", "https://example.com/orphan-ish", "footer"),
        edge("https://example.com/", "https://example.com/well-linked", "nav"),
        edge("https://example.com/blog/post", "https://example.com/well-linked", "content"),
    ]
    result = inlink_composition(links)
    boilerplate_only = {p["url"]: p for p in result["pages"] if p["boilerplate_only"]}
    assert set(boilerplate_only) == {"https://example.com/orphan-ish"}
    well_linked = next(p for p in result["pages"] if p["url"] == "https://example.com/well-linked")
    assert well_linked["boilerplate_only"] is False
    assert any("orphan-ish" in f for f in result["findings"])


def test_duplicate_links_from_the_same_page_and_position_count_once():
    links = [
        edge("https://example.com/", "https://example.com/x", "nav"),
        edge("https://example.com/", "https://example.com/x", "nav"),  # repeated anchor, same page
    ]
    result = inlink_composition(links)
    page = result["pages"][0]
    assert page["inlinks_total"] == 1
    assert page["by_position"] == {"nav": 1}


def test_unclassified_edges_are_not_folded_into_a_bucket():
    """A crawl that never enabled classify_links must read as unmeasured, not
    as 'no boilerplate links found'."""
    links = [edge("https://example.com/", "https://example.com/x", position="")]
    result = inlink_composition(links)
    assert result["pages"] == []
    assert result["edges_unclassified"] == 1
    assert result["edges_classified"] == 0
    assert result["measured"] is False
    assert result["classified_fraction"] == 0.0


def test_mixed_classified_and_unclassified_edges_report_both():
    links = [
        edge("https://example.com/", "https://example.com/x", "nav"),
        edge("https://example.com/other", "https://example.com/y", position=""),
    ]
    result = inlink_composition(links)
    assert result["edges_classified"] == 1
    assert result["edges_unclassified"] == 1
    assert result["classified_fraction"] == 0.5
    assert result["measured"] is True


def test_unclassified_inlinks_to_same_page_block_boilerplate_only_verdict():
    """Issue #467: a page with one classified boilerplate inlink and nine
    unclassified inlinks must not be reported as boilerplate-only — the
    unclassified inlinks could be content links never looked at."""
    links = [edge("https://example.com/", "https://example.com/page", "nav")]
    for i in range(9):
        links.append(edge(f"https://example.com/s{i}", "https://example.com/page", position=""))
    result = inlink_composition(links)
    assert "https://example.com/page" not in result["pages_boilerplate_only"]
    page = next(p for p in result["pages"] if p["url"] == "https://example.com/page")
    assert page["boilerplate_only"] is False
    assert page["inlinks_unclassified"] == 9
    assert not any(
        "linked only from navigation, header, sidebar, or footer" in f for f in result["findings"]
    )


def test_fully_classified_boilerplate_only_page_still_flagged():
    """Negative control: no unclassified inlinks at all must not be affected
    by the fix — the page stays boilerplate-only."""
    links = [
        edge("https://example.com/a", "https://example.com/x", "nav"),
        edge("https://example.com/b", "https://example.com/x", "footer"),
        edge("https://example.com/c", "https://example.com/x", "sidebar"),
    ]
    result = inlink_composition(links)
    assert "https://example.com/x" in result["pages_boilerplate_only"]
    page = result["pages"][0]
    assert page["boilerplate_only"] is True
    assert page["inlinks_unclassified"] == 0


def test_fully_classified_mixed_content_and_nav_stays_not_boilerplate():
    """Negative control: a mix of content and nav classified edges, no
    unclassified edges, must still report boilerplate_only False."""
    links = [
        edge("https://example.com/a", "https://example.com/x", "content"),
        edge("https://example.com/b", "https://example.com/x", "nav"),
    ]
    result = inlink_composition(links)
    page = result["pages"][0]
    assert page["boilerplate_only"] is False
    assert page["inlinks_unclassified"] == 0


def test_empty_link_list_reports_cleanly():
    result = inlink_composition([])
    assert result["ok"] is True
    assert result["pages"] == []
    assert result["findings"] == []
    assert result["classified_fraction"] == 0.0
