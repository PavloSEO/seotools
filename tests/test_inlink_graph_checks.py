"""Whole-graph passes over the complete ``all_inlinks`` inventory: inlink
composition, the discovery path, and insecure subresources (issue #15, items
7, 10 and 11). Each is an aggregate or cross-join that is only knowable once
every link on the site — not only the broken ones — has been collected, so
all three read the same export and honestly skip without it.
"""

from __future__ import annotations

import csv

from seohead.sf.core.audit import run_audit
from seohead.sf.core.crawl_path import shortest_paths_from_seed

INTERNAL_COLS = ["Address", "Content Type", "Status Code", "Status", "Indexability", "Crawl Depth"]
INLINK_COLS = ["Source", "Destination", "Type", "Follow"]


def _write(tmp_path, internal_rows, inlink_rows):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(INTERNAL_COLS)
        w.writerows(internal_rows)
    with open(d / "all_inlinks.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(INLINK_COLS)
        w.writerows(inlink_rows)
    return str(d)


def _fired(res, check):
    return {i.target_url: i for i in res.issues if i.check == check}


# -- shortest_paths_from_seed (pure) ---------------------------------------


def test_shortest_path_is_breadth_first_not_first_discovered():
    # a longer direct edge exists (a -> d) but the shorter route through b/c
    # must win, since BFS explores by hop count, not by edge order.
    edges = [("seed", "a"), ("a", "b"), ("b", "d"), ("seed", "c"), ("c", "d")]
    paths = shortest_paths_from_seed(edges, "seed")
    assert paths["d"] == ["seed", "c", "d"]
    assert paths["seed"] == ["seed"]


def test_unreachable_nodes_have_no_path():
    paths = shortest_paths_from_seed([("seed", "a"), ("x", "y")], "seed")
    assert "y" not in paths
    assert "x" not in paths


# -- composition / discovery path / insecure subresources, wired -----------


def test_composition_and_path_checks_skip_without_all_inlinks(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(INTERNAL_COLS)
        w.writerow(["https://example.com/", "text/html", "200", "OK", "Indexable", "0"])
    res = run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)
    reasons = {s.id: s.reason for s in res.skipped}
    for check_id in (
        "ONLY_NOFOLLOW_INLINKS",
        "ONLY_NONINDEXABLE_SOURCE_INLINKS",
        "DEEP_DISCOVERY_PATH",
        "INSECURE_SUBRESOURCE",
    ):
        assert "all_inlinks" in reasons[check_id]


def test_a_page_reached_only_by_nofollow_is_flagged(tmp_path):
    internal_rows = [
        ["https://example.com/", "text/html", "200", "OK", "Indexable", "0"],
        ["https://example.com/a", "text/html", "200", "OK", "Indexable", "1"],
        ["https://example.com/b", "text/html", "200", "OK", "Indexable", "1"],
        ["https://example.com/target", "text/html", "200", "OK", "Indexable", "2"],
    ]
    inlink_rows = [
        ["https://example.com/a", "https://example.com/target", "Hyperlink", "false"],
        ["https://example.com/b", "https://example.com/target", "Hyperlink", "false"],
    ]
    exports_dir = _write(tmp_path, internal_rows, inlink_rows)
    res = run_audit(input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None)
    fired = _fired(res, "ONLY_NOFOLLOW_INLINKS")
    assert set(fired) == {"https://example.com/target"}


def test_one_follow_inlink_clears_only_nofollow(tmp_path):
    internal_rows = [
        ["https://example.com/a", "text/html", "200", "OK", "Indexable", "1"],
        ["https://example.com/b", "text/html", "200", "OK", "Indexable", "1"],
        ["https://example.com/target", "text/html", "200", "OK", "Indexable", "2"],
    ]
    inlink_rows = [
        ["https://example.com/a", "https://example.com/target", "Hyperlink", "false"],
        ["https://example.com/b", "https://example.com/target", "Hyperlink", "true"],
    ]
    exports_dir = _write(tmp_path, internal_rows, inlink_rows)
    res = run_audit(input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None)
    assert _fired(res, "ONLY_NOFOLLOW_INLINKS") == {}


def test_nofollow_fragment_destination_still_flags_the_page(tmp_path):
    # #313: the only inlink to /target is a nofollow link to its
    # "#details" fragment. Grouping by the raw destination formed a
    # separate bucket for the fragment-bearing edge that could never
    # resolve to the crawled /target page, so the finding never fired.
    internal_rows = [
        ["https://example.test/", "text/html", "200", "OK", "Indexable", "0"],
        ["https://example.test/target", "text/html", "200", "OK", "Indexable", "1"],
    ]
    inlink_rows = [
        ["https://example.test/", "https://example.test/target#details", "Hyperlink", "false"],
    ]
    exports_dir = _write(tmp_path, internal_rows, inlink_rows)
    res = run_audit(input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None)
    fired = _fired(res, "ONLY_NOFOLLOW_INLINKS")
    assert set(fired) == {"https://example.test/target"}


def test_a_followed_different_fragment_clears_the_finding(tmp_path):
    # Negative control for #313: a nofollow edge to one fragment plus a
    # followed edge to a different fragment of the same page must still
    # group as one page-level composition and clear the finding — the fix
    # must not overcorrect into always firing once any fragment is seen.
    internal_rows = [
        ["https://example.test/", "text/html", "200", "OK", "Indexable", "0"],
        ["https://example.test/other", "text/html", "200", "OK", "Indexable", "1"],
        ["https://example.test/target", "text/html", "200", "OK", "Indexable", "1"],
    ]
    inlink_rows = [
        ["https://example.test/", "https://example.test/target#details", "Hyperlink", "false"],
        ["https://example.test/other", "https://example.test/target#top", "Hyperlink", "true"],
    ]
    exports_dir = _write(tmp_path, internal_rows, inlink_rows)
    res = run_audit(input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None)
    assert _fired(res, "ONLY_NOFOLLOW_INLINKS") == {}


def test_a_page_linked_only_from_noindex_sources_is_flagged(tmp_path):
    internal_rows = [
        ["https://example.com/a", "text/html", "200", "OK", "Non-Indexable", "1"],
        ["https://example.com/b", "text/html", "200", "OK", "Non-Indexable", "1"],
        ["https://example.com/target", "text/html", "200", "OK", "Indexable", "2"],
    ]
    inlink_rows = [
        ["https://example.com/a", "https://example.com/target", "Hyperlink", "true"],
        ["https://example.com/b", "https://example.com/target", "Hyperlink", "true"],
    ]
    exports_dir = _write(tmp_path, internal_rows, inlink_rows)
    res = run_audit(input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None)
    fired = _fired(res, "ONLY_NONINDEXABLE_SOURCE_INLINKS")
    assert set(fired) == {"https://example.com/target"}


def test_deep_discovery_path_reports_the_actual_route(tmp_path):
    internal_rows = [["https://example.com/", "text/html", "200", "OK", "Indexable", "0"]]
    inlink_rows = []
    chain = ["https://example.com/"] + [f"https://example.com/l{i}" for i in range(6)]
    for i in range(1, len(chain)):
        internal_rows.append([chain[i], "text/html", "200", "OK", "Indexable", str(i)])
        inlink_rows.append([chain[i - 1], chain[i], "Hyperlink", "true"])
    exports_dir = _write(tmp_path, internal_rows, inlink_rows)
    res = run_audit(input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None)
    fired = _fired(res, "DEEP_DISCOVERY_PATH")
    deepest = chain[-1]
    assert deepest in fired
    assert fired[deepest].details["path"] == chain
    assert fired[deepest].details["hops"] == len(chain) - 1


def test_insecure_subresource_fires_on_an_http_image_from_https(tmp_path):
    internal_rows = [
        ["https://example.com/", "text/html", "200", "OK", "Indexable", "0"],
    ]
    inlink_rows = [
        ["https://example.com/", "http://cdn.example.com/logo.png", "Image", ""],
        ["https://example.com/", "https://example.com/about", "Hyperlink", "true"],
    ]
    exports_dir = _write(tmp_path, internal_rows, inlink_rows)
    res = run_audit(input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None)
    fired = _fired(res, "INSECURE_SUBRESOURCE")
    assert set(fired) == {"https://example.com/"}
    assert fired["https://example.com/"].details["resources"] == ["http://cdn.example.com/logo.png"]


def test_insecure_subresource_defers_to_the_native_mixed_content_export(tmp_path):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(INTERNAL_COLS)
        w.writerow(["https://example.com/", "text/html", "200", "OK", "Indexable", "0"])
    with open(d / "all_inlinks.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(INLINK_COLS)
        w.writerow(["https://example.com/", "http://cdn.example.com/logo.png", "Image", ""])
    with open(d / "security_mixed_content.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Address"])
        w.writerow(["https://example.com/"])
    res = run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)
    reasons = {s.id: s.reason for s in res.skipped}
    assert "INSECURE_SUBRESOURCE" in reasons
    assert "MIXED_CONTENT" in {i.check for i in res.issues}
