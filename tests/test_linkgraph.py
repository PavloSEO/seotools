"""Offline tests for site-wide inlink composition (issue #20, part 3).

Every call passes the crawled site's host: since #208 the composition needs it
to tell an internal destination from somebody else's site. The expectations
below are unchanged -- only the argument the question needs was added.
"""

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
    result = inlink_composition(links, "example.com")
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
    result = inlink_composition(links, "example.com")
    page = result["pages"][0]
    assert page["inlinks_total"] == 1
    assert page["by_position"] == {"nav": 1}


def test_unclassified_edges_are_not_folded_into_a_bucket():
    """A crawl that never enabled classify_links must read as unmeasured, not
    as 'no boilerplate links found'."""
    links = [edge("https://example.com/", "https://example.com/x", position="")]
    result = inlink_composition(links, "example.com")
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
    result = inlink_composition(links, "example.com")
    assert result["edges_classified"] == 1
    assert result["edges_unclassified"] == 1
    assert result["classified_fraction"] == 0.5
    assert result["measured"] is True


def test_empty_link_list_reports_cleanly():
    result = inlink_composition([])
    assert result["ok"] is True
    assert result["pages"] == []
    assert result["findings"] == []
    assert result["classified_fraction"] == 0.0


# ── #208: a conclusion about internal linking is about internal pages ─────────


def test_an_external_footer_destination_is_never_boilerplate_only():
    """The finding tells an operator to add a contextual link to the page. On a
    third party's URL that is advice nobody can act on, and it was fired for
    every external link a site carries in its footer."""
    links = [
        edge("https://example.com/", "https://external.invalid/docs", "footer"),
        edge("https://example.com/about", "https://external.invalid/docs", "footer"),
    ]
    result = inlink_composition(links, "example.com")
    assert result["pages"] == []
    assert result["pages_boilerplate_only"] == []
    assert result["edges_external"] == 2
    assert result["findings"] == []


def test_an_internal_footer_only_destination_still_fires():
    """The positive control: the finding this check exists for is unchanged."""
    links = [
        edge("https://example.com/", "https://example.com/buried", "footer"),
        edge("https://example.com/about", "https://example.com/buried", "footer"),
        edge("https://example.com/", "https://external.invalid/docs", "footer"),
    ]
    result = inlink_composition(links, "example.com")
    assert result["pages_boilerplate_only"] == ["https://example.com/buried"]
    assert result["edges_external"] == 1
    assert result["edges_classified"] == 2  # the external edge left the population


def test_a_subdomain_is_a_different_host():
    """Host equality, the same rule sf.core.inlinks applies to exports. A crawl
    that wants subdomains treated as internal says so through its own scope."""
    links = [edge("https://example.com/", "https://blog.example.com/x", "footer")]
    result = inlink_composition(links, "example.com")
    assert result["edges_external"] == 1
    assert result["pages"] == []


def test_without_a_host_nothing_is_claimed():
    """An unknown population must read as "not measured", never as "all
    internal" -- otherwise the caller that forgot the argument silently gets the
    defect back."""
    links = [
        edge("https://example.com/", "https://external.invalid/docs", "footer"),
        edge("https://example.com/", "https://example.com/buried", "footer"),
    ]
    result = inlink_composition(links)
    assert result["population"] == "unpartitioned"
    assert result["pages_boilerplate_only"] == []
    assert all(p["boilerplate_only"] is False for p in result["pages"])
    assert result["findings"] == []


def test_a_default_port_is_the_same_site():
    """A port is not a different company. Comparing netloc instead of hostname
    made https://example.com:443/x external to example.com -- caught by the
    stored-graph parity test, which crawls exactly that shape."""
    links = [
        edge("https://example.com/", "https://example.com:443/buried", "footer"),
        edge("https://example.com/about", "https://example.com:443/buried", "footer"),
    ]
    result = inlink_composition(links, "example.com")
    assert result["edges_external"] == 0
    assert result["pages_boilerplate_only"] == ["https://example.com:443/buried"]
