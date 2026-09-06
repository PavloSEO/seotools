"""Redirect chain resolution and loop detection over a stored redirect map.

These are the network-free second-pass computations from issue #15: chain
resolution and loop detection are global properties of the redirect graph, so
a hop's terminal status is only knowable once every hop has been fetched.
``resolve_redirect_chains`` walks a finished map; the integration tests below
check that ``check_redirect_chains`` reaches for it when the native
Screaming Frog Redirect Chains report is absent.
"""

from __future__ import annotations

import json
import os

from seohead.sf.core.audit import run_audit
from seohead.sf.core.redirect_chains import resolve_redirect_chains

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")

INTERNAL_ALL_HEADER = (
    "Address,Content Type,Status Code,Status,Indexability,Indexability Status,"
    "Title 1,Title 1 Length,Meta Description 1,Meta Description 1 Length,"
    "H1-1,H1-1 Length,H1-2,H2-1,Canonical Link Element 1,Size (bytes),Word Count,"
    "Text Ratio,Crawl Depth,Inlinks,Outlinks,Response Time,Hash,Meta Robots 1,Redirect URL"
)


def _row(url: str, status: int, redirect_url: str = "") -> str:
    """One Internal:All row with only the columns chain resolution reads."""
    indexability = "Indexable" if status == 200 else "Non-Indexable"
    reason = "" if status == 200 else ("Redirected" if 300 <= status < 400 else "Client Error")
    return (
        f"{url},text/html,{status},OK,{indexability},{reason},"
        f"Title,5,Desc,4,H1,2,,,,"
        f"1000,100,50.0,0,1,1,0.1,HASH,,{redirect_url}"
    )


def _write_internal_all(tmp_path, rows: list[str]) -> str:
    work = tmp_path / "exports"
    work.mkdir()
    (work / "internal_all.csv").write_text(INTERNAL_ALL_HEADER + "\n" + "\n".join(rows) + "\n")
    return str(work)


# -- pure resolver --------------------------------------------------------


def test_a_single_hop_is_not_a_chain():
    outcome = resolve_redirect_chains({"https://x/a": "https://x/b"})
    assert outcome["https://x/a"] == {"kind": "single", "hops": 1, "final_url": "https://x/b"}


def test_two_hops_resolve_as_a_chain_with_the_final_url():
    redirect_map = {"https://x/a": "https://x/b", "https://x/b": "https://x/c"}
    outcome = resolve_redirect_chains(redirect_map)
    assert outcome["https://x/a"] == {"kind": "chain", "hops": 2, "final_url": "https://x/c"}
    # b is itself an ordinary single-hop redirect on its own terms
    assert outcome["https://x/b"] == {"kind": "single", "hops": 1, "final_url": "https://x/c"}


def test_a_cycle_is_only_provable_when_a_target_reappears():
    redirect_map = {"https://x/a": "https://x/b", "https://x/b": "https://x/a"}
    outcome = resolve_redirect_chains(redirect_map)
    assert outcome["https://x/a"] == {"kind": "loop", "hops": 2, "final_url": None}
    assert outcome["https://x/b"] == {"kind": "loop", "hops": 2, "final_url": None}


def test_a_url_that_redirects_to_itself_is_a_loop_of_one():
    outcome = resolve_redirect_chains({"https://x/a": "https://x/a"})
    assert outcome["https://x/a"]["kind"] == "loop"


def test_a_chain_longer_than_the_hop_cap_is_left_unresolved_not_asserted():
    # a genuine (non-cyclic) chain past the cap must not be reported as
    # either a resolved chain or a proven loop — the caller cannot support
    # either conclusion from a walk that was cut short.
    redirect_map = {f"https://x/{i}": f"https://x/{i + 1}" for i in range(5)}
    outcome = resolve_redirect_chains(redirect_map, hop_cap=2)
    assert outcome["https://x/0"]["kind"] == "unresolved"
    assert outcome["https://x/0"]["final_url"] is None


def test_unrelated_chains_are_resolved_independently():
    redirect_map = {
        "https://x/a": "https://x/b",
        "https://x/b": "https://x/c",
        "https://y/p": "https://y/q",
    }
    outcome = resolve_redirect_chains(redirect_map)
    assert outcome["https://x/a"]["kind"] == "chain"
    assert outcome["https://y/p"]["kind"] == "single"


# -- wired into the audit, no native Redirect Chains report present -------


def test_chain_and_loop_fire_from_internal_all_alone(tmp_path):
    """No Redirects:Redirect Chains export — only Internal:All's own column."""
    rows = [
        _row("https://example.com/a", 301, "https://example.com/b"),
        _row("https://example.com/b", 301, "https://example.com/c"),
        _row("https://example.com/c", 200),
        _row("https://example.com/loop1", 301, "https://example.com/loop2"),
        _row("https://example.com/loop2", 301, "https://example.com/loop1"),
        _row("https://example.com/plain", 301, "https://example.com/c"),
    ]
    exports_dir = _write_internal_all(tmp_path, rows)
    result = json.loads(
        json.dumps(
            run_audit(
                input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None
            ).to_json()
        )
    )
    issues = result["issues"]
    chain_urls = {i["target_url"] for i in issues if i["check"] == "REDIRECT_CHAIN"}
    loop_urls = {i["target_url"] for i in issues if i["check"] == "REDIRECT_LOOP"}
    skipped = {s["id"] for s in result["run"]["checks_skipped"]}

    assert chain_urls == {"https://example.com/a"}
    assert loop_urls == {"https://example.com/loop1", "https://example.com/loop2"}
    # a plain, one-hop redirect must not be reported as a chain
    assert "https://example.com/plain" not in chain_urls
    assert "REDIRECT_CHAIN" not in skipped
    assert "REDIRECT_LOOP" not in skipped


def test_no_redirect_data_at_all_is_skipped_by_name(tmp_path):
    """Internal:All without a Redirect URL column names the missing input."""
    exports_dir = os.path.join(FIXTURES)  # ships with no Redirect URL column
    result = json.loads(
        json.dumps(
            run_audit(
                input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None
            ).to_json()
        )
    )
    reasons = {s["id"]: s["reason"] for s in result["run"]["checks_skipped"]}
    assert "REDIRECT_CHAIN" in reasons
    assert "REDIRECT_LOOP" in reasons
    assert "Redirect URL" in reasons["REDIRECT_CHAIN"]


def test_a_threshold_change_costs_no_requests_and_changes_the_result(tmp_path):
    """Re-running with a tighter hop cap must reclassify without new evidence."""
    rows = [
        _row("https://example.com/a", 301, "https://example.com/b"),
        _row("https://example.com/b", 301, "https://example.com/c"),
        _row("https://example.com/c", 200),
    ]
    exports_dir = _write_internal_all(tmp_path, rows)
    from seohead.sf.config import load_config

    tight = load_config(None)
    tight["thresholds"]["redirect_hop_cap"] = 1
    result = json.loads(
        json.dumps(
            run_audit(
                input_mode="parse-exports",
                exports_dir=exports_dir,
                config=tight,
                log=lambda m: None,
            ).to_json()
        )
    )
    chain_urls = {i["target_url"] for i in result["issues"] if i["check"] == "REDIRECT_CHAIN"}
    # the walk from "a" needs 2 hops to resolve; a cap of 1 cannot prove either a clean
    # terminus or a loop -- but that is not "no chain here" (#447): it must still surface
    # as evidence, flagged unresolved rather than silently dropped.
    assert chain_urls == {"https://example.com/a", "https://example.com/b"}
    unresolved = {
        i["target_url"]
        for i in result["issues"]
        if i["check"] == "REDIRECT_CHAIN" and i["details"].get("unresolved")
    }
    assert unresolved == {"https://example.com/a", "https://example.com/b"}


# -- wired into a native seohead crawl (list mode), no Screaming Frog at all --


def test_a_native_crawl_resolves_its_own_chains_without_screaming_frog():
    """The findings the issue titles "cannot be computed while crawling" run
    fine on a native crawl's own evidence — no SF Redirect Chains export
    involved, since ``build_evidence`` already carries a Redirect URL per
    page and ``check_redirect_chains`` no longer requires the native report.
    """
    from seohead.crawl.collect import CrawlResult, PageRecord
    from seohead.crawl.evidence import build_evidence
    from seohead.sf.config import load_config
    from seohead.sf.core.context import AuditContext
    from seohead.sf.core.loader import LoadedExports
    from seohead.sf.core.rules import run_rules

    def page(url: str, status: int, redirect_url: str = "") -> PageRecord:
        rec = PageRecord(url=url)
        rec.status_code = status
        rec.redirect_url = redirect_url
        rec.content_type = "text/html"
        return rec

    pages = [
        page("https://example.com/a", 301, "https://example.com/b"),
        page("https://example.com/b", 301, "https://example.com/c"),
        page("https://example.com/c", 200),
        page("https://example.com/loop1", 301, "https://example.com/loop2"),
        page("https://example.com/loop2", 301, "https://example.com/loop1"),
    ]
    evidence = build_evidence(CrawlResult(pages=pages))

    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])

    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))
    run_rules(ctx)

    chain_urls = {i.target_url for i in ctx.issues if i.check == "REDIRECT_CHAIN"}
    loop_urls = {i.target_url for i in ctx.issues if i.check == "REDIRECT_LOOP"}
    assert chain_urls == {"https://example.com/a"}
    assert loop_urls == {"https://example.com/loop1", "https://example.com/loop2"}
