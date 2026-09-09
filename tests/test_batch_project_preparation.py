"""Offline preparation-state regressions for bounded project workspaces."""

from __future__ import annotations

from seohead.projects.runtime import preparation_status, prepare_project
from seohead.projects.workspace import create_project


def test_injected_crawl_failure_is_saved_as_bounded_not_run_preparation(tmp_path):
    project = tmp_path / "project"
    create_project(project, "https://example.test/")
    calls = []

    def unavailable_crawl(**kwargs):
        calls.append(kwargs)
        return {"ok": False, "error": "synthetic crawler unavailable"}

    result = prepare_project(str(project), tools={"crawl_site": unavailable_crawl})
    preparation = result["preparation"]

    assert len(calls) == 1
    assert calls[0]["project"] == str(project)
    assert calls[0]["overrides"] == {
        "limits.max_urls": 50,
        "limits.max_requests": 150,
        "limits.max_crawl_seconds": 60,
        "sitemaps.auto_discover": True,
    }
    assert preparation["state"] == "partial"
    assert preparation["steps"]["crawl"] == {
        "state": "not_run",
        "reason": "synthetic crawler unavailable",
    }
    assert preparation["steps"]["sitemap"] == {
        "state": "not_run",
        "reason": "crawl source unavailable; sitemap coverage not established",
    }
    assert preparation_status(str(project))["steps"]["crawl"] == preparation["steps"]["crawl"]


def test_rerun_preserves_prior_operator_supplied_competitors_when_not_replaced(tmp_path):
    project = tmp_path / "project"
    create_project(project, "https://example.test/")
    candidates = [
        {
            "url": "https://competitor-one.test/",
            "source": "operator shortlist",
            "observed_at": None,
        },
        {
            "url": "https://competitor-two.test/",
            "source": "operator shortlist",
            "observed_at": None,
        },
    ]

    def unavailable_crawl(**_kwargs):
        return {"ok": False, "error": "synthetic crawler unavailable"}

    first = prepare_project(
        str(project), tools={"crawl_site": unavailable_crawl}, competitors=candidates
    )["preparation"]
    second = prepare_project(str(project), tools={"crawl_site": unavailable_crawl})["preparation"]

    assert [
        {key: row[key] for key in ("url", "source", "observed_at", "directory", "project_uuid")}
        for row in second["competitors"]
    ] == [
        {key: row[key] for key in ("url", "source", "observed_at", "directory", "project_uuid")}
        for row in first["competitors"]
    ]
    assert second["steps"]["competitors"] == {
        "state": "run",
        "reason": "operator-supplied candidate shortlist; competitiveness is not verified",
        "count": 2,
    }
