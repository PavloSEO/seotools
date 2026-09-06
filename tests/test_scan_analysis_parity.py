"""Golden audit parity for legacy graphs and the SQL-backed native scan path."""

from __future__ import annotations

import copy
import json

import pytest

from seohead.crawl.settings import load
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.servers.handlers import _audit_crawl_result
from seohead.servers.scan_handlers import _rebuild_page_result
from seohead.storage.native_scan import NativeScan
from tests.test_scan_artifact_office import frozen_office_clock as frozen_office_clock
from tests.test_scan_crawl_parity import _fetcher, _legacy, _Response, _runtime_versions


def _fixture():
    """One graph deliberately carrying every outcome-sensitive edge distinction."""
    return {
        "https://example.test/robots.txt": _Response(
            200, "User-agent: SEOHEAD-Tools\nAllow: /\n", {"content-type": "text/plain"}
        ),
        "https://example.test/": _Response(
            200,
            "<html><head><title>Start</title></head><body>"
            "<nav><a href='/a/'>click here</a><a href='/a?x=1#part'>read more</a></nav>"
            "<main><a href='/b'>descriptive anchor</a><a href='/c' rel='nofollow'>details</a>"
            "<a href='/nofollow-only' rel='nofollow'>click here</a>"
            "<a href='/deep-1'>next</a></main></body></html>",
        ),
        "https://example.test/a/": _Response(
            200, "<html><body><a href='/target'>read more</a></body></html>"
        ),
        "https://example.test/a?x=1": _Response(
            200, "<html><body><a href='/target#part'>click here</a></body></html>"
        ),
        "https://example.test/b": _Response(
            200, "<html><body><a href='/target'>guide</a></body></html>"
        ),
        "https://example.test/c": _Response(
            200,
            "<html><head><meta name='robots' content='noindex'></head>"
            "<body><a href='/nonindex-only'>click here</a></body></html>",
        ),
        "https://example.test/nofollow-only": _Response(200, "<html><body>Nofollow</body></html>"),
        "https://example.test/nonindex-only": _Response(
            200, "<html><body>Noindex source</body></html>"
        ),
        "https://example.test/deep-1": _Response(
            200, "<html><body><a href='/deep-2'>next</a></body></html>"
        ),
        "https://example.test/deep-2": _Response(
            200, "<html><body><a href='/deep-3'>next</a></body></html>"
        ),
        "https://example.test/deep-3": _Response(
            200, "<html><body><a href='/deep-4'>next</a></body></html>"
        ),
        "https://example.test/deep-4": _Response(
            200, "<html><body><a href='/deep-5'>next</a></body></html>"
        ),
        "https://example.test/deep-5": _Response(
            200, "<html><body><a href='/deep-target'>click here</a></body></html>"
        ),
        "https://example.test/deep-target": _Response(200, "<html><body>Deep target</body></html>"),
        "https://example.test/target": _Response(200, "<html><body>Target</body></html>"),
    }


def _settings(mode):
    return load(
        overrides={
            "speed.min_delay_seconds": 0,
            "limits.max_urls": 5 if mode == "partial" else 20,
            "limits.max_depth": 8,
            "link_attributes.capture": True,
            "link_position.classify": True,
            "discovery.follow_nofollow": True,
        }
    )


def _audit(result, settings, *, stored_scan=None, stored_sitemap=None, sitemap_seed=None):
    return _audit_crawl_result(
        result,
        settings=settings,
        url="https://example.test/",
        sitemap_seed=sitemap_seed or {"sitemap_url": None, "sitemap_urls": [], "declared": []},
        discovery={"mode": "spider", "directive_policy": "respect", "robots_blocked": 0},
        stored_scan=stored_scan,
        stored_sitemap=stored_sitemap,
    )


def _outcome(audit):
    """The whole audit contract except the report clock and measured response durations.

    ``generated_at`` is a report wall clock.  Every ``response_time`` in this
    contract is independently measured by each crawl, including a copied value
    in a finding's details, so a numeric duration cannot decide graph parity.
    Missing or invalid duration values, plus all thresholds, counts, URLs, and
    findings, stay strict.
    """
    measured_duration_fields = {"response_time"}

    def normalize(value):
        if isinstance(value, dict):
            return {
                key: 0.0
                if key in measured_duration_fields and type(child) in (int, float)
                else normalize(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value

    result = copy.deepcopy(audit)
    result["run"].pop("generated_at")
    return normalize(result)


def _different_paths(left, right, path="$"):
    if type(left) is not type(right):
        return [path]
    if isinstance(left, dict):
        paths = []
        for key in sorted(set(left) | set(right), key=str):
            child = f"{path}.{key}"
            if key not in left or key not in right:
                paths.append(child)
            else:
                paths.extend(_different_paths(left[key], right[key], child))
        return paths
    if isinstance(left, list):
        paths = []
        for index in range(max(len(left), len(right))):
            child = f"{path}[{index}]"
            if index >= len(left) or index >= len(right):
                paths.append(child)
            else:
                paths.extend(_different_paths(left[index], right[index], child))
        return paths
    return [] if left == right else [path]


def _at_path(value, path):
    for part in path.removeprefix("$").split("."):
        if not part:
            continue
        if "[" in part:
            name, index = part[:-1].split("[")
            value = value[name][int(index)] if name else value[int(index)]
        else:
            value = value[part]
    return value


def test_outcome_normalizes_measured_durations_but_keeps_issues_strict():
    """Parity ignores separate clocks, never a different audit conclusion."""
    left = {
        "run": {"generated_at": "2026-09-06T00:00:00Z"},
        "pages": [{"metrics": {"response_time": 0.001, "word_count": 10}}],
        "issues": [
            {
                "check": "SLOW_RESPONSE_TIME",
                "details": {"response_time": 0.001, "max_s": 1.5},
            }
        ],
    }
    right = copy.deepcopy(left)
    right["run"]["generated_at"] = "2026-09-06T00:00:01Z"
    right["pages"][0]["metrics"]["response_time"] = 0.999
    right["issues"][0]["details"]["response_time"] = 0.999

    assert _outcome(left) == _outcome(right)

    missing_duration = copy.deepcopy(right)
    missing_duration["pages"][0]["metrics"]["response_time"] = None
    assert _outcome(left) != _outcome(missing_duration)

    strict_number = copy.deepcopy(right)
    strict_number["pages"][0]["metrics"]["word_count"] = 11
    assert _outcome(left) != _outcome(strict_number)

    right["issues"].append({"check": "UNEXPECTED_PARITY_BREAK", "details": {}})
    with pytest.raises(AssertionError):
        assert _outcome(left) == _outcome(right)


@pytest.mark.parametrize("mode", ("complete", "partial", "empty", "unclassified"))
def test_sql_graph_audit_matches_legacy_without_building_all_inlinks(
    tmp_path, monkeypatch, frozen_office_clock, mode
):
    """F must preserve findings/order while the SQL path has no legacy edge materialization."""
    import seohead.crawl.evidence as evidence
    from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
    from seohead.sf.tasks import build_tasks

    settings = _settings(mode)
    responses = _fixture()
    if mode == "empty":
        responses["https://example.test/"] = _Response(200, "<html><body>Empty</body></html>")
    if mode == "unclassified":
        settings["link_position"]["classify"] = False
    legacy = _legacy(
        settings,
        _fetcher(responses),
        tmp_path / "legacy-decisions.jsonl",
        content_area_config=None,
    )
    path = tmp_path / "scan.sqlite"
    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(path),
        settings=settings,
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions=_runtime_versions(),
        fetcher=_fetcher(responses),
        sleeper=lambda _seconds: None,
    )
    with NativeScan.open(path) as scan:
        sitemap_url = "https://example.test/sitemap.xml"
        sitemap_id = scan.declare_sitemap(sitemap_url, "explicit", 0)
        declared = [page.url for page in legacy.pages]
        scan.write_sitemap_members(sitemap_id, list(enumerate(declared)))
        scan.finish_sitemap(sitemap_id, True, "")
        sitemap_seed = {
            "sitemap_url": sitemap_url,
            "sitemap_urls": [sitemap_url],
            "declared": declared,
        }
        # The protocol fetch is outside the graph parity fixture; both paths receive
        # the same complete selected-root reconciliation below.
        monkeypatch.setattr(
            "seohead.sf.core.sitemap_coverage.run_sitemap",
            lambda *_args, **_kwargs: {"sitemaps": []},
        )
        _legacy_response, legacy_audit = _audit(legacy, settings, sitemap_seed=sitemap_seed)
        # The SQL route receives pages and the transient start gate, never legacy links/forms.
        sql_result = _rebuild_page_result(scan)
        sql_result.start_page_evidence = dict(run.start_page_gate or {})
        sql_result.resumed = False
        sql_result.finish_reason = run.finish_reason

        with prepare_sitemap_reconciliation(scan.con, start_url="https://example.test/") as sitemap:
            monkeypatch.setattr(
                evidence,
                "_inlinks_frame",
                lambda *_args, **_kwargs: pytest.fail("SQL path materialized all_inlinks"),
            )
            _sql_response, sql_audit = _audit_crawl_result(
                sql_result,
                settings=settings,
                url="https://example.test/",
                sitemap_seed=sitemap_seed,
                discovery={"mode": "spider", "directive_policy": "respect", "robots_blocked": 0},
                stored_scan=scan,
                stored_sitemap=sitemap,
            )

    sql_outcome, legacy_outcome = _outcome(sql_audit), _outcome(legacy_audit)
    paths = _different_paths(sql_outcome, legacy_outcome)
    (tmp_path / "scan-analysis-parity-diff.txt").write_text("\n".join(paths) + "\n")
    assert sql_outcome == legacy_outcome, "\n".join(paths)
    assert build_tasks(sql_audit, None) == build_tasks(legacy_audit, None)
    if mode == "complete":
        assert {issue["check"] for issue in sql_audit["issues"]} >= {
            "ONLY_NOFOLLOW_INLINKS",
            "ONLY_NONINDEXABLE_SOURCE_INLINKS",
            "DEEP_DISCOVERY_PATH",
        }
    # Renderers receive comparable copies: their byte-level parity should not
    # depend on either audit's independently measured response durations.
    legacy_render_audit, sql_render_audit = _outcome(legacy_audit), _outcome(sql_audit)
    for audit in (legacy_render_audit, sql_render_audit):
        audit["run"]["generated_at"] = legacy_audit["run"]["generated_at"]
    from seohead.reports import build_report

    for fmt in ("json", "md", "csv", "xlsx", "docx"):
        left, right = tmp_path / f"legacy.{fmt}", tmp_path / f"sql.{fmt}"
        assert build_report(legacy_render_audit, fmt, str(left))["ok"]
        assert build_report(sql_render_audit, fmt, str(right))["ok"]
        if fmt == "json":
            legacy_report, sql_report = json.loads(left.read_text()), json.loads(right.read_text())
            report_paths = _different_paths(legacy_report, sql_report)
            if report_paths:
                first = report_paths[0]
                legacy_value, sql_value = (
                    _at_path(legacy_report, first),
                    _at_path(sql_report, first),
                )
                pytest.fail(
                    f"report JSON first difference {first}: "
                    f"{type(legacy_value).__name__}={legacy_value!r} vs "
                    f"{type(sql_value).__name__}={sql_value!r}"
                )
        assert left.read_bytes() == right.read_bytes()
        if fmt == "csv":
            for suffix in (".pages.csv", ".scope.csv"):
                left_sidecar, right_sidecar = left.with_suffix(suffix), right.with_suffix(suffix)
                assert left_sidecar.exists() == right_sidecar.exists()
                if left_sidecar.exists():
                    assert left_sidecar.read_bytes() == right_sidecar.read_bytes()
