"""Configured pagination/filter canonical policies use only crawled evidence."""

from __future__ import annotations

import copy
import csv
import json

import pytest

from seohead.canonical_policy import validate_canonical_policy
from seohead.sf.config import ConfigError, load_config, validate_config
from seohead.sf.core.audit import run_audit
from seohead.sf.reporters.jsonfile import to_dict
from seohead.sf.tasks import build_tasks

BASE = "https://shop.example.test/catalog/"
PAGINATION = f"{BASE}page/2/"
FILTERED = f"{BASE}?color=blue"


def _audit(tmp_path, rows, policy=None, include_canonical=True):
    exports = tmp_path / "exports"
    exports.mkdir(parents=True)
    columns = [
        "Address",
        "Content Type",
        "Status Code",
        "Indexability",
        "Body Unavailable",
    ]
    if include_canonical:
        columns.append("Canonical Link Element 1")
    with (exports / "internal_all.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        for page_row in rows:
            url, canonical, *status_values = page_row
            status = status_values[0] if status_values else "200"
            content_type = status_values[1] if len(status_values) > 1 else "text/html"
            body_unavailable = status_values[2] if len(status_values) > 2 else ""
            row = [url, content_type, status, "Indexable", body_unavailable]
            if include_canonical:
                row.append(canonical)
            writer.writerow(row)
    overrides = {"canonical_policy": policy} if policy is not None else None
    return run_audit(
        input_mode="parse-exports",
        exports_dir=str(exports),
        config_overrides=overrides,
        log=lambda _message: None,
    )


def _policy(pagination=(), filters=()):
    return {"pagination": list(pagination), "filters": list(filters)}


def test_absent_policy_is_skipped_not_a_sitewide_canonical_finding(tmp_path):
    result = _audit(
        tmp_path,
        [(BASE, BASE), (PAGINATION, BASE), (FILTERED, BASE)],
    )
    check_ids = {issue.check for issue in result.issues}
    skipped = {item.id: item.reason for item in result.skipped}
    assert "PAGINATION_CANONICAL_POLICY" not in check_ids
    assert "FILTER_CANONICAL_POLICY" not in check_ids
    assert "no pagination canonical policy configured" in skipped["PAGINATION_CANONICAL_POLICY"]
    assert "no filters canonical policy configured" in skipped["FILTER_CANONICAL_POLICY"]


def test_pagination_and_filter_mismatches_are_separate_and_flow_into_tasks(tmp_path):
    filter_target = BASE
    result = _audit(
        tmp_path,
        [
            (BASE, BASE),
            (PAGINATION, BASE),
            (FILTERED, FILTERED),
        ],
        _policy(
            pagination=[{"pattern": r"/catalog/page/\d+/?$", "policy": "self"}],
            filters=[
                {
                    "pattern": r"[?&]color=",
                    "policy": "landing",
                    "target": filter_target,
                }
            ],
        ),
    )
    issues = {issue.check: issue for issue in result.issues}
    pagination = issues["PAGINATION_CANONICAL_POLICY"]
    filtered = issues["FILTER_CANONICAL_POLICY"]
    assert pagination.target_url == PAGINATION
    assert pagination.details["canonical_policy"] == {
        "source_url": PAGINATION,
        "declared_canonical_url": BASE,
        "expected_canonical_url": PAGINATION,
        "policy": "self",
        "matched_pattern": r"/catalog/page/\d+/?$",
    }
    assert filtered.target_url == FILTERED
    assert filtered.details["canonical_policy"]["declared_canonical_url"] == FILTERED
    assert filtered.details["canonical_policy"]["expected_canonical_url"] == filter_target
    assert filtered.details["canonical_policy"]["policy"] == "landing"

    backlog = build_tasks(to_dict(result), load_config(None))
    tasks = {task["check"]: task for task in backlog["tasks"]}
    assert set(tasks) >= {
        "PAGINATION_CANONICAL_POLICY",
        "FILTER_CANONICAL_POLICY",
    }
    pagination_reproduction = tasks["PAGINATION_CANONICAL_POLICY"]["reproductions"][0]
    assert PAGINATION in pagination_reproduction and BASE in pagination_reproduction


def test_configured_self_first_page_and_landing_targets_can_be_satisfied(tmp_path):
    first_page = "https://shop.example.test/archive/"
    sorted_landing = "https://shop.example.test/category/"
    result = _audit(
        tmp_path,
        [
            (BASE, BASE),
            (PAGINATION, PAGINATION),
            ("https://shop.example.test/archive/", first_page),
            ("https://shop.example.test/archive/page/3/", first_page),
            (
                "https://shop.example.test/category/?color=green",
                "https://shop.example.test/category/?color=green",
            ),
            ("https://shop.example.test/category/?sort=price", sorted_landing),
            (first_page, first_page),
            (sorted_landing, sorted_landing),
        ],
        _policy(
            pagination=[
                {"pattern": r"/catalog/page/\d+/?$", "policy": "self"},
                {
                    "pattern": r"/archive/page/\d+/?$",
                    "policy": "first_page",
                    "target": first_page,
                },
            ],
            filters=[
                {"pattern": r"[?&]color=", "policy": "self"},
                {
                    "pattern": r"[?&]sort=",
                    "policy": "landing",
                    "target": sorted_landing,
                },
            ],
        ),
    )
    assert not {
        issue.check
        for issue in result.issues
        if issue.check in {"PAGINATION_CANONICAL_POLICY", "FILTER_CANONICAL_POLICY"}
    }
    assert not {
        item.id
        for item in result.skipped
        if item.id in {"PAGINATION_CANONICAL_POLICY", "FILTER_CANONICAL_POLICY"}
    }


@pytest.mark.parametrize(
    "policy",
    [
        {"pagination": [{"pattern": "[", "policy": "self"}]},
        {"filters": [{"pattern": "color", "policy": "landing"}]},
        {"filters": [{"pattern": "color", "policy": "self", "target": BASE}]},
        {"filters": [{"pattern": "color", "policy": ["self"]}]},
        {"filters": [{"pattern": "color", "policy": "landing", "target": "//invalid"}]},
        {
            "filters": [
                {
                    "pattern": "color",
                    "policy": "landing",
                    "target": "https://user:pass@shop.example.test/",
                }
            ]
        },
        {
            "filters": [
                {
                    "pattern": "color",
                    "policy": "landing",
                    "target": "https://shop.example.test:bad/",
                }
            ]
        },
    ],
    ids=[
        "invalid-regex",
        "missing-target",
        "self-has-target",
        "wrong-policy-type",
        "relative-target",
        "target-credentials",
        "invalid-port",
    ],
)
def test_invalid_canonical_policy_is_rejected(policy):
    with pytest.raises(ValueError, match=r"canonical_policy\."):
        validate_canonical_policy(policy)

    sf_config = load_config(None)
    sf_config["canonical_policy"] = policy
    with pytest.raises(ConfigError, match=r"canonical_policy\."):
        validate_config(sf_config)

    from seohead.crawl.settings import DEFAULTS as CRAWL_DEFAULTS
    from seohead.crawl.settings import ConfigError as CrawlConfigError
    from seohead.crawl.settings import validate as validate_crawl_settings

    crawl_config = copy.deepcopy(CRAWL_DEFAULTS)
    crawl_config["analysis"]["canonical_policy"] = policy
    with pytest.raises(CrawlConfigError, match=r"analysis\.canonical_policy\."):
        validate_crawl_settings(crawl_config)


def test_canonical_policy_requires_targets_to_exist_in_crawl_evidence(tmp_path):
    landing = "https://shop.example.test/category/"
    result = _audit(
        tmp_path,
        [(FILTERED, FILTERED)],
        _policy(filters=[{"pattern": r"[?&]color=", "policy": "landing", "target": landing}]),
    )
    assert not [issue for issue in result.issues if issue.check == "FILTER_CANONICAL_POLICY"]
    skipped = {item.id: item.reason for item in result.skipped}
    assert "FILTER_CANONICAL_POLICY" in skipped
    assert "absent from crawl evidence" in skipped["FILTER_CANONICAL_POLICY"]
    assert FILTERED in skipped["FILTER_CANONICAL_POLICY"]
    assert landing in skipped["FILTER_CANONICAL_POLICY"]


def test_placeholder_target_with_no_response_is_unavailable(tmp_path):
    landing = "https://shop.example.test/category/"
    result = _audit(
        tmp_path,
        [
            (FILTERED, FILTERED),
            (landing, landing, "", ""),
        ],
        _policy(filters=[{"pattern": r"[?&]color=", "policy": "landing", "target": landing}]),
    )
    assert not [issue for issue in result.issues if issue.check == "FILTER_CANONICAL_POLICY"]
    skipped = {item.id: item.reason for item in result.skipped}
    assert "FILTER_CANONICAL_POLICY" in skipped
    assert "absent from crawl evidence" in skipped["FILTER_CANONICAL_POLICY"]


def test_slash_alias_redirect_does_not_prove_expected_target_was_fetched(tmp_path):
    landing = "https://shop.example.test/category/"
    result = _audit(
        tmp_path,
        [
            (FILTERED, FILTERED),
            (landing.rstrip("/"), "", "301"),
        ],
        _policy(filters=[{"pattern": r"[?&]color=", "policy": "landing", "target": landing}]),
    )
    assert not [issue for issue in result.issues if issue.check == "FILTER_CANONICAL_POLICY"]
    skipped = {item.id: item.reason for item in result.skipped}
    assert "FILTER_CANONICAL_POLICY" in skipped
    assert "absent from crawl evidence" in skipped["FILTER_CANONICAL_POLICY"]


def test_oversized_source_body_makes_blank_canonical_unavailable(tmp_path):
    result = _audit(
        tmp_path,
        [
            (FILTERED, "", "200", "text/html", "oversized"),
            (BASE, BASE),
        ],
        _policy(filters=[{"pattern": r"[?&]color=", "policy": "landing", "target": BASE}]),
    )
    assert not [issue for issue in result.issues if issue.check == "FILTER_CANONICAL_POLICY"]
    skipped = {item.id: item.reason for item in result.skipped}
    assert "FILTER_CANONICAL_POLICY" in skipped
    assert "source HTML body unavailable" in skipped["FILTER_CANONICAL_POLICY"]


def test_target_status_remains_outside_policy_check_for_issue_824(tmp_path):
    result = _audit(
        tmp_path,
        [(BASE, BASE, "301"), (FILTERED, BASE)],
        _policy(filters=[{"pattern": r"[?&]color=", "policy": "landing", "target": BASE}]),
    )
    checks = {issue.check for issue in result.issues}
    assert "CANONICAL_TO_REDIRECT" in checks
    assert "FILTER_CANONICAL_POLICY" not in checks


def test_missing_canonical_column_and_unmatched_patterns_are_unmeasured(tmp_path):
    policy = _policy(
        pagination=[{"pattern": r"/never-matches/", "policy": "self"}],
        filters=[{"pattern": r"[?&]color=", "policy": "self"}],
    )
    no_column = _audit(
        tmp_path / "without-column", [(FILTERED, "")], policy, include_canonical=False
    )
    skipped = {item.id: item.reason for item in no_column.skipped}
    assert "Canonical column" in skipped["PAGINATION_CANONICAL_POLICY"]
    assert "Canonical column" in skipped["FILTER_CANONICAL_POLICY"]

    unmatched = _audit(tmp_path / "unmatched", [(BASE, BASE)], policy)
    skipped = {item.id: item.reason for item in unmatched.skipped}
    assert "no crawled URLs matched" in skipped["PAGINATION_CANONICAL_POLICY"]
    assert "no crawled URLs matched" in skipped["FILTER_CANONICAL_POLICY"]


def test_native_crawl_cli_uses_analysis_policy_and_writes_tasks(tmp_path, monkeypatch, capsys):
    # Both analysis.canonical_policy.pagination and
    # analysis.canonical_policy.filters are results-affecting project settings.
    import seohead.crawl.spider as spider_mod
    from seohead.cli import main as cli_main
    from seohead.crawl.collect import PageRecord
    from seohead.crawl.spider import SpiderResult
    from seohead.servers.handlers import HANDLERS

    config = tmp_path / "crawl.json"
    config.write_text(
        json.dumps(
            {
                "analysis": {
                    "canonical_policy": _policy(
                        pagination=[{"pattern": r"/catalog/page/\d+/?$", "policy": "self"}]
                    )
                }
            }
        ),
        encoding="utf-8",
    )
    result = SpiderResult(
        pages=[
            PageRecord(url=BASE, status_code=200, content_type="text/html", canonical=BASE),
            PageRecord(
                url=PAGINATION,
                status_code=200,
                content_type="text/html",
                canonical=BASE,
            ),
        ]
    )
    monkeypatch.setattr(spider_mod, "crawl_site", lambda *_args, **_kwargs: result)
    monkeypatch.setenv("SEOHEAD_RUN_LOG", "off")

    out_dir = tmp_path / "native-run"
    assert (
        cli_main(
            [
                "crawl-site",
                "--url",
                BASE,
                "--config",
                str(config),
                "--out-dir",
                str(out_dir),
            ]
        )
        == 0
    )
    capsys.readouterr()
    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["summary"]["by_check"]["PAGINATION_CANONICAL_POLICY"] == 1
    tasks = json.loads((out_dir / "tasks.json").read_text(encoding="utf-8"))
    assert "PAGINATION_CANONICAL_POLICY" in {task["check"] for task in tasks["tasks"]}

    mcp_out_dir = tmp_path / "mcp-native-run"
    mcp_audit = HANDLERS["crawl_site"](
        url=BASE,
        config=str(config),
        out_dir=str(mcp_out_dir),
    )
    assert mcp_audit["summary"]["by_check"]["PAGINATION_CANONICAL_POLICY"] == 1
    mcp_tasks = json.loads((mcp_out_dir / "tasks.json").read_text(encoding="utf-8"))
    assert "PAGINATION_CANONICAL_POLICY" in {task["check"] for task in mcp_tasks["tasks"]}


def test_sf_cli_and_mcp_export_paths_share_policy_findings_and_tasks(tmp_path, capsys):
    from seohead.cli import main as cli_main
    from seohead.servers.sf_mcp import _do_run

    exports = tmp_path / "sf-exports"
    exports.mkdir()
    with (exports / "internal_all.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["Address", "Content Type", "Status Code", "Indexability", "Canonical Link Element 1"]
        )
        writer.writerow([BASE, "text/html", "200", "Indexable", BASE])
        writer.writerow([PAGINATION, "text/html", "200", "Indexable", BASE])

    config = tmp_path / "sf-config.json"
    config.write_text(
        json.dumps(
            {
                "canonical_policy": {
                    "pagination": [{"pattern": r"/catalog/page/\d+/?$", "policy": "self"}]
                }
            }
        ),
        encoding="utf-8",
    )
    cli_out = tmp_path / "cli-report"
    assert (
        cli_main(
            [
                "sf",
                "run",
                "--exports-dir",
                str(exports),
                "--config",
                str(config),
                "--out",
                str(cli_out),
                "--tasks",
            ]
        )
        == 0
    )
    capsys.readouterr()
    cli_audit = json.loads((cli_out / "audit.json").read_text(encoding="utf-8"))
    assert cli_audit["summary"]["by_check"]["PAGINATION_CANONICAL_POLICY"] == 1
    cli_tasks = json.loads((cli_out / "tasks.json").read_text(encoding="utf-8"))
    assert "PAGINATION_CANONICAL_POLICY" in {task["check"] for task in cli_tasks["tasks"]}

    mcp_out = tmp_path / "mcp-report"
    mcp_result = _do_run(
        "parse-exports",
        str(exports),
        config=str(config),
        out=str(mcp_out),
    )
    assert mcp_result["summary"]["by_check"]["PAGINATION_CANONICAL_POLICY"] == 1
