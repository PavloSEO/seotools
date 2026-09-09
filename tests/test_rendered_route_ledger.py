"""Offline store-only rendered-route ledger contracts for #693."""

from __future__ import annotations

import pytest

from seohead.crawl.settings import load
from seohead.crawl.sqlite_adapter import _document_batch
from seohead.storage.native_scan import NativeScan
from seohead.storage.rendered_routes import read
from tests.test_native_capture import _claim, _renderer
from tests.test_scan_native import _metadata, _record


def _batch(html: str, representation: str = "static"):
    from seohead.crawl.spider import Scope
    from seohead.storage.rendered_routes import observations
    from seohead.tools.parser import parse_html

    settings = load(overrides={"rendering.rendered_links.store": True, "limits.max_urls": 1})
    parsed = parse_html(html, "https://example.test/", {"max_link_observations": 20_000})
    batch = _document_batch(
        parsed,
        source_url="https://example.test/",
        depth=0,
        scope=Scope.from_config(settings["scope"]),
        start_host="example.test",
        settings=settings,
    )
    return observations(parsed, batch, representation)


def test_static_pipeline_stores_eligible_raw_observations_without_frontier_mutation(tmp_path):
    values, coverage = _batch('<a href="/foo#x">one</a><a href="mailto:x@y">skip</a>')
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _claim(scan)
        before = scan.con.execute("SELECT COUNT(*) FROM frontier").fetchone()[0]
        scan.commit_page(
            lease,
            _record(lease.url),
            runtime={
                "max_depth_reached": 0,
                "elapsed_seconds": 0.0,
                "circuit_timeout_streak": 0,
                "circuit_server_error_streak": 0,
                "crawl_delay_applied": None,
                "throttle": {"delay_seconds": 0.0, "concurrency": 1, "consecutive_ok": 0},
            },
            route_observations=values,
            route_coverage=coverage,
        )
        result = read(scan.con)
        assert result["routes"][0]["resolved_url"] == "https://example.test/foo"
        assert result["routes"][0]["relation"] == "unknown"
        assert result["routes"][0]["occurrences"][0]["raw_value"] == "/foo#x"
        assert scan.con.execute("SELECT COUNT(*) FROM frontier").fetchone()[0] == before


def test_complete_counterpart_derives_shared_by_resolved_url_not_raw_spelling(tmp_path):
    path = tmp_path / "scan.sqlite"
    static, static_coverage = _batch('<a href="/foo">raw</a>')
    rendered, rendered_coverage = _batch(
        '<a href="https://example.test/foo">rendered</a>', "rendered"
    )
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _claim(scan)
        scan.commit_page(
            lease,
            _record(lease.url),
            runtime={
                "max_depth_reached": 0,
                "elapsed_seconds": 0.0,
                "circuit_timeout_streak": 0,
                "circuit_server_error_streak": 0,
                "crawl_delay_applied": None,
                "throttle": {"delay_seconds": 0.0, "concurrency": 1, "consecutive_ok": 0},
            },
            route_observations=static,
            route_coverage=static_coverage,
        )
        record = _record(lease.url)
        record["representation"] = "rendered"
        scan.commit_render(
            lease.url,
            record,
            html="<html></html>",
            renderer=_renderer(lease.url),
            captured_at="2026-09-09T00:00:00Z",
            route_observations=rendered,
            route_coverage=rendered_coverage,
        )
        route = read(scan.con)["routes"][0]
        assert route["relation"] == "shared"
        assert {row["raw_value"] for row in route["occurrences"]} == {
            "/foo",
            "https://example.test/foo",
        }


def test_unavailable_render_coverage_keeps_static_relation_unknown_and_rolls_back(tmp_path):
    path = tmp_path / "scan.sqlite"
    static, static_coverage = _batch('<a href="/foo">raw</a>')
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _claim(scan)
        scan.commit_page(
            lease,
            _record(lease.url),
            runtime={
                "max_depth_reached": 0,
                "elapsed_seconds": 0.0,
                "circuit_timeout_streak": 0,
                "circuit_server_error_streak": 0,
                "crawl_delay_applied": None,
                "throttle": {"delay_seconds": 0.0, "concurrency": 1, "consecutive_ok": 0},
            },
            route_observations=static,
            route_coverage=static_coverage,
        )
        scan.commit_render(
            lease.url,
            None,
            html=None,
            renderer=_renderer(lease.url),
            captured_at="2026-09-09T00:00:00Z",
            body_state="unavailable",
            body_reason="fetch_failed",
            route_coverage={
                "representation": "rendered",
                "observed": 0,
                "omitted": 0,
                "completeness": "unavailable",
                "reason": "render failed",
            },
        )
        assert read(scan.con)["routes"][0]["relation"] == "unknown"
        before = scan.con.execute("SELECT COUNT(*) FROM context_items").fetchone()[0]
        scan.failpoint = lambda point: (
            (_ for _ in ()).throw(RuntimeError(point)) if point == "after_render_page" else None
        )
        with pytest.raises(RuntimeError):
            scan.commit_render(
                lease.url,
                None,
                html=None,
                renderer=_renderer(lease.url),
                captured_at="2026-09-09T00:00:01Z",
                body_state="unavailable",
                body_reason="fetch_failed",
                route_coverage={
                    "representation": "rendered",
                    "observed": 0,
                    "omitted": 0,
                    "completeness": "unavailable",
                    "reason": "render failed",
                },
            )
        assert scan.con.execute("SELECT COUNT(*) FROM context_items").fetchone()[0] == before
