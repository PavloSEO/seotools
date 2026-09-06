"""Offline page-at-a-time reanalysis of retained native evidence."""

from __future__ import annotations

import copy
import dataclasses
import json
import socket
import sqlite3
from pathlib import Path

import httpx
import pytest

from seohead.crawl.capture import CaptureEvent
from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import fingerprint, load
from seohead.storage import ScanError
from seohead.storage.native_scan import NativeScan
from seohead.storage.reanalysis_pages import iterate_reparsed_pages


def test_every_page_field_has_an_explicit_replay_source():
    from seohead.crawl.collect import _record_from_parsed
    from seohead.storage.reanalysis_pages import _TRANSPORT_FIELDS

    parsed = set(_record_from_parsed({}))
    reconstructed = {
        "redirect_chain",
        "representation",
        "body_unavailable",
        "jsonld_blocks_found",
        "jsonld_blocks_parsed",
        "text_ratio",
    }
    assert not (_TRANSPORT_FIELDS & parsed)
    assert _TRANSPORT_FIELDS | parsed | reconstructed == {
        field.name for field in dataclasses.fields(PageRecord)
    }


def _config() -> dict:
    return load(
        overrides={
            "speed.min_delay_seconds": 0,
            "resources.fetch": False,
            "limits.max_depth": 2,
        }
    )


def _metadata(config: dict) -> dict:
    return {
        "start_url": "https://example.test/",
        "config": config,
        "config_fingerprint": fingerprint(config),
        "writer_version": "3.0.0",
        "writer_revision": "a" * 40,
        "runtime_versions": {
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
    }


def _runtime() -> dict:
    return {
        "max_depth_reached": 0,
        "elapsed_seconds": 0.0,
        "circuit_timeout_streak": 0,
        "circuit_server_error_streak": 0,
        "crawl_delay_applied": None,
        "throttle": {"delay_seconds": 0.0, "concurrency": 1, "consecutive_ok": 0},
    }


def _event(url: str, body: bytes, **changes) -> CaptureEvent:
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
        "response_time": 0.25,
    }
    values.update(changes)
    return CaptureEvent(**values)


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


def _record(url: str, *, representation: str = "static", content_type: str = "text/html") -> dict:
    return dataclasses.asdict(
        PageRecord(
            url=url,
            status_code=200,
            content_type=content_type,
            size_bytes=123,
            response_time=0.25,
            x_robots="noarchive",
            crawl_depth=0,
            representation=representation,
        )
    )


def _link(source: str, destination: str) -> dict:
    return {
        "source": source,
        "destination": destination,
        "anchor": "link",
        "nofollow": False,
        "position": "content",
        "rel": (),
        "target": "",
        "raw_href": destination,
    }


def _scan_with_raw_and_dom(path: Path) -> dict:
    config = _config()
    url = "https://example.test/"
    resource = {"kind": "script", "url": "https://example.test/app.js", "raw_url": "/app.js"}
    raw = b'<html><head><title>Raw title</title><script src="/app.js"></script></head><body><a href="/raw">Raw</a><form action="/raw-form"><input type="password"></form></body></html>'
    dom = '<html><head><title>DOM title</title><script src="/app.js"></script></head><body>rendered<a href="/rendered">Rendered</a><form action="/dom-form"></form></body></html>'
    with NativeScan.create(path, **_metadata(config)) as scan:
        scan.enqueue([(url, 0)])
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            _record(url),
            links=[_link(url, "https://example.test/raw")],
            forms=[{"page": url, "method": "post", "action": "/raw-form", "has_password": True}],
            captures=[_event(url, raw)],
            resources=[resource],
            resource_inventory_state="complete",
            runtime=_runtime(),
        )
        scan.commit_render(
            url,
            _record(url, representation="rendered"),
            html=dom,
            renderer=_renderer(url),
            captured_at="2026-09-06T10:01:00Z",
            links=[_link(url, "https://example.test/rendered")],
            forms=[{"page": url, "method": "get", "action": "/dom-form", "has_password": False}],
            resources=[resource],
            resource_inventory_state="complete",
        )
    return config


def _reader(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def test_reanalysis_uses_only_retained_documents_and_preserves_raw_rendered_union(
    tmp_path, monkeypatch
):
    path = tmp_path / "retained.sqlite"
    config = _scan_with_raw_and_dom(path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("offline reanalysis attempted network or browser work")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    import seohead.tools.render as render

    monkeypatch.setattr(render, "render_document", forbidden)
    with _reader(path) as con:
        before = con.total_changes
        replay = next(iterate_reparsed_pages(con, config))
        assert con.total_changes == before

    assert replay.page["status_code"] == 200
    assert replay.page["response_time"] == 0.25
    assert replay.page["x_robots"] == "noarchive"
    assert replay.page["title"] == "DOM title"
    assert replay.page["outlinks"] == 2
    assert replay.start_page_gate == {
        "html": replay.raw_html,
        "outlinks": 1,
        "external_outlinks": 0,
    }
    assert replay.static_document_id != replay.selected_document_id
    assert replay.raw_html is not None and "Raw title" in replay.raw_html
    assert replay.selected_html is not None and "DOM title" in replay.selected_html
    assert [edge["destination"] for edge in replay.links["static"]] == ["https://example.test/raw"]
    assert [edge["destination"] for edge in replay.links["rendered"]] == [
        "https://example.test/rendered"
    ]
    assert replay.forms["static"][0]["has_password"] is True
    assert replay.forms["rendered"][0]["action"] == "https://example.test/dom-form"
    assert (
        replay.resources["static"]
        == replay.resources["rendered"]
        == ({"kind": "script", "url": "https://example.test/app.js", "raw_url": "/app.js"},)
    )
    assert replay.representation_facts["static"]["state"] == "complete"
    json.dumps(dataclasses.asdict(replay), sort_keys=True)


def test_reanalysis_does_not_fabricate_missing_static_html(tmp_path):
    path = tmp_path / "missing.sqlite"
    config = _config()
    with NativeScan.create(path, **_metadata(config)) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(scan.claim(1)[0], _record("https://example.test/"), runtime=_runtime())

    with (
        _reader(path) as con,
        pytest.raises(ScanError, match="no active resource-inventory document"),
    ):
        list(iterate_reparsed_pages(con, config))


def test_reanalysis_replaces_stale_parser_fields_with_the_retained_raw_document(tmp_path):
    path = tmp_path / "changed-parser.sqlite"
    config = _config()
    url = "https://example.test/"
    stale = _record(url)
    stale.update(
        title="Stale title",
        canonical="https://example.test/stale",
        hreflang=[{"lang": "en", "raw_href": "/stale", "url": "https://example.test/stale"}],
        meta_refresh="10;url=https://example.test/stale",
    )
    with NativeScan.create(path, **_metadata(config)) as scan:
        scan.enqueue([(url, 0)])
        scan.commit_page(
            scan.claim(1)[0],
            stale,
            captures=[_event(url, b"<html><head></head><body>fresh</body></html>")],
            resources=[],
            resource_inventory_state="complete",
            runtime=_runtime(),
        )

    with _reader(path) as con:
        replay = next(iterate_reparsed_pages(con, config))
    assert replay.page["title"] == ""
    assert replay.page["canonical"] == ""
    assert replay.page["hreflang"] == []
    assert replay.page["meta_refresh"] == ""


def test_reanalysis_marks_resource_inventory_unavailable_when_the_recorded_parser_disabled_it(
    tmp_path,
):
    path = tmp_path / "resources-disabled.sqlite"
    config = _scan_with_raw_and_dom(path)
    recorded_without_resources = copy.deepcopy(config)
    recorded_without_resources.pop("resources")

    with _reader(path) as con:
        replay = next(iterate_reparsed_pages(con, recorded_without_resources))
    assert replay.resources["static"] == ()
    assert replay.representation_facts["static"]["resource_inventory_state"] == "unavailable"
    assert replay.representation_facts["static"]["resource_inventory_reason"] == (
        "resource declarations were not parsed"
    )


def test_reanalysis_uses_the_static_response_effective_url_for_relative_links(tmp_path):
    path = tmp_path / "redirect.sqlite"
    config = _config()
    old = "https://example.test/old"
    final = "https://example.test/dir/new"
    with NativeScan.create(path, **{**_metadata(config), "start_url": old}) as scan:
        scan.enqueue([(old, 0)])
        scan.commit_page(
            scan.claim(1)[0],
            _record(old),
            captures=[
                _event(
                    old,
                    b'<html><body><a href="child">child</a></body></html>',
                    effective_url=final,
                    redirect_history=(
                        {
                            "request_url": old,
                            "status_code": 301,
                            "location_raw": "/dir/new",
                            "next_url": final,
                            "blocked": False,
                        },
                    ),
                )
            ],
            resources=[],
            resource_inventory_state="complete",
            runtime=_runtime(),
        )

    with _reader(path) as con:
        replay = next(iterate_reparsed_pages(con, config))
    assert replay.links["static"][0]["destination"] == "https://example.test/dir/child"


def test_reanalysis_start_gate_uses_raw_counts_even_when_link_storage_is_disabled(tmp_path):
    path = tmp_path / "gate.sqlite"
    config = load(
        overrides={
            "speed.min_delay_seconds": 0,
            "discovery.hyperlinks.store": False,
            "discovery.external.store": False,
        }
    )
    url = "https://example.test/"
    raw = b'<html><body><a href="/internal">internal</a><a href="https://other.test/x">external</a></body></html>'
    with NativeScan.create(path, **_metadata(config)) as scan:
        scan.enqueue([(url, 0)])
        scan.commit_page(
            scan.claim(1)[0],
            _record(url),
            captures=[_event(url, raw)],
            resources=[],
            resource_inventory_state="complete",
            runtime=_runtime(),
        )

    with _reader(path) as con:
        replay = next(iterate_reparsed_pages(con, config))
    assert replay.links["static"] == ()
    assert replay.start_page_gate == {"html": raw.decode(), "outlinks": 2, "external_outlinks": 1}


def test_reanalysis_uses_the_recorded_parser_limit_not_the_collector_default(tmp_path, monkeypatch):
    path = tmp_path / "parser-limit.sqlite"
    config = load(overrides={"speed.min_delay_seconds": 0, "limits.max_response_bytes": 777})
    url = "https://example.test/"
    with NativeScan.create(path, **_metadata(config)) as scan:
        scan.enqueue([(url, 0)])
        scan.commit_page(
            scan.claim(1)[0],
            _record(url),
            captures=[_event(url, b"<html><body>bounded</body></html>")],
            resources=[],
            resource_inventory_state="complete",
            runtime=_runtime(),
        )

    import seohead.storage.reanalysis_pages as pages

    observed = []
    original = pages._apply_body

    def bounded(*args, **kwargs):
        observed.append(kwargs["max_response_bytes"])
        return original(*args, **kwargs)

    monkeypatch.setattr(pages, "_apply_body", bounded)
    with _reader(path) as con:
        next(iterate_reparsed_pages(con, config))
    assert observed == [777]


def test_reanalysis_accepts_a_complete_zero_byte_html_body(tmp_path):
    path = tmp_path / "empty.sqlite"
    config = _config()
    url = "https://example.test/"
    with NativeScan.create(path, **_metadata(config)) as scan:
        scan.enqueue([(url, 0)])
        scan.commit_page(
            scan.claim(1)[0],
            _record(url),
            captures=[_event(url, b"")],
            resources=[],
            resource_inventory_state="complete",
            runtime=_runtime(),
        )

    with _reader(path) as con:
        replay = next(iterate_reparsed_pages(con, config))
    assert replay.raw_html == replay.selected_html == ""
    assert replay.page["title"] == ""
    assert replay.start_page_gate == {"html": "", "outlinks": 0, "external_outlinks": 0}


def test_reanalysis_preserves_non_html_transport_without_a_document(tmp_path):
    path = tmp_path / "binary.sqlite"
    config = _config()
    with NativeScan.create(path, **_metadata(config)) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(
            scan.claim(1)[0],
            _record("https://example.test/", content_type="application/pdf"),
            runtime=_runtime(),
        )

    with _reader(path) as con:
        replay = next(iterate_reparsed_pages(con, config))
    assert replay.page["content_type"] == "application/pdf"
    assert replay.page["status_code"] == 200
    assert replay.links == {"static": ()}
    assert replay.raw_html is None
