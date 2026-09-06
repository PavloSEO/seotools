"""Crawl-to-crawl comparison: fixed vs merely-not-recrawled must not look alike.

The distinction between "left" and "disappeared" is the entire value of this
module. A naive diff of two finding sets cannot make it; this one can because
it also looks at which URLs were actually crawled each time.
"""

import pytest

from seohead.sf.core.compare import CompareError, compare, preflight


def _audit(urls, issues, **run):
    return {
        "run": {"generated_at": "t", **run},
        "pages": [{"url": u} for u in urls],
        "issues": [{"check": c, "target_url": u} for c, u in issues],
    }


def test_a_fixed_page_lands_in_left_not_disappeared():
    """Still crawled, no longer matching — a real fix."""
    before = _audit(["https://e.com/a"], [("BROKEN", "https://e.com/a")])
    after = _audit(["https://e.com/a"], [])
    result = compare(before, after)
    assert [i["target_url"] for i in result["left"]] == ["https://e.com/a"]
    assert result["disappeared"] == []


def test_an_uncrawled_page_lands_in_disappeared_not_left():
    """Not in this crawl at all — the fix is unproven, not achieved."""
    before = _audit(["https://e.com/a"], [("BROKEN", "https://e.com/a")])
    after = _audit(["https://e.com/b"], [])  # a was never re-crawled
    result = compare(before, after)
    assert result["left"] == []
    assert [i["target_url"] for i in result["disappeared"]] == ["https://e.com/a"]


def test_a_genuinely_new_page_with_a_finding_is_appeared_not_entered():
    before = _audit(["https://e.com/a"], [])
    after = _audit(["https://e.com/a", "https://e.com/new"], [("BROKEN", "https://e.com/new")])
    result = compare(before, after)
    assert [i["target_url"] for i in result["appeared"]] == ["https://e.com/new"]
    assert result["entered"] == []


def test_a_new_finding_on_a_previously_crawled_page_is_entered():
    before = _audit(["https://e.com/a"], [])
    after = _audit(["https://e.com/a"], [("BROKEN", "https://e.com/a")])
    result = compare(before, after)
    assert [i["target_url"] for i in result["entered"]] == ["https://e.com/a"]
    assert result["appeared"] == []


def test_an_unchanged_finding_appears_in_no_bucket():
    before = _audit(["https://e.com/a"], [("BROKEN", "https://e.com/a")])
    after = _audit(["https://e.com/a"], [("BROKEN", "https://e.com/a")])
    result = compare(before, after)
    assert result["summary"] == {
        "entered": 0,
        "left": 0,
        "appeared": 0,
        "disappeared": 0,
        "by_check": {},
    }


def test_the_four_sets_are_disjoint_and_exhaustive():
    before = _audit(
        ["https://e.com/a", "https://e.com/b", "https://e.com/c"],
        [("X", "https://e.com/a"), ("X", "https://e.com/b")],
    )
    after = _audit(
        ["https://e.com/a", "https://e.com/c", "https://e.com/d"],
        [("X", "https://e.com/c"), ("X", "https://e.com/d")],
    )
    result = compare(before, after)
    all_urls = [
        i["target_url"]
        for bucket in ("entered", "left", "appeared", "disappeared")
        for i in result[bucket]
    ]
    assert sorted(all_urls) == [
        "https://e.com/a",
        "https://e.com/b",
        "https://e.com/c",
        "https://e.com/d",
    ]
    assert len(all_urls) == len(set(all_urls))  # disjoint


def test_by_check_summary_matches_the_bucket_contents():
    before = _audit(["https://e.com/a"], [("BROKEN", "https://e.com/a")])
    after = _audit(["https://e.com/a"], [])
    result = compare(before, after)
    assert result["summary"]["by_check"]["BROKEN"]["left"] == 1


def test_output_is_deterministic_regardless_of_input_dict_order():
    before = _audit(["https://e.com/b", "https://e.com/a"], [("X", "https://e.com/b")])
    after = _audit(["https://e.com/a", "https://e.com/b"], [("X", "https://e.com/a")])
    r1 = compare(before, after)
    r2 = compare(before, after)
    assert r1["entered"] == r2["entered"]
    assert [i["target_url"] for i in r1["entered"]] == ["https://e.com/a"]


# ── preflight warnings ────────────────────────────────────────────────────


def test_a_partial_before_crawl_warns_about_appeared_findings():
    """Issue #212: a truncated baseline poisons "appeared", not "disappeared" —
    a URL it never reached looks brand new to it, not gone from the current
    crawl."""
    before = _audit(["https://e.com/a"], [("X", "https://e.com/a")], crawl_partial=True)
    after = _audit([], [])
    warnings = preflight(before, after)
    assert any("appeared" in w and "before" in w for w in warnings)


def test_a_partial_after_crawl_warns_about_disappeared_findings():
    before = _audit(["https://e.com/a"], [])
    after = _audit([], [], crawl_partial=True)
    warnings = preflight(before, after)
    assert any("disappeared" in w and "after" in w for w in warnings)


def test_an_invalid_crawl_warns_plainly():
    before = _audit([], [], crawl_valid=False)
    after = _audit(["https://e.com/a"], [])
    warnings = preflight(before, after)
    assert any("before" in w and "invalid" in w for w in warnings)


def test_differing_results_affecting_config_is_flagged_by_name():
    before = _audit(["https://e.com/a"], [], crawl_config={"robots.policy": "respect"})
    after = _audit(["https://e.com/a"], [], crawl_config={"robots.policy": "ignore"})
    warnings = preflight(before, after)
    assert any("robots.policy" in w for w in warnings)


def test_identical_config_produces_no_config_warning():
    cfg = {"robots.policy": "respect", "limits.max_urls": 200}
    before = _audit(["https://e.com/a"], [], crawl_config=cfg)
    after = _audit(["https://e.com/a"], [], crawl_config=dict(cfg))
    assert preflight(before, after) == []


def test_no_config_present_on_either_side_warns_unknown_comparability():
    """#287: a missing manifest is an unknown comparison basis, not an established match --
    the same distinction crawl_partial already draws. Silently treating "neither side
    recorded a config" as "therefore they match" is exactly the bug: it let a native crawl
    and an SF audit, or two runs under different settings, compare as if nothing differed."""
    before = _audit(["https://e.com/a"], [])
    after = _audit(["https://e.com/a"], [])
    warnings = preflight(before, after)
    assert any("no crawl configuration" in w for w in warnings)


def test_one_side_missing_crawl_config_warns_by_name():
    """#287's reported case: a native crawl with a recorded manifest compared against a
    run that has none (an SF audit, or a crawl_config write that never happened) must not
    look like a match just because only one side has anything to compare."""
    before = _audit(
        ["https://e.com/a"],
        [("TITLE_TOO_SHORT", "https://e.com/a")],
        crawl_config={"rendering.mode": "raw"},
    )
    after = _audit(["https://e.com/a"], [])
    warnings = preflight(before, after)
    assert any("after" in w and "no crawl configuration" in w for w in warnings)


def test_differing_sf_profiles_warn_even_without_a_full_manifest():
    """#287: SF audits never record crawl_config, but they do record their export
    profile -- a known, if partial, signal that check coverage differed between runs."""
    before = _audit(["https://e.com/a"], [("TITLE_TOO_SHORT", "https://e.com/a")], profile="full")
    after = _audit(["https://e.com/a"], [], profile="lite")
    warnings = preflight(before, after)
    assert any("profile" in w and "full" in w and "lite" in w for w in warnings)


def test_identical_profile_does_not_add_a_profile_warning():
    before = _audit(["https://e.com/a"], [], profile="full")
    after = _audit(["https://e.com/a"], [], profile="full")
    warnings = preflight(before, after)
    assert not any("profile" in w for w in warnings)


def test_warnings_are_included_in_the_compare_result():
    before = _audit([], [], crawl_valid=False)
    after = _audit(["https://e.com/a"], [])
    result = compare(before, after)
    assert result["warnings"]


# ── refusal ─────────────────────────────────────────────────────────────


def test_a_document_missing_pages_is_refused_by_name():
    before = {"run": {}, "issues": []}
    after = _audit(["https://e.com/a"], [])
    with pytest.raises(CompareError, match="before"):
        compare(before, after)


def test_a_document_missing_issues_is_refused_by_name():
    before = _audit(["https://e.com/a"], [])
    after = {"run": {}, "pages": []}
    with pytest.raises(CompareError, match="after"):
        compare(before, after)


# ── partial baseline (issue #212) ────────────────────────────────────────
#
# A truncated baseline cannot prove a URL it never reached is genuinely new
# -- only that it wasn't seen. The whole value of compare mode is telling a
# fix apart from a deletion; letting a partial baseline manufacture false
# "appeared" findings breaks exactly that.


def test_a_partial_baseline_does_not_report_a_preexisting_url_as_appeared():
    before = _audit(["https://e.com/home"], [], crawl_partial=True)
    after = _audit(
        ["https://e.com/home", "https://e.com/preexisting-404"],
        [("BROKEN_PAGE_4XX", "https://e.com/preexisting-404")],
    )
    result = compare(before, after)
    assert result["appeared"] == []
    assert [i["target_url"] for i in result["entered"]] == ["https://e.com/preexisting-404"]


def test_a_partial_baseline_warns_about_appeared_not_disappeared():
    before = _audit(["https://e.com/home"], [], crawl_partial=True)
    after = _audit(["https://e.com/home"], [])
    warnings = preflight(before, after)
    assert any("appeared" in w and "before" in w for w in warnings)
    assert not any("disappeared" in w and "before" in w for w in warnings)


def test_a_full_baseline_still_reports_a_genuinely_new_url_as_appeared():
    """crawl_partial absent (a complete baseline) keeps the useful signal."""
    before = _audit(["https://e.com/home"], [])
    after = _audit(["https://e.com/home", "https://e.com/new"], [("BROKEN", "https://e.com/new")])
    result = compare(before, after)
    assert [i["target_url"] for i in result["appeared"]] == ["https://e.com/new"]


# ── partial after crawl (issue #458) ─────────────────────────────────────
#
# Symmetric to the partial-baseline cases above: a partial after crawl cannot
# prove a before-only URL is genuinely gone, only that it was not reached.


def test_a_partial_after_crawl_does_not_report_an_unreached_url_as_disappeared():
    before = _audit(
        ["https://x.com/a", "https://x.com/b"],
        [("MISSING_TITLE", "https://x.com/a")],
    )
    after = _audit(["https://x.com/b"], [], crawl_partial=True)  # /a never reached this run
    result = compare(before, after)
    assert result["disappeared"] == []
    assert [i["target_url"] for i in result["left"]] == ["https://x.com/a"]


def test_a_full_after_crawl_still_reports_a_genuinely_gone_url_as_disappeared():
    """crawl_partial absent (a complete after crawl) keeps the useful signal."""
    before = _audit(
        ["https://x.com/a", "https://x.com/b"],
        [("MISSING_TITLE", "https://x.com/a")],
    )
    after = _audit(["https://x.com/b"], [])  # full crawl, /a genuinely gone
    result = compare(before, after)
    assert [i["target_url"] for i in result["disappeared"]] == ["https://x.com/a"]
    assert result["left"] == []


def test_a_partial_after_crawl_still_reports_a_reached_url_as_left():
    """The URL IS present in the after crawl — not the unproven case."""
    before = _audit(
        ["https://x.com/a", "https://x.com/b"],
        [("MISSING_TITLE", "https://x.com/a")],
    )
    after = _audit(["https://x.com/a", "https://x.com/b"], [], crawl_partial=True)
    result = compare(before, after)
    assert [i["target_url"] for i in result["left"]] == ["https://x.com/a"]
    assert result["disappeared"] == []


# ── audit-wide findings (issue #213) ─────────────────────────────────────
#
# A finding with no target_url (e.g. TITLE_TEMPLATED) describes the crawl as
# a whole, not a page — it must still participate in the delta instead of
# being silently dropped, but it cannot appear/disappear since there is no
# page whose presence changed.


def _global_audit(urls, checks):
    return {
        "run": {},
        "pages": [{"url": u} for u in urls],
        "issues": [{"check": c, "target_url": None} for c in checks],
    }


def test_a_new_audit_wide_finding_is_entered_not_dropped():
    before = _global_audit(["https://e.com/a"], [])
    after = _global_audit(["https://e.com/a"], ["TITLE_TEMPLATED"])
    result = compare(before, after)
    assert result["summary"]["entered"] == 1
    assert [i["check"] for i in result["entered"]] == ["TITLE_TEMPLATED"]
    assert result["summary"]["by_check"]["TITLE_TEMPLATED"]["entered"] == 1


def test_a_resolved_audit_wide_finding_is_left_not_dropped():
    before = _global_audit(["https://e.com/a"], ["TITLE_TEMPLATED"])
    after = _global_audit(["https://e.com/a"], [])
    result = compare(before, after)
    assert result["summary"]["left"] == 1
    assert [i["check"] for i in result["left"]] == ["TITLE_TEMPLATED"]


def test_an_audit_wide_finding_never_lands_in_appeared_or_disappeared():
    before = _global_audit(["https://e.com/a"], [])
    after = _global_audit(["https://e.com/a", "https://e.com/b"], ["TITLE_TEMPLATED"])
    result = compare(before, after)
    assert result["appeared"] == []
    assert result["disappeared"] == []
