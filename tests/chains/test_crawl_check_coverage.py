"""Every check id must declare itself on a native crawl (issues #128, #165).

``run_inlinks`` was wired only into the Screaming Frog export pipeline
(``seohead/sf/core/audit.py``), never into the native crawl path
(``seohead/servers/handlers.py:crawl_site``). Its eighteen checks therefore
neither fired nor appeared in ``run.checks_skipped`` on a crawl: they were
silently absorbed into ``checks_silent``, which the coverage arithmetic reads
as "ran clean" -- so a run that never even looked at hreflang, anchor text, or
the link graph reported itself as having done so. Writing the general form of
that regression test (below) surfaced the same gap in two more modules:
``run_heuristics`` and most of ``sitemap_coverage.run_sitemap`` were not
called from ``crawl_site`` either (issue #165), for seventeen more check ids.
Both gaps are the same architectural bug -- ``crawl_site`` decides which audit
modules run and ``sf/core/audit.py:run_audit`` decides separately, so wiring a
module into one never wires it into the other.

This test pins the general shape of the bug, not the specific check ids: for
every check id, a crawl must place it in exactly one of fired / skipped /
silent, and "silent" must mean the check's owning pipeline function actually
ran -- never that nobody called it. Whoever adds the next check to any of
``run_rules``, ``run_inlinks``, ``run_heuristics`` or ``sitemap_coverage.
run_sitemap`` is covered by construction; whoever wires a *new* module into
the analyzer without also wiring it into ``crawl_site`` reproduces this bug
shape and this test catches it.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from seohead.crawl import link_findings
from seohead.servers import handlers
from seohead.sf.core import heuristics as heuristics_module
from seohead.sf.core import inlinks as inlinks_module
from seohead.sf.core import rules as rules_module
from seohead.sf.core import sitemap_coverage as sitemap_module
from seohead.sf.core.registry import CHECKS
from tests.chains.chain_site import run_chain_site

# INLINK_BOILERPLATE_ONLY *is* wired in -- crawl_site adds it directly -- but
# only when link_position.classify is turned on, which this test's crawls
# leave at its default (off). Excluded for the same reason as any other
# not-exercised-here check: this test proves nothing about a code path it
# never runs.
_NOT_WIRED_INTO_CRAWL = frozenset({"INLINK_BOILERPLATE_ONLY"})

# Same shape again (issue #125): UNSAFE_CROSS_ORIGIN_LINK and PROTOCOL_RELATIVE_LINK are
# wired in -- crawl_site adds them directly, from seohead.crawl.link_findings -- but only
# when link_attributes.capture is on, which this test's crawls also leave at its default
# (off). The other four checks issue #125 added (OUTLINK_TO_LOCALHOST,
# FOLLOW_AND_NOFOLLOW_INLINKS, FORM_URL_INSECURE, FORM_ON_HTTP_URL) need no such setting and
# are genuinely exercised by every crawl below, so they are not excluded here.
#
# What is no longer here is the whole computed body this set used to open with: every check
# whose source was "heuristic" or "sitemap" was excluded wholesale, because neither module
# was called from a crawl at all. Issue #165 wired both in, so the exclusion that stood for
# "this pipeline never runs" is gone and only the three settings-gated ids remain.
_NOT_WIRED_INTO_CRAWL |= {"UNSAFE_CROSS_ORIGIN_LINK", "PROTOCOL_RELATIVE_LINK"}

# SITEMAP_ORPHAN and URL_NOT_IN_SITEMAP are the two sitemap-sourced ids
# crawl_site answers itself, from its own link-graph reconciliation
# (reconcile_sitemap), never through sitemap_coverage.run_sitemap -- the same
# split ``sitemap_coverage.run_sitemap``'s own ``_emit_from_export`` calls for
# these two ids already respect (they read an SF export frame neither path
# ever has, so they contribute nothing here regardless of which function ran).
_SITEMAP_RECONCILED_INLINE = frozenset({"SITEMAP_ORPHAN", "URL_NOT_IN_SITEMAP"})


def _owning_spy(check_id: str, source: str, spies: dict[str, object]):
    """Which pipeline function's spy proves this check id was genuinely evaluated.

    Registry ``source`` strings are a human-readable label, not a machine contract, so this
    is deliberately the minimum classification the checks actually need rather than a parse
    of every string shape in the registry -- new sources fall through to ``run_rules``, which
    is exactly right for every check that reads ``internal_all`` columns directly.
    """
    if source.startswith("inlinks:"):
        return spies["inlinks"]
    if check_id == "NEAR_DUPLICATE":
        # Dual-sourced: rules.py answers it from SF's native "No. Near Duplicates"
        # column (never present on a native crawl) and heuristics.py answers it
        # from stored HTML (also never present here) -- on a crawl this id's own
        # skip always comes from heuristics.py's check_content_duplication.
        return spies["heuristics"]
    if source in ("heuristic", "SF-derived+heuristic"):
        return spies["heuristics"]
    if source == "sitemap":
        return spies["sitemap"]
    if source == "crawl:link_findings":
        return spies[check_id]
    return spies["rules"]


# Same shape again (issue #125): UNSAFE_CROSS_ORIGIN_LINK and PROTOCOL_RELATIVE_LINK are
# wired in -- crawl_site adds them directly, from seohead.crawl.link_findings -- but only
# when link_attributes.capture is on, which this test's crawls also leave at its default
# (off). The other four checks issue #125 added (OUTLINK_TO_LOCALHOST,
# FOLLOW_AND_NOFOLLOW_INLINKS, FORM_URL_INSECURE, FORM_ON_HTTP_URL) need no such setting and
# are genuinely exercised by every crawl below, so they are not excluded here.
_NOT_WIRED_INTO_CRAWL |= {"UNSAFE_CROSS_ORIGIN_LINK", "PROTOCOL_RELATIVE_LINK"}


@pytest.fixture(scope="module")
def site(monkeypatch_module):
    # The crawler refuses private-network targets unless explicitly authorized; a loopback
    # fixture is exactly the case that authorization exists for.
    monkeypatch_module.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    with run_chain_site() as base_url:
        yield base_url


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    patch_ = MonkeyPatch()
    yield patch_
    patch_.undo()


def test_every_wired_check_is_fired_skipped_or_provably_evaluated(site, tmp_path):
    with (
        patch.object(rules_module, "run_rules", wraps=rules_module.run_rules) as spy_rules,
        patch.object(
            inlinks_module, "run_inlinks", wraps=inlinks_module.run_inlinks
        ) as spy_inlinks,
        patch.object(
            heuristics_module, "run_heuristics", wraps=heuristics_module.run_heuristics
        ) as spy_heuristics,
        patch.object(
            sitemap_module, "run_sitemap", wraps=sitemap_module.run_sitemap
        ) as spy_sitemap,
        patch.object(
            link_findings,
            "outlinks_to_localhost",
            wraps=link_findings.outlinks_to_localhost,
        ) as spy_localhost,
        patch.object(
            link_findings,
            "follow_and_nofollow_inlinks",
            wraps=link_findings.follow_and_nofollow_inlinks,
        ) as spy_follow_mix,
        patch.object(
            link_findings,
            "form_url_insecure",
            wraps=link_findings.form_url_insecure,
        ) as spy_insecure_form,
        patch.object(
            link_findings,
            "forms_on_http_pages_with_password",
            wraps=link_findings.forms_on_http_pages_with_password,
        ) as spy_http_password_form,
    ):
        result = handlers.crawl_site(
            url=f"{site}/",
            out_dir=str(tmp_path),
            max_urls=30,
            sitemap=f"{site}/sitemap.xml",
        )

    assert spy_rules.called, "run_rules must run on every native crawl"
    assert spy_inlinks.called, "run_inlinks must run on every native crawl (issue #128)"
    assert spy_heuristics.called, "run_heuristics must run on every native crawl (issue #165)"
    assert spy_sitemap.called, "run_sitemap must run on every native crawl (issue #165)"
    # Proof the inline sitemap-reconciliation block ran, since SITEMAP_ORPHAN and
    # URL_NOT_IN_SITEMAP are added there directly rather than through a run_* function.
    assert result["summary"].get("sitemap"), "the sitemap block must run when sitemap= is given"

    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    fired = {issue["check"] for issue in audit["issues"]}
    skipped = {s["id"] for s in audit["run"]["checks_skipped"]}

    spies = {
        "rules": spy_rules,
        "inlinks": spy_inlinks,
        "heuristics": spy_heuristics,
        "sitemap": spy_sitemap,
        "OUTLINK_TO_LOCALHOST": spy_localhost,
        "FOLLOW_AND_NOFOLLOW_INLINKS": spy_follow_mix,
        "FORM_URL_INSECURE": spy_insecure_form,
        "FORM_ON_HTTP_URL": spy_http_password_form,
    }
    in_scope = set(CHECKS) - _NOT_WIRED_INTO_CRAWL
    for check_id in sorted(in_scope):
        if check_id in fired or check_id in skipped:
            continue  # declared one way or the other -- provably evaluated
        # Silent: neither fired nor skipped. That is only honest if the function
        # that would have done either actually ran this check's evaluation.
        if check_id in _SITEMAP_RECONCILED_INLINE:
            assert result["summary"].get("sitemap"), f"{check_id} is silent but never evaluated"
            continue
        source = CHECKS[check_id]["source"]
        spy = _owning_spy(check_id, source, spies)
        assert spy.called, f"{check_id} is silent but its owning pipeline function never ran"


def test_the_excluded_set_names_only_checks_actually_absent_from_the_registry():
    """A ratchet: this set may shrink further, but every id in it must still be
    a real check this test's own crawl configuration genuinely never exercises."""
    assert set(CHECKS) >= _NOT_WIRED_INTO_CRAWL
    assert {
        "INLINK_BOILERPLATE_ONLY",
        "UNSAFE_CROSS_ORIGIN_LINK",
        "PROTOCOL_RELATIVE_LINK",
    } == _NOT_WIRED_INTO_CRAWL


def test_the_twelve_checks_issue_128_reported_are_no_longer_silently_uninvoked(site, tmp_path):
    """The reproduction from the issue, pinned directly.

    Five of the twelve have real evidence on this fixture's own hyperlink
    graph (anchor text, discovery path, link score, inlink composition) and
    are provably invoked even though the fixture trips none of them; the
    other seven have no evidence a native crawl can ever produce (hreflang was
    never parsed; no resource inventory exists) and must skip by name instead.
    Neither group may land in ``checks_silent`` without having actually run.
    """
    with patch.object(inlinks_module, "run_inlinks", wraps=inlinks_module.run_inlinks) as spy:
        handlers.crawl_site(
            url=f"{site}/", out_dir=str(tmp_path), max_urls=30, sitemap=f"{site}/sitemap.xml"
        )
    assert spy.called

    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    fired = {issue["check"] for issue in audit["issues"]}
    reasons = {s["id"]: s["reason"] for s in audit["run"]["checks_skipped"]}

    evaluated_but_clean_on_this_fixture = {
        "GENERIC_ANCHOR_TEXT",
        "ONLY_NOFOLLOW_INLINKS",
        "ONLY_NONINDEXABLE_SOURCE_INLINKS",
        "DEEP_DISCOVERY_PATH",
        "LOW_LINK_SCORE",
    }
    for check_id in evaluated_but_clean_on_this_fixture:
        assert check_id not in fired, check_id
        assert check_id not in reasons, check_id  # silent -- but only valid because spy.called

    must_skip_by_name = {
        "INSECURE_SUBRESOURCE",
        "HREFLANG_INVALID_CODE",
        "HREFLANG_MULTIPLE_ENTRIES",
        "HREFLANG_MISSING_SELF_REFERENCE",
        "HREFLANG_MISSING_XDEFAULT",
        "HREFLANG_NOT_CANONICAL",
        "HREFLANG_MISSING_RETURN_LINK",
    }
    for check_id in must_skip_by_name:
        assert check_id in reasons, f"{check_id} must skip honestly, got: {reasons.get(check_id)}"
        assert reasons[check_id], check_id  # the reason itself must be non-empty


def test_the_seventeen_checks_issue_165_reported_are_no_longer_silently_uninvoked(site, tmp_path):
    """The reproduction from issue #165, pinned directly.

    Six of the seventeen have no evidence a native crawl can ever produce (DOM depth/node
    counts and the near-duplicate fallback need HTML stored to disk, which crawl_site never
    writes; three sitemap-comparison ids read an SF export a crawl never has) and must skip by
    name. Nine have real evidence on this fixture -- HTML weight, titles, and the sitemap's own
    protocol limits, robots-blocked-resources check and lastmod dates -- but this fixture trips
    none of them, so they are provably invoked and clean rather than skipped. The remaining two
    (this fixture's robots.txt never declares its sitemap, and one of its three sitemap URLs is
    never linked) are real, provable findings, not just a reachable skip branch.
    """
    with (
        patch.object(
            heuristics_module, "run_heuristics", wraps=heuristics_module.run_heuristics
        ) as spy_heuristics,
        patch.object(
            sitemap_module, "run_sitemap", wraps=sitemap_module.run_sitemap
        ) as spy_sitemap,
    ):
        handlers.crawl_site(
            url=f"{site}/", out_dir=str(tmp_path), max_urls=30, sitemap=f"{site}/sitemap.xml"
        )
    assert spy_heuristics.called
    assert spy_sitemap.called

    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    fired = {issue["check"] for issue in audit["issues"]}
    reasons = {s["id"]: s["reason"] for s in audit["run"]["checks_skipped"]}

    evaluated_but_clean_on_this_fixture = {
        "HTML_BLOAT",
        "LARGE_HTML",
        "TITLE_TEMPLATED",
        "ROBOTS_BLOCKS_RESOURCES",
        "SITEMAP_FETCH_INCOMPLETE",
        "SITEMAP_STALE_LASTMOD",
        "SITEMAP_TOO_LARGE",
        "SITEMAP_TOO_MANY_URLS",
        "SITEMAP_URL_DUPLICATED",
    }
    for check_id in evaluated_but_clean_on_this_fixture:
        assert check_id not in fired, check_id
        assert check_id not in reasons, check_id  # silent -- but only valid because spy.called

    must_skip_by_name = {
        "DOM_TOO_DEEP",
        "DOM_TOO_MANY_NODES",
        "NEAR_DUPLICATE",
        "SITEMAP_URL_3XX",
        "SITEMAP_URL_4XX_5XX",
        "SITEMAP_URL_NON_INDEXABLE",
    }
    for check_id in must_skip_by_name:
        assert check_id in reasons, f"{check_id} must skip honestly, got: {reasons.get(check_id)}"
        assert reasons[check_id], check_id  # the reason itself must be non-empty

    # Real findings, not just reachable skips: this fixture's robots.txt allows
    # everything but never declares a Sitemap: directive, and its sitemap names
    # /orphan/ -- a URL nothing on the site links to.
    assert "SITEMAP_NOT_IN_ROBOTS" in fired
    assert "SITEMAP_DESYNC" in fired


def test_a_check_the_crawl_graph_can_answer_actually_fires_on_real_evidence(tmp_path):
    """Not just "reaches a skip branch": LOW_LINK_SCORE must fire on a link graph
    the crawl's own hyperlink evidence can prove, or the fix only ever produces
    honest skips and never the answers the issue asked for.

    A dense core of twelve pages, all linking to each other and back to the
    hub, plus one page the hub links to and nothing else reaches: the core
    pages each hold ~12 inbound edges, ``/lonely`` holds exactly one, and
    ``compute_link_scores`` (PageRank power iteration) puts its score at
    ~0.22x the site median -- comfortably under the 0.25 default threshold.
    """
    import http.server
    import threading

    core = [f"/c{n}" for n in range(1, 13)]
    core_links = "".join(f'<a href="{c}">{c}</a>' for c in core)
    pages = {
        "/": f"<html><head><title>Hub</title></head><body>{core_links}"
        f'<a href="/lonely">lonely</a></body></html>',
        "/lonely": "<html><head><title>Lonely</title></head><body>nothing links back here except "
        "the hub itself, and it links to no one</body></html>",
    }
    for c in core:
        others = "".join(f'<a href="{c2}">{c2}</a>' for c2 in core if c2 != c)
        pages[c] = (
            f'<html><head><title>{c}</title></head><body><a href="/">hub</a>{others}</body></html>'
        )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path.split("?", 1)[0])
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        result = handlers.crawl_site(url=f"{base}/", out_dir=str(tmp_path), max_urls=30)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["urls_collected"] == len(pages)
    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    fired = {issue["check"]: issue for issue in audit["issues"]}
    assert "LOW_LINK_SCORE" in fired, "a real, provable outlier must fire, not just skip honestly"
    assert fired["LOW_LINK_SCORE"]["target_url"] == f"{base}/lonely"
