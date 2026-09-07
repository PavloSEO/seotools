"""Internal linking as a graph, not as a list of broken edges (issue #634).

Four checks and one summary block, over three layers that had to exist first:
the per-page DOM facts a link's *region* cannot carry (``parser.link_placement``
-> ``LINK_INSIDE_HEADING``, ``IMAGE_LINK_WITHOUT_TEXT``), the whole-graph pass
over the complete edge inventory (``DEEP_CLICK_DEPTH``,
``DUPLICATE_INTERNAL_LINK``), and the numbers both read -- click depth from the
crawl's own start URL, edges by position, and how much of the graph is a copy of
itself.

The seed rule is the load-bearing part of the depth half: ``pages.crawl_depth``
records where the crawler happened to reach a URL, and a sitemap-seeded crawl
records 0 for most of a site, so walking from "a page at depth 0" would describe
a different site. These tests pin that the walk starts from a recorded start URL
or refuses to run.
"""

from __future__ import annotations

import csv
import json

import pytest

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.audit import run_audit
from seohead.sf.core.context import AuditContext
from seohead.sf.core.crawl_path import shortest_depths_from_seed, shortest_paths_from_seed
from seohead.sf.core.internal_linking import summarize_depth, summarize_positions
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules
from seohead.storage import ScanError, import_run, open_scan
from seohead.tools.parser import link_placement, parse_html
from tests.test_scan_artifact import BUILD
from tests.test_scan_artifact import legacy_run as legacy_run

# ---------------------------------------------------------------------------
# Pure parser facts: what a page's own DOM says about its anchors.
# ---------------------------------------------------------------------------

# The shape issue #634 names: an H2 that *is* a link somewhere else, and a card
# photo linking with nothing to read. Both sit inside <main>, so both would be
# classified "content" and neither is visible from a link's position.
PLACEMENT_PAGE = """
<html><body><main>
  <h1><a href="/">Home</a></h1>
  <h2>A section that is only text</h2>
  <h3><a href="/elsewhere">Somewhere else entirely</a></h3>
  <a href="/product-a"><img src="/a.jpg" alt=""></a>
  <a href="/product-b"><img src="/b.jpg" alt="Product B"></a>
  <a href="/product-c" aria-label="Product C"><img src="/c.jpg"></a>
  <a href="/product-d">Product D</a>
  <template><h2><a href="/never">Never rendered</a></h2></template>
</main></body></html>
"""


def _placement(html: str, url: str = "https://example.com/page") -> dict:
    return parse_html(html, url)["link_placement"]


def test_a_link_wrapped_in_a_heading_is_recorded_with_the_nearest_heading_level():
    placement = _placement(PLACEMENT_PAGE)
    assert [(item["level"], item["anchor"]) for item in placement["in_heading"]] == [
        (1, "Home"),
        (3, "Somewhere else entirely"),
    ]
    assert placement["in_heading_total"] == 2
    assert placement["in_heading"][0]["destination"] == "https://example.com/"


def test_only_the_image_link_with_neither_anchor_text_nor_alt_is_recorded():
    """alt text, an aria-label, and ordinary anchor text each say where a link
    goes; only the one that says nothing at all is the defect."""
    placement = _placement(PLACEMENT_PAGE)
    assert [item["destination"] for item in placement["image_no_text"]] == [
        "https://example.com/product-a"
    ]
    assert placement["image_no_text_total"] == 1


def test_a_text_link_with_no_image_is_never_an_image_link():
    placement = _placement('<html><body><a href="/x"></a></body></html>')
    assert placement["image_no_text"] == []
    assert placement["in_heading"] == []


def test_a_heading_link_inside_a_template_is_not_in_the_rendered_document():
    assert all(
        item["destination"] != "https://example.com/never"
        for item in _placement(PLACEMENT_PAGE)["in_heading"]
    )


def test_the_stored_lists_are_capped_and_the_total_still_says_how_many_there_were():
    from seohead.tools.parser import _MAX_PLACEMENT_ITEMS

    many = "".join(f'<a href="/n{n}">n{n}</a>' for n in range(_MAX_PLACEMENT_ITEMS + 7))
    placement = _placement(f"<html><body><h2>{many}</h2></body></html>")
    assert len(placement["in_heading"]) == _MAX_PLACEMENT_ITEMS
    assert placement["in_heading_total"] == _MAX_PLACEMENT_ITEMS + 7


def test_switching_link_parsing_off_records_no_placement_rather_than_an_empty_one():
    parsed = parse_html(PLACEMENT_PAGE, "https://example.com/page", {"links": False})
    assert parsed["link_placement"] is None


def test_link_placement_is_callable_on_its_own_tree():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(PLACEMENT_PAGE, features="lxml")
    placement = link_placement(soup, "https://example.com/page", "https://example.com/page")
    assert placement["in_heading_total"] == 2
    assert placement["image_no_text_total"] == 1


def test_placement_destinations_are_resolved_the_way_the_link_graph_resolves_them():
    """A destination named in a placement finding must be the same URL the edge
    list carries, or the two evidence sets describe different links."""
    html = '<html><head><base href="https://example.com/base/"></head><body>'
    html += '<h2><a href="child">Child</a></h2></body></html>'
    parsed = parse_html(html, "https://example.com/page")
    assert parsed["link_placement"]["in_heading"][0]["destination"] == parsed["links"][0]["href"]


# ---------------------------------------------------------------------------
# LINK_INSIDE_HEADING / IMAGE_LINK_WITHOUT_TEXT, through a native crawl.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text
        self.status_code = 200
        self.headers = {"content-type": "text/html"}


_BODY = "Enough body text to be a real page. " * 40

_BAD_PLACEMENT_PAGE = f"""<html><head><title>A page whose heading points away</title></head>
<body><main>
<h1><a href="https://example.com/">Home</a></h1>
<a href="https://example.com/product"><img src="/p.jpg" alt=""></a>
<p>{_BODY}</p>
</main></body></html>"""

_CLEAN_PLACEMENT_PAGE = f"""<html><head><title>A page that names its own subject</title></head>
<body><main>
<h1>The subject of this page</h1>
<a href="https://example.com/product"><img src="/p.jpg" alt="The product"></a>
<p>{_BODY}</p>
</main></body></html>"""


def _run_crawl(mapping):
    crawl_result = collect_urls(
        list(mapping), fetcher=lambda url: mapping[url], sleeper=lambda _s: None
    )
    evidence = build_evidence(crawl_result)
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])
    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))
    run_rules(ctx)
    return ctx


def _fired_urls(ctx) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for issue in ctx.issues:
        out.setdefault(issue.check, set()).add(issue.target_url)
    return out


def _details(ctx, check_id, url):
    return next(i.details for i in ctx.issues if i.check == check_id and i.target_url == url)


def test_link_inside_heading_fires_on_the_linked_h1_and_not_on_the_plain_one():
    ctx = _run_crawl(
        {
            "https://example.com/bad": _FakeResponse(_BAD_PLACEMENT_PAGE),
            "https://example.com/clean": _FakeResponse(_CLEAN_PLACEMENT_PAGE),
        }
    )
    fired = _fired_urls(ctx)
    assert "https://example.com/bad" in fired.get("LINK_INSIDE_HEADING", set())
    assert "https://example.com/clean" not in fired.get("LINK_INSIDE_HEADING", set())
    evidence = _details(ctx, "LINK_INSIDE_HEADING", "https://example.com/bad")
    assert evidence["count"] == 1
    assert evidence["levels"] == ["h1"]
    assert evidence["first_links"][0]["destination"] == "https://example.com/"


def test_image_link_without_text_fires_on_the_empty_alt_and_not_on_the_described_one():
    ctx = _run_crawl(
        {
            "https://example.com/bad": _FakeResponse(_BAD_PLACEMENT_PAGE),
            "https://example.com/clean": _FakeResponse(_CLEAN_PLACEMENT_PAGE),
        }
    )
    fired = _fired_urls(ctx)
    assert "https://example.com/bad" in fired.get("IMAGE_LINK_WITHOUT_TEXT", set())
    assert "https://example.com/clean" not in fired.get("IMAGE_LINK_WITHOUT_TEXT", set())
    evidence = _details(ctx, "IMAGE_LINK_WITHOUT_TEXT", "https://example.com/bad")
    assert evidence["first_destinations"] == ["https://example.com/product"]


def test_one_finding_per_page_rather_than_one_per_link():
    two_bad = f"""<html><head><title>Two links in one heading</title></head><body><main>
    <h1><a href="https://example.com/a">A</a> <a href="https://example.com/b">B</a></h1>
    <p>{_BODY}</p></main></body></html>"""
    ctx = _run_crawl({"https://example.com/bad": _FakeResponse(two_bad)})
    assert len([i for i in ctx.issues if i.check == "LINK_INSIDE_HEADING"]) == 1
    assert _details(ctx, "LINK_INSIDE_HEADING", "https://example.com/bad")["count"] == 2


def test_placement_checks_skip_honestly_on_a_plain_sf_export(result):
    """``result`` (conftest.py) is a real-shaped Screaming Frog export. It carries
    no placement column at all, and neither check may read that as a clean page."""
    skipped = {s.id: s.reason for s in result.skipped}
    fired = {i.check for i in result.issues}
    for check_id in ("LINK_INSIDE_HEADING", "IMAGE_LINK_WITHOUT_TEXT"):
        assert check_id in skipped
        assert "native crawl only" in skipped[check_id]
        assert check_id not in fired


# ---------------------------------------------------------------------------
# The whole-graph pass: click depth and repeated edges, from an export.
# ---------------------------------------------------------------------------

INTERNAL_COLS = ["Address", "Content Type", "Status Code", "Status", "Indexability", "Crawl Depth"]
INLINK_COLS = ["Source", "Destination", "Type", "Follow", "Anchor Text", "Link Position"]


def _write(tmp_path, internal_rows, inlink_rows, name="exports"):
    d = tmp_path / name
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(INTERNAL_COLS)
        writer.writerows(internal_rows)
    with open(d / "all_inlinks.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(INLINK_COLS)
        writer.writerows(inlink_rows)
    return str(d)


def _page_row(url, depth):
    return [url, "text/html", "200", "OK", "Indexable", str(depth)]


def _chain(length: int):
    """A site shaped like the archive in the field notes: one long "next" chain.

    Crawl Depth is written as 0 on the home page and 1 on every other page, which
    is what a sitemap-seeded crawl records -- and is exactly the column that must
    not be mistaken for click depth.
    """
    urls = ["https://example.com/"] + [f"https://example.com/p{n}" for n in range(1, length)]
    internal = [_page_row(urls[0], 0)] + [_page_row(url, 1) for url in urls[1:]]
    inlinks = [
        [urls[n], urls[n + 1], "Hyperlink", "true", "Next", "content"] for n in range(length - 1)
    ]
    return urls, internal, inlinks


def _issues(res, check):
    return {i.target_url: i for i in res.issues if i.check == check}


def test_click_depth_walks_the_link_graph_rather_than_the_crawl_depth_column(tmp_path):
    urls, internal, inlinks = _chain(14)
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        log=lambda m: None,
    )
    depth = res.summary["internal_linking"]["click_depth"]
    assert depth["measured"] is True
    assert depth["seed"] == "https://example.com/"
    assert depth["max"] == 13
    assert depth["reachable"] == 14
    assert depth["unreachable"] == 0
    assert depth["within"] == {"3": 4, "5": 6, "10": 11}
    assert depth["histogram"]["13"] == 1
    # The column the walk deliberately does not read still says the site is flat.
    assert {row[5] for row in internal} == {"0", "1"}
    assert urls[-1] == "https://example.com/p13"


def test_deep_click_depth_fires_past_the_floor_and_stays_silent_within_it(tmp_path):
    _urls, internal, inlinks = _chain(14)
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        log=lambda m: None,
    )
    fired = _issues(res, "DEEP_CLICK_DEPTH")
    # Default floor is 10, so p11..p13 are over it and p1..p10 are not.
    assert set(fired) == {f"https://example.com/p{n}" for n in (11, 12, 13)}
    assert "https://example.com/p10" not in fired
    assert "DEEP_CLICK_DEPTH" not in {s.id for s in res.skipped}


def test_a_shallow_site_leaves_deep_click_depth_silent(tmp_path):
    internal = [
        _page_row("https://example.com/", 0),
        _page_row("https://example.com/a", 1),
        _page_row("https://example.com/b", 1),
    ]
    inlinks = [
        ["https://example.com/", "https://example.com/a", "Hyperlink", "true", "A", "content"],
        ["https://example.com/", "https://example.com/b", "Hyperlink", "true", "B", "nav"],
    ]
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        log=lambda m: None,
    )
    assert _issues(res, "DEEP_CLICK_DEPTH") == {}
    assert res.summary["internal_linking"]["click_depth"]["max"] == 1


def test_every_deep_click_depth_finding_names_the_floor_it_used(tmp_path):
    _urls, internal, inlinks = _chain(6)
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        config_overrides={"thresholds": {"click_depth_max": 2}},
        log=lambda m: None,
    )
    fired = _issues(res, "DEEP_CLICK_DEPTH")
    assert set(fired) == {f"https://example.com/p{n}" for n in (3, 4, 5)}
    evidence = fired["https://example.com/p5"].details
    assert evidence["floor_used"] == 2
    assert evidence["threshold"] == "thresholds.click_depth_max"
    assert evidence["click_depth"] == 5
    assert evidence["path"][0] == "https://example.com/"
    assert evidence["path"][-1] == "https://example.com/p5"
    assert res.summary["internal_linking"]["click_depth"]["floor_used"] == 2


def test_a_page_nothing_links_to_is_counted_unreachable_not_omitted(tmp_path):
    internal = [
        _page_row("https://example.com/", 0),
        _page_row("https://example.com/a", 1),
        _page_row("https://example.com/island", 1),
    ]
    inlinks = [
        ["https://example.com/", "https://example.com/a", "Hyperlink", "true", "A", "content"],
    ]
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        log=lambda m: None,
    )
    depth = res.summary["internal_linking"]["click_depth"]
    assert depth["pages"] == 3
    assert depth["reachable"] == 2
    assert depth["unreachable"] == 1


def test_click_depth_refuses_to_guess_a_seed_when_many_pages_claim_depth_zero(tmp_path):
    """The sitemap-seeded case from the field notes: 33 471 of 40 920 pages
    recorded Crawl Depth 0, so that column cannot say where the crawl began."""
    internal = [
        _page_row("https://example.com/", 0),
        _page_row("https://example.com/a", 0),
        _page_row("https://example.com/b", 0),
    ]
    inlinks = [
        ["https://example.com/", "https://example.com/a", "Hyperlink", "true", "A", "content"],
        ["https://example.com/a", "https://example.com/b", "Hyperlink", "true", "B", "content"],
    ]
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        log=lambda m: None,
    )
    reasons = {s.id: s.reason for s in res.skipped}
    assert "3 pages carry Crawl Depth 0" in reasons["DEEP_CLICK_DEPTH"]
    assert res.summary["internal_linking"]["click_depth"]["measured"] is False
    # The other half of the measurement still ran: positions and repeats are
    # per-edge facts and need no seed.
    assert res.summary["internal_linking"]["measured"] is True


def test_duplicate_internal_link_fires_on_the_repeated_block_and_not_on_the_clean_page(tmp_path):
    internal = [
        _page_row("https://example.com/", 0),
        _page_row("https://example.com/twice", 1),
        _page_row("https://example.com/once", 1),
    ]
    inlinks = [
        ["https://example.com/twice", "https://example.com/", "Hyperlink", "true", "Home", "nav"],
        [
            "https://example.com/twice",
            "https://example.com/",
            "Hyperlink",
            "true",
            "Home",
            "footer",
        ],
        ["https://example.com/once", "https://example.com/", "Hyperlink", "true", "Home", "nav"],
        ["https://example.com/", "https://example.com/twice", "Hyperlink", "true", "T", "content"],
        ["https://example.com/", "https://example.com/once", "Hyperlink", "true", "O", "content"],
    ]
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        log=lambda m: None,
    )
    fired = _issues(res, "DUPLICATE_INTERNAL_LINK")
    assert set(fired) == {"https://example.com/twice"}
    assert "https://example.com/once" not in fired
    assert fired["https://example.com/twice"].details["surplus_links"] == 1
    assert fired["https://example.com/twice"].details["repeats"] == [
        {"destination": "https://example.com/", "anchor": "Home", "count": 2}
    ]
    assert res.summary["internal_linking"]["duplicate_edges"] == 1


def test_the_same_destination_under_a_different_anchor_is_not_a_repeat(tmp_path):
    internal = [
        _page_row("https://example.com/", 0),
        _page_row("https://example.com/hub", 1),
    ]
    inlinks = [
        ["https://example.com/hub", "https://example.com/", "Hyperlink", "true", "Home", "nav"],
        [
            "https://example.com/hub",
            "https://example.com/",
            "Hyperlink",
            "true",
            "Start",
            "content",
        ],
        ["https://example.com/", "https://example.com/hub", "Hyperlink", "true", "Hub", "content"],
    ]
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        log=lambda m: None,
    )
    assert _issues(res, "DUPLICATE_INTERNAL_LINK") == {}
    assert res.summary["internal_linking"]["duplicate_edges"] == 0


def test_unclassified_edges_are_reported_as_unclassified_never_as_content(tmp_path):
    """The rule this whole feature is written under: a crawl run without
    ``link_position.classify`` records no position on any edge, and that must
    read as "nobody looked", not as a site whose links are all body copy."""
    internal = [
        _page_row("https://example.com/", 0),
        _page_row("https://example.com/a", 1),
        _page_row("https://example.com/b", 1),
    ]
    inlinks = [
        ["https://example.com/", "https://example.com/a", "Hyperlink", "true", "A", ""],
        ["https://example.com/", "https://example.com/b", "Hyperlink", "true", "B", "nav"],
    ]
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        log=lambda m: None,
    )
    block = res.summary["internal_linking"]
    assert block["by_position"] == {"nav": 1}
    assert "content" not in block["by_position"]
    assert block["unclassified"] == 1
    assert block["edges_total"] == 2
    assert block["classified_fraction"] == 0.5


def test_the_graph_checks_skip_and_the_summary_says_so_without_all_inlinks(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(INTERNAL_COLS)
        writer.writerow(_page_row("https://example.com/", 0))
    res = run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)
    reasons = {s.id: s.reason for s in res.skipped}
    fired = {i.check for i in res.issues}
    for check_id in ("DEEP_CLICK_DEPTH", "DUPLICATE_INTERNAL_LINK"):
        assert "all_inlinks" in reasons[check_id]
        assert check_id not in fired
    assert res.summary["internal_linking"]["measured"] is False
    assert "all_inlinks" in res.summary["internal_linking"]["reason"]


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


def test_depths_agree_with_the_paths_the_other_walk_returns():
    edges = [("seed", "a"), ("a", "b"), ("b", "d"), ("seed", "c"), ("c", "d")]
    depths = shortest_depths_from_seed(edges, "seed")
    paths = shortest_paths_from_seed(edges, "seed")
    assert depths == {key: len(path) - 1 for key, path in paths.items()}


def test_a_position_that_never_appeared_is_absent_rather_than_reported_as_zero():
    """A `header: 0` row would read as "this site has no header". It usually
    means an earlier rule matched first -- a menu inside <header> is `nav`."""
    summary = summarize_positions({"nav": 3, "content": 7, "": 2})
    assert summary["by_position"] == {"content": 7, "nav": 3}
    assert "header" not in summary["by_position"]
    assert summary["unclassified"] == 2


def test_the_depth_summary_names_the_floor_that_produced_its_verdicts():
    summary = summarize_depth({"a": 0, "b": 4}, ["a", "b", "c"], seed="a", floor=3)
    assert summary["floor_used"] == 3
    assert summary["unreachable"] == 1
    assert summary["max"] == 4
    assert summary["within"] == {"3": 1, "5": 2, "10": 2}


# ---------------------------------------------------------------------------
# Storage.
# ---------------------------------------------------------------------------


def test_the_stored_placement_survives_an_import_unchanged(legacy_run, tmp_path):
    pages = legacy_run / "pages.jsonl"
    rows = [json.loads(line) for line in pages.read_text().splitlines()]
    rows[0]["link_placement"] = {
        "in_heading": [{"level": 1, "destination": "https://x.test/", "anchor": "Home"}],
        "in_heading_total": 1,
        "image_no_text": [{"destination": "https://x.test/p"}],
        "image_no_text_total": 1,
    }
    pages.write_text("".join(json.dumps(row) + "\n" for row in rows))
    out = import_run(legacy_run, tmp_path / "scan.sqlite", producer_build=BUILD)
    con = open_scan(out)
    try:
        stored = con.execute(
            "SELECT link_placement_json FROM pages WHERE page_ordinal=0"
        ).fetchone()[0]
        assert json.loads(stored) == rows[0]["link_placement"]
    finally:
        con.close()


@pytest.mark.parametrize(
    "placement",
    [
        {"in_heading": [], "in_heading_total": 0},
        {
            "in_heading": [{"level": 9, "destination": "https://x.test/", "anchor": ""}],
            "in_heading_total": 1,
            "image_no_text": [],
            "image_no_text_total": 0,
        },
        {
            "in_heading": [],
            "in_heading_total": -1,
            "image_no_text": [],
            "image_no_text_total": 0,
        },
        {
            "in_heading": [],
            "in_heading_total": 0,
            "image_no_text": [{"destination": "https://x.test/", "extra": 1}],
            "image_no_text_total": 1,
        },
        "not an object at all",
    ],
)
def test_a_placement_record_of_the_wrong_shape_is_refused(legacy_run, tmp_path, placement):
    pages = legacy_run / "pages.jsonl"
    rows = [json.loads(line) for line in pages.read_text().splitlines()]
    rows[0]["link_placement"] = placement
    pages.write_text("".join(json.dumps(row) + "\n" for row in rows))
    with pytest.raises(ScanError, match="link_placement"):
        import_run(legacy_run, tmp_path / "bad.sqlite", producer_build=BUILD)


# ---------------------------------------------------------------------------
# A partial crawl proves no route.
# ---------------------------------------------------------------------------


def _context_from(tmp_path, internal, inlinks):
    from seohead.sf.core.inlinks import run_inlinks
    from seohead.sf.core.loader import load_exports

    exports = load_exports(_write(tmp_path, internal, inlinks))
    ctx = AuditContext(exports, load_config(None))
    run_inlinks(ctx)
    return ctx


def test_a_partial_crawl_withdraws_the_depth_verdict_it_did_produce(tmp_path):
    from seohead.sf.core.aggregate import aggregate

    _urls, internal, inlinks = _chain(14)
    ctx = _context_from(tmp_path, internal, inlinks)
    assert {i.check for i in ctx.issues} & {"DEEP_CLICK_DEPTH"}
    res = aggregate(ctx, {"input_mode": "crawl", "crawl_partial": True}, {}, {})
    assert "DEEP_CLICK_DEPTH" not in {i.check for i in res.issues}
    reasons = {s.id: s.reason for s in res.skipped}
    assert "crawl is partial" in reasons["DEEP_CLICK_DEPTH"]
    assert res.summary["internal_linking"]["click_depth"]["measured"] is False
    assert "crawl is partial" in res.summary["internal_linking"]["click_depth"]["reason"]
    # The per-edge halves survive: they describe the edges that were fetched.
    assert res.summary["internal_linking"]["by_position"] == {"content": 13}


def test_a_partial_crawl_that_found_nothing_deep_still_says_it_could_not_prove_it(tmp_path):
    """The false clean this guard exists for: a shallow-looking partial crawl must
    not read as "every page is within the floor" just because nothing fired."""
    from seohead.sf.core.aggregate import aggregate

    _urls, internal, inlinks = _chain(4)
    ctx = _context_from(tmp_path, internal, inlinks)
    assert "DEEP_CLICK_DEPTH" not in {i.check for i in ctx.issues}
    res = aggregate(ctx, {"input_mode": "crawl", "crawl_partial": True}, {}, {})
    reasons = {s.id: s.reason for s in res.skipped}
    assert "crawl is partial" in reasons["DEEP_CLICK_DEPTH"]


def test_a_finished_crawl_keeps_the_depth_verdict(tmp_path):
    from seohead.sf.core.aggregate import aggregate

    _urls, internal, inlinks = _chain(14)
    ctx = _context_from(tmp_path, internal, inlinks)
    res = aggregate(ctx, {"input_mode": "crawl", "crawl_partial": False}, {}, {})
    assert "DEEP_CLICK_DEPTH" in {i.check for i in res.issues}
    assert "DEEP_CLICK_DEPTH" not in {s.id for s in res.skipped}
    assert res.summary["internal_linking"]["click_depth"]["measured"] is True


def test_a_route_too_long_to_quote_is_truncated_at_both_ends_not_pasted_whole(tmp_path):
    """The archive that prompted this check has a 3 005-hop route to one article.
    The depth is stated in full; the route is quoted at its two ends."""
    _urls, internal, inlinks = _chain(40)
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, internal, inlinks),
        log=lambda m: None,
    )
    deepest = _issues(res, "DEEP_CLICK_DEPTH")["https://example.com/p39"].details
    assert deepest["click_depth"] == 39
    assert "path" not in deepest
    assert deepest["path_truncated"] is True
    assert deepest["path_start"][0] == "https://example.com/"
    assert deepest["path_end"][-1] == "https://example.com/p39"
    assert len(deepest["path_start"]) == len(deepest["path_end"]) == 5
