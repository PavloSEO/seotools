"""NativeScan integration coverage for retained transport observations."""

from __future__ import annotations

import json
import sqlite3

import pytest

from seohead.crawl.capture import CaptureEvent
from seohead.crawl.settings import fingerprint
from seohead.crawl.settings import load as load_config
from seohead.crawl.sqlite_adapter import retained_start_gate
from seohead.storage import ScanError, open_scan
from seohead.storage.bodies import read_document
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _link, _metadata, _record, _runtime


def _event(
    url: str, body: bytes = b"<html><title>Captured</title></html>", **changes
) -> CaptureEvent:
    values = {
        "method": "GET",
        "requested_url": url,
        "effective_url": url,
        "redirect_history": (),
        "requested_at": "2026-09-06T10:00:00Z",
        "received_at": "2026-09-06T10:00:01Z",
        "status_code": 200,
        "request_headers": (("accept", "text/html"),),
        "credentials_used": False,
        "response_headers": (("content-type", "text/html; charset=utf-8"),),
        "content_type": "text/html; charset=utf-8",
        "content_encoding": "",
        "entity_bytes": body,
        "body_fidelity": "entity_bytes",
        "body_state": "complete",
        "body_reason": "none",
        "error": "",
        "error_kind": "",
        "effective_status_code": 200,
        "effective_headers": (("content-type", "text/html; charset=utf-8"),),
        "response_time": 0.1,
    }
    values.update(changes)
    return CaptureEvent(**values)


def _claim(scan: NativeScan, url: str = "https://example.test/"):
    scan.enqueue([(url, 0 if url.endswith("/") else 1)])
    return scan.claim(1)[0]


def _metadata_for(config):
    metadata = _metadata()
    metadata["config"] = config
    metadata["config_fingerprint"] = fingerprint(config)
    return metadata


def _renderer(url: str):
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


def test_commit_page_disk_full_after_bodies_rolls_back_retained_capture(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _claim(scan)

        def fail_after_bodies(point):
            if point == "after_bodies":
                raise sqlite3.OperationalError("database or disk is full")

        scan.failpoint = fail_after_bodies
        with pytest.raises(sqlite3.OperationalError, match="disk is full"):
            scan.commit_page(
                lease,
                _record(),
                links=[_link(lease.url, "https://example.test/next")],
                captures=[_event(lease.url)],
                runtime=_runtime(),
            )

        for table in ("pages", "responses", "documents", "bodies", "links", "forms"):
            assert scan.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert (
            scan.con.execute(
                "SELECT state FROM frontier WHERE url_id=?", (lease.url_id,)
            ).fetchone()[0]
            == "inflight"
        )


def test_native_preflight_low_disk_refuses_before_any_page_commit(tmp_path, monkeypatch):
    import seohead.storage.native_scan as native_scan

    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        monkeypatch.setattr(
            native_scan.shutil,
            "disk_usage",
            lambda _path: type("Usage", (), {"free": 0})(),
        )
        with pytest.raises(ScanError, match="insufficient free disk"):
            scan.preflight_capture()
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
        assert scan.con.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0


def test_native_capture_deduplicates_bodies_and_keeps_document_lineage(tmp_path):
    path = tmp_path / "scan.sqlite"
    shared = b"<html><body>same retained entity</body></html>"
    with NativeScan.create(path, **_metadata()) as scan:
        first = _claim(scan)
        scan.enqueue([("https://example.test/second", 1)])
        scan.commit_page(
            first,
            _record(first.url),
            links=[_link(first.url, "https://example.test/linked")],
            forms=[
                {"page": first.url, "method": "get", "action": "/search", "has_password": False}
            ],
            captures=[_event(first.url, shared)],
            runtime=_runtime(),
        )
        second = scan.claim(1)[0]
        scan.commit_page(
            second,
            _record(second.url),
            captures=[_event(second.url, shared)],
            runtime={**_runtime(), "max_depth_reached": 1},
        )

        assert scan.con.execute("SELECT COUNT(*) FROM bodies").fetchone()[0] == 1
        document_ids = [
            row[0]
            for row in scan.con.execute("SELECT document_id FROM pages ORDER BY page_ordinal")
        ]
        assert len(document_ids) == 2 and all(document_ids)
        assert (
            scan.con.execute("SELECT source_document_id FROM links").fetchone()[0]
            == document_ids[0]
        )
        assert (
            scan.con.execute("SELECT source_document_id FROM forms").fetchone()[0]
            == document_ids[0]
        )
        assert (
            scan.con.execute("SELECT COUNT(DISTINCT body_sha256) FROM documents").fetchone()[0] == 1
        )


@pytest.mark.parametrize(
    ("overrides", "event_changes", "html_state", "html_reason", "body_state", "body_reason"),
    [
        ({}, {}, "complete", "", "complete", "none"),
        (
            {"storage.body_mode": "off"},
            {},
            "unavailable",
            "body retention is disabled",
            "omitted",
            "not_enabled",
        ),
        (
            {},
            {"credentials_used": True},
            "partial",
            "some HTML documents are missing or not retained",
            "omitted",
            "credentialed",
        ),
    ],
    ids=("captured", "retention-off", "credentialed-omission"),
)
def test_native_capture_capabilities_distinguish_retained_off_and_omitted(
    tmp_path, overrides, event_changes, html_state, html_reason, body_state, body_reason
):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(**overrides)) as scan:
        lease = _claim(scan)
        scan.commit_page(
            lease,
            _record(),
            captures=[_event(lease.url, **event_changes)],
            runtime=_runtime(),
        )
        capabilities = json.loads(
            scan.con.execute("SELECT capabilities_json FROM scan").fetchone()[0]
        )
        response = scan.con.execute("SELECT body_state,body_reason FROM responses").fetchone()

    assert capabilities["responses"] == {"state": "complete", "reason": ""}
    assert capabilities["html_bodies"] == {"state": html_state, "reason": html_reason}
    assert tuple(response) == (body_state, body_reason)


def test_exact_capture_retry_is_idempotent_with_one_response_document_and_body(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _claim(scan)
        kwargs = {"captures": [_event(lease.url)], "runtime": _runtime()}
        first = scan.commit_page(lease, _record(), **kwargs)
        retry = scan.commit_page(lease, _record(), **kwargs)
        assert not first.already_committed and retry.already_committed
        assert tuple(
            scan.con.execute(
                "SELECT COUNT(*), (SELECT COUNT(*) FROM documents), (SELECT COUNT(*) FROM bodies) FROM responses"
            ).fetchone()
        ) == (1, 1, 1)


def test_retained_native_document_opens_through_validated_read_only_reader(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _claim(scan)
        scan.commit_page(lease, _record(), captures=[_event(lease.url)], runtime=_runtime())

    con = open_scan(path, require_audit=False)
    try:
        document_id = con.execute("SELECT document_id FROM pages").fetchone()[0]
        assert (
            read_document(con, document_id, max_decoded_bytes=1024)
            == "<html><title>Captured</title></html>"
        )
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM documents")
    finally:
        con.close()


def test_native_credential_config_is_redacted_but_resume_verifies_the_live_secret(
    tmp_path, monkeypatch
):
    path = tmp_path / "scan.sqlite"
    secret_name = "SEOHEAD_NATIVE_CAPTURE_TOKEN"
    secret = "Bearer local-test-secret"
    profile = str(tmp_path / "private-browser-profile")
    monkeypatch.setenv(secret_name, secret)
    config = load_config(
        overrides={
            "speed.min_delay_seconds": 0,
            "http.credential_headers": [
                {"host": "example.test", "headers": {"Authorization": f"env:{secret_name}"}}
            ],
            "http.credentials_acknowledged": True,
            "rendering.browser.persistent_profile_dir": profile,
        }
    )
    with NativeScan.create(path, **_metadata_for(config)) as scan:
        lease = _claim(scan)
        scan.commit_page(lease, _record(), captures=[_event(lease.url)], runtime=_runtime())
        recorded = scan.con.execute("SELECT config_json FROM scan").fetchone()[0]

    assert "REDACTED" in recorded
    assert secret not in recorded
    assert secret_name not in recorded
    assert profile not in recorded
    monkeypatch.delenv(secret_name)
    reader = open_scan(path, require_audit=False)
    reader.close()

    monkeypatch.setenv(secret_name, secret)
    with NativeScan.open(path, expected_config=config):
        pass
    monkeypatch.setenv(secret_name, "Bearer changed-secret")
    with pytest.raises(ScanError, match="credential context differs"):
        NativeScan.open(path, expected_config=config)


def test_session_changed_capture_marks_implicit_context_and_refuses_resume(tmp_path, monkeypatch):
    path = tmp_path / "scan.sqlite"
    secret_name = "SEOHEAD_SESSION_CHANGED_TOKEN"
    monkeypatch.setenv(secret_name, "Bearer stable-secret")
    config = load_config(
        overrides={
            "speed.min_delay_seconds": 0,
            "http.credential_headers": [
                {"host": "example.test", "headers": {"Authorization": f"env:{secret_name}"}}
            ],
            "http.credentials_acknowledged": True,
        }
    )
    with NativeScan.create(path, **_metadata_for(config)) as scan:
        lease = _claim(scan)
        scan.commit_page(
            lease,
            _record(),
            captures=[_event(lease.url, session_changed=True)],
            runtime=_runtime(),
        )
        context = json.loads(
            scan.con.execute(
                "SELECT payload_json FROM context_items WHERE kind='credential_context' AND item_key='run'"
            ).fetchone()[0]
        )
        assert context["implicit_state"] is True

    with pytest.raises(ScanError, match="implicit cookie or browser credential state"):
        NativeScan.open(path, expected_config=config)


def test_retained_start_gate_rebuilds_static_html_when_page_selection_is_rendered(tmp_path):
    path = tmp_path / "scan.sqlite"
    settings = load_config(overrides={"speed.min_delay_seconds": 0})
    static_html = (
        '<html><body><a href="/from-static">internal</a>'
        '<a href="https://other.test/from-static">external</a></body></html>'
    )
    with NativeScan.create(path, **_metadata_for(settings)) as scan:
        lease = _claim(scan)
        scan.commit_page(
            lease,
            _record(),
            captures=[_event(lease.url, static_html.encode())],
            runtime=_runtime(),
        )
        rendered = _record()
        rendered.update(representation="rendered", outlinks=99, external_outlinks=42)
        scan.commit_render(
            lease.url,
            rendered,
            html="<html><body>rendered-only</body></html>",
            renderer=_renderer(lease.url),
            captured_at="2026-09-06T10:01:00Z",
        )

        gate = retained_start_gate(scan, settings)

    assert gate == {"html": static_html, "outlinks": 2, "external_outlinks": 1}
