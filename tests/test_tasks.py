"""Task backlog generation from an audit."""

from __future__ import annotations

from seohead.sf.config import load_config
from seohead.sf.reporters.jsonfile import to_dict
from seohead.sf.tasks import build_tasks, render_tasks_md


def test_groups_by_check_with_priority(result):
    backlog = build_tasks(to_dict(result), load_config(None))
    assert backlog["summary"]["tasks_total"] >= 1
    checks = {t["check"] for t in backlog["tasks"]}
    # Create one task per issue type by default.
    assert "BROKEN_INTERNAL_LINK" in checks
    bl = next(t for t in backlog["tasks"] if t["check"] == "BROKEN_INTERNAL_LINK")
    assert bl["priority"] == "P1"
    assert bl["effort"] == "high"
    assert bl["broken_links"]  # Link-location evidence is carried into the task.
    assert bl["broken_links"][0]["link_path"]


def test_severity_filter(result):
    cfg = load_config(None)
    cfg["tasks_pipeline"]["include_severities"] = ["critical"]
    backlog = build_tasks(to_dict(result), cfg)
    assert {t["severity"] for t in backlog["tasks"]} == {"critical"}


def test_per_issue_grouping(result):
    cfg = load_config(None)
    cfg["tasks_pipeline"]["group_by"] = "issue"
    backlog = build_tasks(to_dict(result), cfg)
    # Per-issue grouping yields at least as many tasks as grouping by check.
    grouped = build_tasks(to_dict(result), load_config(None))
    assert backlog["summary"]["tasks_total"] >= grouped["summary"]["tasks_total"]


def test_markdown_renders(result):
    md = render_tasks_md(build_tasks(to_dict(result), load_config(None)))
    assert "# Audit Tasks" in md
    assert "P1" in md
    assert "/html/body/footer/nav/a[2]" in md  # tasks.md preserves XPath location evidence.


def _audit_with_occurrences(occurrences_count: int) -> dict:
    return {
        "run": {"project": "example.test", "generated_at": "2026-09-05T00:00:00Z"},
        "summary": {"health_score": 80},
        "issues": [
            {
                "id": "ISSUE-000001",
                "check": "BROKEN_INTERNAL_LINK",
                "severity": "critical",
                "source": "inlinks:Client Error (4xx) Inlinks",
                "message": "Internal link points to a 4xx URL",
                "target_url": "https://example.test/dead",
                "status_code": 404,
                "occurrences_count": occurrences_count,
                "fix_hint": "Replace the shared footer link.",
                "locations": [
                    {
                        "source_url": "https://example.test/source-a",
                        "anchor": "Old page",
                        "link_position": "Footer",
                        "link_path": "/html/body/footer/a[1]",
                    }
                ],
            }
        ],
    }


def test_min_occurrences_drops_low_frequency_issue_grouping():
    """#224: min_occurrences was never enforced when group_by == "issue"."""
    cfg = load_config(None)
    cfg["tasks_pipeline"]["group_by"] = "issue"
    cfg["tasks_pipeline"]["min_occurrences"] = 2
    backlog = build_tasks(_audit_with_occurrences(1), cfg)
    assert backlog["summary"]["tasks_total"] == 0


def test_min_occurrences_keeps_high_frequency_check_grouping():
    """#224: check grouping compared the issue-record count, not occurrences_count.

    One record with occurrences_count=100 must clear a min_occurrences=2
    threshold even though it is the only record in its group.
    """
    cfg = load_config(None)
    cfg["tasks_pipeline"]["group_by"] = "check"
    cfg["tasks_pipeline"]["min_occurrences"] = 2
    backlog = build_tasks(_audit_with_occurrences(100), cfg)
    assert backlog["summary"]["tasks_total"] == 1


def test_min_occurrences_same_meaning_both_grouping_modes():
    """#224: min_occurrences must exclude/retain identically regardless of group_by."""
    for group_by in ("check", "issue"):
        cfg = load_config(None)
        cfg["tasks_pipeline"]["group_by"] = group_by
        cfg["tasks_pipeline"]["min_occurrences"] = 5
        dropped = build_tasks(_audit_with_occurrences(4), cfg)
        assert dropped["summary"]["tasks_total"] == 0, group_by
        kept = build_tasks(_audit_with_occurrences(5), cfg)
        assert kept["summary"]["tasks_total"] == 1, group_by


def _partial_audit() -> dict:
    return {
        "run": {
            "project": "example.test",
            "generated_at": "2026-09-05T00:00:00Z",
            "crawl_valid": True,
            "crawl_partial": True,
            "crawl_finish_reason": "url_limit",
        },
        "summary": {
            "health_score": 90,
            "health_score_scope": (
                "1 of 1,000 sitemap URLs crawled — the score describes the "
                "crawled subset, not the whole site"
            ),
        },
        "issues": [
            {
                "id": "ISSUE-000001",
                "check": "BROKEN_INTERNAL_LINK",
                "severity": "critical",
                "target_url": "https://example.test/dead",
                "occurrences_count": 1,
                "locations": [],
            }
        ],
    }


def test_partial_crawl_scope_and_reason_carried_into_source():
    """#308: scope, stop reason and basis survive into tasks.json, not just audit.json."""
    backlog = build_tasks(_partial_audit())
    src = backlog["source"]
    assert src["crawl_partial"] is True
    assert src["crawl_finish_reason"] == "url_limit"
    assert "1 of 1,000" in src["health_score_scope"]


def test_partial_crawl_warning_rendered_before_task_list():
    """#308: a distinct partial-run warning must appear before the P1 section."""
    md = render_tasks_md(build_tasks(_partial_audit()))
    assert "Partial crawl" in md
    assert "url_limit" in md
    assert "1 of 1,000" in md
    assert md.index("Partial crawl") < md.index("## P1")


def test_invalid_crawl_warning_stays_separate_from_partial_warning():
    """#308: the failed-crawl warning must still lead, and stay distinct."""
    audit = _partial_audit()
    audit["run"]["crawl_valid"] = False
    audit["run"]["crawl_invalid_reason"] = "fetch failed for every URL"
    md = render_tasks_md(build_tasks(audit))
    assert "Crawl failed" in md
    assert "Partial crawl" in md
    assert md.index("Crawl failed") < md.index("Partial crawl")


def test_complete_crawl_has_no_partial_warning():
    """#308: a normal, complete run must not carry a stray partial-crawl warning."""
    backlog = build_tasks(_partial_audit_source())
    assert backlog["source"]["crawl_partial"] is False
    md = render_tasks_md(backlog)
    assert "Partial crawl" not in md


def _partial_audit_source() -> dict:
    # Reuse the ``result`` fixture's shape indirectly via a plain dict: a
    # normal audit with no partial-crawl signal at all.
    return {
        "run": {"project": "example.test", "generated_at": "2026-09-05T00:00:00Z"},
        "summary": {"health_score": 95},
        "issues": [],
    }


def _broken_link_audit(locations: list[dict]) -> dict:
    return {
        "run": {"project": "example.test", "generated_at": "2026-09-05T00:00:00Z"},
        "summary": {"health_score": 80},
        "issues": [
            {
                "id": "ISSUE-000001",
                "check": "BROKEN_INTERNAL_LINK",
                "severity": "critical",
                "target_url": "https://example.test/dead",
                "status_code": 404,
                "occurrences_count": len(locations),
                "locations": locations,
            }
        ],
    }


_THREE_LOCATIONS = [
    {
        "source_url": "https://example.test/a",
        "anchor": "A",
        "link_position": "Content",
        "link_path": "/a",
    },
    {
        "source_url": "https://example.test/b",
        "anchor": "B",
        "link_position": "Footer",
        "link_path": "/b",
    },
    {
        "source_url": "https://example.test/c",
        "anchor": "C",
        "link_position": "Footer",
        "link_path": "/c",
    },
]


def test_target_cap_alone_does_not_truncate_broken_link_locations():
    """#309: max_urls_per_task caps target URLs, not source locations."""
    backlog = build_tasks(
        _broken_link_audit(_THREE_LOCATIONS),
        {"tasks_pipeline": {"max_urls_per_task": 1}},
    )
    task = backlog["tasks"][0]
    assert task["urls"] == ["https://example.test/dead"]
    assert task["urls_truncated"] == 0
    assert [row["source_url"] for row in task["broken_links"]] == [
        "https://example.test/a",
        "https://example.test/b",
        "https://example.test/c",
    ]
    assert task["broken_links_total"] == 3
    assert task["broken_links_truncated"] == 0
    md = render_tasks_md(backlog)
    assert "https://example.test/b" in md
    assert "https://example.test/c" in md
    assert "omitted" not in md


def test_location_cap_reports_totals_and_omissions():
    """#309: a dedicated location cap must report what it hides, in JSON and Markdown."""
    backlog = build_tasks(
        _broken_link_audit(_THREE_LOCATIONS),
        {"tasks_pipeline": {"max_locations_per_task": 1}},
    )
    task = backlog["tasks"][0]
    assert [row["source_url"] for row in task["broken_links"]] == ["https://example.test/a"]
    assert task["broken_links_total"] == 3
    assert task["broken_links_truncated"] == 2
    md = render_tasks_md(backlog)
    assert "https://example.test/b" not in md
    assert "https://example.test/c" not in md
    assert "2 more source location(s) omitted" in md


def _reduced_coverage_audit(*, partial: bool) -> dict:
    return {
        "run": {
            "project": "example.test",
            "generated_at": "2026-09-06T00:00:00Z",
            "crawl_valid": True,
            "crawl_partial": partial,
            "crawl_finish_reason": "url_limit" if partial else None,
        },
        "summary": {
            "health_score": 82,
            "health_score_basis": (
                "40 of 96 checks could run; the score is not comparable to a run with full evidence"
            ),
            "check_coverage": {
                "checks_total": 96,
                "checks_fired": 40,
                "checks_skipped": 50,
                "checks_disabled": 6,
                "coverage": 0.417,
            },
        },
        "issues": [
            {
                "check": "MISSING_TITLE",
                "severity": "warning",
                "target_url": "https://example.test/a",
                "occurrences_count": 1,
                "source": "SF",
            }
        ],
    }


def test_reduced_coverage_basis_rendered_for_non_partial_crawl():
    """#457: a fully-crawled but low-coverage run must still show its basis."""
    md = render_tasks_md(build_tasks(_reduced_coverage_audit(partial=False)))
    assert "40 of 96" in md


def test_reduced_coverage_basis_rendered_alongside_partial_warning():
    """#457: a partial crawl keeps both the partial warning and the basis."""
    md = render_tasks_md(build_tasks(_reduced_coverage_audit(partial=True)))
    assert "Partial crawl" in md
    assert "40 of 96" in md


def test_full_coverage_run_has_no_basis_text():
    """#457 negative control: a fully-covered run injects no new basis text."""
    audit = _reduced_coverage_audit(partial=False)
    audit["summary"]["health_score_basis"] = None
    audit["summary"]["check_coverage"] = {
        "checks_total": 96,
        "checks_fired": 96,
        "checks_skipped": 0,
        "checks_disabled": 0,
        "coverage": 1.0,
    }
    md = render_tasks_md(build_tasks(audit))
    assert "not comparable" not in md
    assert "checks could run" not in md


def _same_url_repeat_audit(count: int) -> dict:
    return {
        "run": {"project": "example.test", "generated_at": "2026-09-06T00:00:00Z"},
        "summary": {"health_score": 90},
        "issues": [
            {
                "check": "MISSING_TITLE",
                "severity": "warning",
                "target_url": "https://example.test/a",
                "occurrences_count": 1,
                "source": "SF",
            }
            for _ in range(count)
        ],
    }


def test_grouped_title_counts_unique_pages_not_records():
    """#459: title, affected_count and urls must agree on the same page count."""
    backlog = build_tasks(_same_url_repeat_audit(3))
    task = backlog["tasks"][0]
    assert task["affected_count"] == 1
    assert task["urls"] == ["https://example.test/a"]
    assert "1 page" in task["title"]
    assert "3 pages" not in task["title"]


def test_grouped_title_still_counts_distinct_urls_correctly():
    """#459 negative control: distinct-URL grouping is unchanged."""
    audit = {
        "run": {"project": "example.test", "generated_at": "2026-09-06T00:00:00Z"},
        "summary": {"health_score": 90},
        "issues": [
            {
                "check": "MISSING_TITLE",
                "severity": "warning",
                "target_url": f"https://example.test/{i}",
                "occurrences_count": 1,
                "source": "SF",
            }
            for i in range(3)
        ],
    }
    backlog = build_tasks(audit)
    task = backlog["tasks"][0]
    assert task["affected_count"] == 3
    assert "3 pages" in task["title"]
