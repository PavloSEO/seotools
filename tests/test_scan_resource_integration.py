"""Offline end-to-end acceptance tests for the bounded resource lane."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

import pytest

from seohead.crawl.capture import CaptureEvent
from seohead.crawl.resource_fetch import ResourceFetchResult
from seohead.crawl.settings import fingerprint, load
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.crawl.sqlite_resources import capture_resources
from seohead.storage import open_scan
from seohead.storage.native_scan import NativeScan
from seohead.storage.resource_capture import commit_resource
from tests.test_scan_native import _metadata, _record, _runtime


class _Response:
    def __init__(self, content: bytes, content_type: str, status_code: int = 200) -> None:
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.status_code = status_code
        self.headers = {"content-type": content_type}


def _settings(**overrides):
    return load(
        overrides={
            "speed.min_delay_seconds": 0,
            "limits.max_urls": 100,
            "limits.max_depth": 1,
            "resources.max_requests": 100,
            **overrides,
        }
    )


def _runtime_versions():
    return {
        "python": "test",
        "sqlite": "test",
        "httpx": "test",
        "lxml": "test",
        "beautifulsoup4": "test",
    }


def _crawl(path, settings, fetcher):
    return crawl_to_scan(
        "https://example.test/",
        scan_out=str(path),
        settings=settings,
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions=_runtime_versions(),
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )


def _shared_site_fetcher(calls):
    resources = '<link rel="stylesheet" href="/shared.css"><script src="/shared.js"></script>'

    def fetcher(url):
        calls[url] += 1
        if url.endswith("/robots.txt"):
            return _Response(b"User-agent: SEOHEAD-Tools\nAllow: /\n", "text/plain")
        if url == "https://example.test/shared.css":
            return _Response(b"body{color:blue}", "text/css")
        if url == "https://example.test/shared.js":
            return _Response(b"window.shared=true", "application/javascript")
        links = "".join(f'<a href="/p{index}">P{index}</a>' for index in range(1, 10))
        return _Response(
            f"<html><head>{resources}</head><body>{links if url.endswith('/') else 'page'}</body></html>".encode(),
            "text/html",
        )

    return fetcher


def test_shared_resource_capture_keeps_occurrences_but_fetches_each_url_once(tmp_path):
    path = tmp_path / "shared.sqlite"
    calls = Counter()
    run = _crawl(path, _settings(**{"resources.fetch": True}), _shared_site_fetcher(calls))
    assert run.pages == 10
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 10
        assert con.execute("SELECT COUNT(*) FROM frontier").fetchone()[0] == 10
        assert con.execute("SELECT COUNT(*) FROM resource_refs").fetchone()[0] == 20
        assert (
            con.execute(
                "SELECT COUNT(*) FROM responses WHERE purpose IN ('script','stylesheet')"
            ).fetchone()[0]
            == 2
        )
        assert (
            con.execute(
                "SELECT COUNT(DISTINCT body_sha256) FROM responses WHERE purpose IN ('script','stylesheet')"
            ).fetchone()[0]
            == 2
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM resource_refs WHERE capture_state='measured'"
            ).fetchone()[0]
            == 20
        )
    assert calls["https://example.test/shared.css"] == 1
    assert calls["https://example.test/shared.js"] == 1


def test_disabled_default_records_declarations_without_fetching_resources(tmp_path):
    path = tmp_path / "disabled.sqlite"
    calls = Counter()
    _crawl(path, _settings(), _shared_site_fetcher(calls))
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM resource_refs").fetchone()[0] == 20
        assert (
            con.execute(
                "SELECT COUNT(*) FROM resource_refs WHERE capture_state='resources_disabled'"
            ).fetchone()[0]
            == 20
        )
        capabilities = json.loads(con.execute("SELECT capabilities_json FROM scan").fetchone()[0])
    assert calls["https://example.test/shared.css"] == 0
    assert calls["https://example.test/shared.js"] == 0
    assert capabilities["resource_refs"] == {"state": "complete", "reason": ""}
    assert capabilities["resource_bodies"] == {
        "state": "unavailable",
        "reason": "resources.fetch disabled",
    }


def test_measured_empty_inventory_is_complete_while_unavailable_html_is_not(tmp_path):
    empty_path = tmp_path / "empty.sqlite"
    unavailable_path = tmp_path / "unavailable.sqlite"

    def empty_fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(b"User-agent: SEOHEAD-Tools\nAllow: /\n", "text/plain")
        return _Response(b"<html><body>empty</body></html>", "text/html")

    def unavailable_fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(b"User-agent: SEOHEAD-Tools\nAllow: /\n", "text/plain")
        return _Response(b"x" * 32, "text/html")

    _crawl(empty_path, _settings(), empty_fetcher)
    _crawl(
        unavailable_path,
        _settings(**{"limits.max_response_bytes": 8}),
        unavailable_fetcher,
    )
    with sqlite3.connect(empty_path) as con:
        empty_caps = json.loads(con.execute("SELECT capabilities_json FROM scan").fetchone()[0])
    with sqlite3.connect(unavailable_path) as con:
        unavailable_caps = json.loads(
            con.execute("SELECT capabilities_json FROM scan").fetchone()[0]
        )
    assert empty_caps["resource_refs"] == {"state": "complete", "reason": ""}
    assert unavailable_caps["resource_refs"] == {
        "state": "unavailable",
        "reason": "resource declarations were not measured",
    }


def test_duplicate_declarations_above_reference_batch_size_fetch_once_and_resolve_all(tmp_path):
    path = tmp_path / "duplicates.sqlite"
    calls = Counter()
    duplicate = '<script src="/shared.js"></script>' * 513

    def fetcher(url):
        calls[url] += 1
        if url.endswith("/robots.txt"):
            return _Response(b"User-agent: SEOHEAD-Tools\nAllow: /\n", "text/plain")
        if url.endswith("shared.js"):
            return _Response(b"window.shared=true", "application/javascript")
        return _Response(
            f"<html><head>{duplicate}</head><body>one</body></html>".encode(), "text/html"
        )

    _crawl(path, _settings(**{"resources.fetch": True}), fetcher)
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM resource_refs").fetchone()[0] == 513
        assert (
            con.execute(
                "SELECT COUNT(*) FROM resource_refs WHERE capture_state='measured'"
            ).fetchone()[0]
            == 513
        )
        assert (
            con.execute("SELECT COUNT(DISTINCT response_id) FROM resource_refs").fetchone()[0] == 1
        )
    assert calls["https://example.test/shared.js"] == 1


@pytest.mark.parametrize(
    ("resource_response", "settings_overrides", "state", "reason"),
    [
        (
            None,
            {"resources.max_requests": 0},
            "resource_budget_exhausted",
            "resource request budget exhausted",
        ),
        (
            _Response(b"<html>wrong</html>", "text/html"),
            {},
            "body_unavailable",
            "unsupported_media",
        ),
        (
            _Response(b"secret", "application/javascript", 200),
            {},
            "body_unavailable",
            "credentialed",
        ),
        (
            _Response(b"private", "application/javascript", 200),
            {},
            "body_unavailable",
            "cache_control_no_store",
        ),
    ],
    ids=("request-limit", "mime-mismatch", "credentialed", "no-store"),
)
def test_resource_budget_mime_and_retention_omissions_are_named(
    tmp_path, monkeypatch, resource_response, settings_overrides, state, reason
):
    path = tmp_path / f"{state}-{reason}.sqlite"

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(b"User-agent: SEOHEAD-Tools\nAllow: /\n", "text/plain")
        if url.endswith("shared.js"):
            if resource_response is None:
                raise AssertionError("request limit must stop before the resource fetch")
            response = resource_response
            if reason == "cache_control_no_store":
                response.headers["cache-control"] = "no-store"
            return response
        return _Response(
            b'<html><head><script src="/shared.js"></script></head><body>one</body></html>',
            "text/html",
        )

    overrides = {"resources.fetch": True, **settings_overrides}
    if reason == "credentialed":
        overrides.update(
            {
                "http.credential_headers": [
                    {
                        "host": "example.test",
                        "headers": {"Authorization": "env:SEOHEAD_TEST_SECRET"},
                    }
                ],
                "http.credentials_acknowledged": True,
            }
        )
    if reason == "credentialed":
        monkeypatch.setenv("SEOHEAD_TEST_SECRET", "secret")
    _crawl(path, _settings(**overrides), fetcher)
    with sqlite3.connect(path) as con:
        row = con.execute("SELECT capture_state,reason FROM resource_refs").fetchone()
    assert tuple(row) == (state, reason)


def _metadata_for_resources(**overrides):
    config = _settings(**{"resources.fetch": True, **overrides})
    metadata = _metadata()
    metadata["config"] = config
    metadata["config_fingerprint"] = fingerprint(config)
    return metadata


def _event(url: str, body: bytes, content_type: str) -> CaptureEvent:
    return CaptureEvent(
        method="GET",
        requested_url=url,
        effective_url=url,
        redirect_history=(),
        requested_at="2026-09-06T10:00:00Z",
        received_at="2026-09-06T10:00:01Z",
        status_code=200,
        request_headers=(("accept", "*/*"),),
        credentials_used=False,
        response_headers=(("content-type", content_type),),
        content_type=content_type,
        content_encoding="",
        entity_bytes=body,
        body_fidelity="entity_bytes",
        body_state="complete",
        body_reason="none",
        error="",
        error_kind="",
        effective_status_code=200,
        effective_headers=(("content-type", content_type),),
        response_time=0.0,
    )


def _renderer(url: str) -> dict:
    return {
        "engine": "playwright-chromium",
        "engine_version": "test",
        "settings": {
            "viewport": {"width": 1280, "height": 720},
            "device_pixel_ratio": 1.0,
            "mobile_emulation": False,
            "touch_emulation": False,
            "script_timeout_seconds": 0.0,
            "resize_to_content": False,
            "resize_to_content_max_height_px": 15000,
            "persistent_profile": False,
        },
        "navigation": {
            "requested_url": url,
            "final_url": url,
            "wait_until": "load",
            "timeout_seconds": 30.0,
        },
        "transforms": {
            "flatten_shadow_dom_requested": False,
            "flatten_shadow_dom_applied": 0,
            "flatten_iframes_requested": False,
            "flatten_iframes_applied": 0,
        },
        "policy": {"credentials_used": False, "cache_control_no_store": False},
    }


def _rendered_record(url: str) -> dict:
    record = _record(url)
    record.update(representation="rendered", title="Rendered", word_count=1)
    return record


def test_render_replacements_reuse_resource_response_and_keep_unique_operation_context(tmp_path):
    path = tmp_path / "rendered.sqlite"
    resource_url = "https://example.test/app.js"
    settings = _settings(
        **{"resources.fetch": True, "resources.max_requests": 1, "robots.policy": "ignore"}
    )
    calls = Counter()

    def fetcher(url):
        calls[url] += 1
        assert url == resource_url
        return _Response(b"window.app=true", "application/javascript")

    with NativeScan.create(
        path, **_metadata_for_resources(**{"resources.max_requests": 1, "robots.policy": "ignore"})
    ) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        declaration = {"kind": "script", "url": resource_url, "raw_url": "/app.js"}
        scan.commit_page(
            lease,
            _record(),
            captures=[_event(lease.url, b"<html></html>", "text/html")],
            resources=[declaration],
            resource_inventory_state="complete",
            runtime=_runtime(),
        )
        capture_resources(scan, settings, fetcher=fetcher, sleeper=lambda _seconds: None)
        for captured_at in ("2026-09-06T10:02:00Z", "2026-09-06T10:03:00Z"):
            scan.commit_render(
                lease.url,
                _rendered_record(lease.url),
                html="<html><body>rendered</body></html>",
                renderer=_renderer(lease.url),
                captured_at=captured_at,
                resources=[declaration],
                resource_inventory_state="complete",
            )
            capture_resources(scan, settings, fetcher=fetcher, sleeper=lambda _seconds: None)

        refs = list(
            scan.con.execute(
                "SELECT representation,source_document_id,capture_state,response_id "
                "FROM resource_refs ORDER BY representation"
            )
        )
        context_keys = [
            row[0]
            for row in scan.con.execute(
                "SELECT item_key FROM context_items WHERE kind='resource_commit' ORDER BY item_key"
            )
        ]
        assert len(refs) == 2
        assert [row[0] for row in refs] == ["rendered", "static"]
        assert all(
            row[1] is not None and row[2] == "measured" and row[3] is not None for row in refs
        )
        assert len(context_keys) == len(set(context_keys)) == 3
    assert calls == Counter({resource_url: 1})
    with open_scan(path, require_audit=False) as reader:
        assert (
            reader.execute(
                "SELECT COUNT(*) FROM resource_refs WHERE capture_state='measured'"
            ).fetchone()[0]
            == 2
        )


def test_successful_resource_retry_is_a_noop_after_request_budget_is_consumed(tmp_path):
    path = tmp_path / "retry.sqlite"
    resource_url = "https://example.test/app.js"
    with NativeScan.create(
        path, **_metadata_for_resources(**{"resources.max_requests": 1})
    ) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            _record(),
            captures=[_event(lease.url, b"<html></html>", "text/html")],
            resources=[{"kind": "script", "url": resource_url, "raw_url": "/app.js"}],
            resource_inventory_state="complete",
            runtime=_runtime(),
        )
        url_id = scan.con.execute(
            "SELECT url_id FROM urls WHERE url=?", (resource_url,)
        ).fetchone()[0]
        outcome = ResourceFetchResult(
            (_event(resource_url, b"window.app=true", "application/javascript"),), "measured", "", 1
        )
        assert commit_resource(scan, url_id, "script", outcome, elapsed_seconds=1.0)
        assert not commit_resource(scan, url_id, "script", outcome, elapsed_seconds=1.0)
        assert (
            scan.con.execute("SELECT COUNT(*) FROM responses WHERE purpose='script'").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("failpoint", ["after_resource_body", "after_resource_references"])
def test_resource_commit_failpoints_roll_back_and_safe_retry(tmp_path, failpoint):
    path = tmp_path / f"{failpoint}.sqlite"
    resource_url = "https://example.test/app.js"
    with NativeScan.create(path, **_metadata_for_resources()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            _record(),
            captures=[_event(lease.url, b"<html></html>", "text/html")],
            resources=[{"kind": "script", "url": resource_url, "raw_url": "/app.js"}],
            resource_inventory_state="complete",
            runtime=_runtime(),
        )
        url_id = scan.con.execute(
            "SELECT url_id FROM urls WHERE url=?", (resource_url,)
        ).fetchone()[0]
        outcome = ResourceFetchResult(
            (_event(resource_url, b"window.ok=true", "application/javascript"),), "measured", "", 1
        )
        scan.failpoint = lambda point: (
            (_ for _ in ()).throw(sqlite3.OperationalError("disk full"))
            if point == failpoint
            else None
        )
        with pytest.raises(sqlite3.OperationalError, match="disk full"):
            commit_resource(scan, url_id, "script", outcome, elapsed_seconds=1.0)
        assert (
            scan.con.execute("SELECT COUNT(*) FROM responses WHERE purpose='script'").fetchone()[0]
            == 0
        )
        assert (
            scan.con.execute("SELECT capture_state FROM resource_refs").fetchone()[0]
            == "not_fetched"
        )
        scan.failpoint = None
        assert commit_resource(scan, url_id, "script", outcome, elapsed_seconds=1.0)
        assert not commit_resource(scan, url_id, "script", outcome, elapsed_seconds=1.0)
        assert (
            scan.con.execute("SELECT COUNT(*) FROM responses WHERE purpose='script'").fetchone()[0]
            == 1
        )


def test_configured_resource_response_limit_retains_no_prefix(tmp_path):
    path = tmp_path / "small-resource.sqlite"

    def fetcher(url):
        if url.endswith("/app.js"):
            return _Response(b"window.example=true", "application/javascript")
        return _Response(b'<html><script src="/app.js"></script></html>', "text/html")

    _crawl(
        path,
        _settings(
            **{
                "resources.fetch": True,
                "resources.max_response_bytes": 4,
                "robots.policy": "ignore",
            }
        ),
        fetcher,
    )
    with sqlite3.connect(path) as con:
        assert con.execute(
            "SELECT body_state,body_reason,body_sha256 FROM responses WHERE purpose='script'"
        ).fetchone() == ("truncated", "truncated", None)
        assert con.execute("SELECT capture_state,reason FROM resource_refs").fetchone() == (
            "body_unavailable",
            "truncated",
        )
