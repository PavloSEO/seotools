"""Auditable finding exclusions are post-analysis and keep measured evidence."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import jsonschema
import pytest

from seohead.crawl import settings as crawl_settings
from seohead.servers import handlers
from seohead.sf.config import ConfigError, load_config, validate_config
from seohead.sf.core.aggregate import aggregate
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import load_exports
from seohead.sf.reporters.md import write_markdown
from seohead.sf.tasks import build_tasks, render_tasks_md
from seohead.tools.finding_exclusions import matching_rule, validate_rules


def _context(tmp_path, rows):
    path = tmp_path / "internal_all.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Address", "Content Type", "Status Code", "Status", "Indexability"])
        writer.writerows(rows)
    return AuditContext(load_exports(str(tmp_path)), load_config(None))


def _policy():
    return [
        {
            "id": "legacy-title",
            "pattern": r"/legacy(?:/|$)",
            "checks": ["TITLE_MISSING"],
            "reason": "Legacy templates are outside this audit scope.",
        },
        {
            "id": "all-legacy",
            "pattern": r"/legacy(?:/|$)",
            "reason": "Legacy URLs are being retired.",
        },
    ]


def test_ordered_check_scoped_suppression_preserves_original_findings_and_coverage(tmp_path):
    legacy = "https://example.test/legacy/"
    current = "https://example.test/current/"
    ctx = _context(
        tmp_path,
        [
            [legacy, "text/html", "200", "OK", "Indexable"],
            [current, "text/html", "200", "OK", "Indexable"],
        ],
    )
    ctx.config["finding_exclusions"] = _policy()
    ctx.add("TITLE_MISSING", target_url=legacy, occurrences_count=3)
    ctx.add("TITLE_TOO_LONG", target_url=current)
    ctx.add("TITLE_MISSING", target_url=current)
    ctx.add("TITLE_TOO_LONG", target_url=None)

    result = aggregate(ctx, {"input_mode": "crawl", "generated_at": "2026-10-03T00:00:00Z"}, {}, {})
    audit = result.to_json()

    assert {(issue.check, issue.target_url) for issue in result.issues} == {
        ("TITLE_TOO_LONG", current),
        ("TITLE_MISSING", current),
        ("TITLE_TOO_LONG", None),
    }
    assert [(issue["check"], issue["target_url"]) for issue in result.suppressed_issues] == [
        ("TITLE_MISSING", legacy)
    ]
    suppressed = result.suppressed_issues[0]
    assert suppressed["occurrences_count"] == 3
    assert suppressed["id"] == "ISSUE-000002"
    assert suppressed["suppression"] == {
        "rule_id": "legacy-title",
        "pattern": r"/legacy(?:/|$)",
        "reason": "Legacy templates are outside this audit scope.",
        "matched_url": legacy,
    }
    assert result.pages[0].issue_ids == []
    assert result.pages[0].suppressed_issue_ids == [suppressed["id"]]
    assert len(result.pages[0].suppressed_issue_ids) == 1
    assert len(result.suppressed_issues) == 1
    assert result.summary["check_coverage"]["checks_fired"] == 2
    assert "TITLE_MISSING" not in result.summary["check_coverage"]["checks_silent_ids"]
    assert result.summary["finding_exclusions"]["suppressed_total"] == 1
    assert result.summary["finding_exclusions"]["by_rule"] == [
        {
            "id": "legacy-title",
            "reason": "Legacy templates are outside this audit scope.",
            "suppressed_findings": 1,
            "suppressed_occurrences": 3,
        },
        {
            "id": "all-legacy",
            "reason": "Legacy URLs are being retired.",
            "suppressed_findings": 0,
            "suppressed_occurrences": 0,
        },
    ]
    assert result.summary["totals"]["issues_total"] == 3
    assert result.summary["totals"]["findings_total"] == 4

    backlog = build_tasks(audit)
    task_urls = {url for task in backlog["tasks"] for url in task["urls"]}
    assert current in task_urls
    assert backlog["source"]["finding_exclusions"] == result.summary["finding_exclusions"]
    task_md = render_tasks_md(backlog)
    assert "1 finding suppressed by 2 rules" in task_md

    md_path = tmp_path / "audit.md"
    write_markdown(result, str(md_path))
    markdown = md_path.read_text(encoding="utf-8")
    assert "Suppressed findings (1)" in markdown
    assert "Finding exclusions (2 rules)" in markdown
    assert "Legacy templates are outside this audit scope." in markdown
    assert (
        suppressed["id"] not in markdown
    )  # original detail remains in JSON, not duplicated in prose

    schema_path = Path(__file__).parents[1] / "seohead/sf/schema/audit.schema.json"
    jsonschema.validate(audit, json.loads(schema_path.read_text(encoding="utf-8")))


def test_suppressing_no_response_does_not_make_crawl_valid_or_score_it(tmp_path):
    url = "https://example.test/unavailable"
    ctx = _context(tmp_path, [[url, "", "0", "No Response", "Non-Indexable"]])
    ctx.config["finding_exclusions"] = [
        {"id": "offline", "pattern": ".*", "reason": "Known temporary outage."}
    ]
    ctx.add("NO_RESPONSE", target_url=url, status_code=0)

    result = aggregate(ctx, {"input_mode": "crawl"}, {}, {})

    assert result.issues == []
    assert len(result.suppressed_issues) == 1
    assert result.run["crawl_valid"] is False
    assert result.summary["health_score"] is None
    assert result.summary["health_score_reason"]


def test_rules_are_first_match_and_url_less_findings_never_match():
    rules = validate_rules(_policy())
    compiled = [(rule, re.compile(rule["pattern"])) for rule in rules]

    assert (
        matching_rule("TITLE_MISSING", "https://example.test/legacy/", compiled)["id"]
        == "legacy-title"
    )
    assert (
        matching_rule("TITLE_TOO_LONG", "https://example.test/legacy/", compiled)["id"]
        == "all-legacy"
    )
    assert matching_rule("TITLE_MISSING", None, compiled) is None
    assert matching_rule("TITLE_MISSING", "https://example.test/current/", compiled) is None


@pytest.mark.parametrize(
    "policy",
    [
        [{"id": "bad", "pattern": "[", "reason": "invalid regex"}],
        [
            {"id": "same", "pattern": ".*", "reason": "one"},
            {"id": "same", "pattern": ".*", "reason": "two"},
        ],
        [{"id": "unknown-check", "pattern": ".*", "checks": ["NOT_A_CHECK"], "reason": "unknown"}],
    ],
)
def test_sf_config_rejects_invalid_exclusion_rules(policy):
    config = load_config(None)
    config["finding_exclusions"] = policy
    with pytest.raises(ConfigError, match="finding_exclusions"):
        validate_config(config)


def test_exclusion_rules_have_documented_size_limits():
    valid = {
        "id": "r" * 64,
        "pattern": "." * 500,
        "reason": "x" * 500,
    }
    assert len(validate_rules([valid])) == 1
    with pytest.raises(ValueError, match="at most 100 rules"):
        validate_rules([valid] * 101)
    with pytest.raises(ValueError, match="checks may contain at most 200"):
        validate_rules([{**valid, "checks": ["TITLE_MISSING"] * 201}])
    for field, limit in (("id", 64), ("pattern", 500), ("reason", 500)):
        invalid = {**valid, field: valid[field] + "x"}
        with pytest.raises(ValueError, match=f"{field}.*at most {limit}"):
            validate_rules([invalid])


def test_crawl_analysis_policy_is_additive_and_not_a_scope_filter():
    policy = [{"id": "legacy", "pattern": "/legacy", "reason": "Retired."}]
    settings = crawl_settings.load(overrides={"analysis.finding_exclusions": policy})

    assert settings["analysis"]["finding_exclusions"] == validate_rules(policy)
    assert settings["scope"] == crawl_settings.DEFAULTS["scope"]
    assert "analysis.finding_exclusions" in crawl_settings.RESULTS_AFFECTING
    assert "scope.include_patterns" not in settings["analysis"]


def test_native_crawl_rejects_unknown_check_before_collection(tmp_path, monkeypatch):
    collection_started = False

    def fake_crawl(*_args, **_kwargs):
        nonlocal collection_started
        collection_started = True
        raise AssertionError("invalid policy must fail before collection")

    monkeypatch.setattr("seohead.crawl.spider.crawl_site", fake_crawl)
    with pytest.raises(ConfigError, match="NOT_A_CHECK"):
        handlers.crawl_site(
            url="https://example.test/",
            out_dir=str(tmp_path),
            overrides={
                "analysis.finding_exclusions": [
                    {
                        "id": "bad-selector",
                        "pattern": ".*",
                        "checks": ["NOT_A_CHECK"],
                        "reason": "Invalid check selector.",
                    }
                ]
            },
        )
    assert collection_started is False
