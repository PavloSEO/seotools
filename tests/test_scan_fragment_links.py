"""Offline fragment-target evaluation over retained scan evidence (issue #827).

A broken bookmark is not a broken HTTP link: the fragment resolves inside the
retained destination document. These tests build synthetic ``scan.v1``
artifacts with ``NativeScan`` only -- no crawler, fetcher, browser or network.
"""

from __future__ import annotations

import csv as _csv
import dataclasses
import json
import socket
import sqlite3
from pathlib import Path

from seohead.crawl.capture import CaptureEvent
from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import fingerprint, load
from seohead.storage import fragment_links
from seohead.storage.fragment_links import evaluate, findings, paginate
from seohead.storage.native_scan import NativeScan

BASE = "https://example.test"


def _config(**overrides) -> dict:
    values = {"speed.min_delay_seconds": 0, "resources.fetch": False}
    values.update(overrides)
    return load(overrides=values)


def _metadata(config: dict) -> dict:
    return {
        "start_url": f"{BASE}/",
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


def _record(url: str, **changes) -> dict:
    fields = {
        "url": url,
        "status_code": 200,
        "content_type": "text/html",
        "size_bytes": 128,
        "response_time": 0.25,
        "crawl_depth": 0,
    }
    fields.update(changes)
    return dataclasses.asdict(PageRecord(**fields))


def _commit(scan: NativeScan, url: str, html=None, *, record=None, event=None) -> None:
    """Commit one page; ``html=None`` with ``event=None`` leaves no document."""
    scan.enqueue([(url, 0)])
    captures = ()
    if html is not None or event is not None:
        body = html if isinstance(html, bytes) else (html or "").encode()
        captures = (_event(url, body, **(event or {})),)
    scan.commit_page(
        scan.claim(1)[0],
        record if record is not None else _record(url),
        captures=captures,
        resources=[],
        resource_inventory_state="complete" if captures else None,
        runtime=_runtime(),
    )


def _render(scan: NativeScan, url: str, html: str) -> None:
    scan.commit_render(
        url,
        _record(url, representation="rendered"),
        html=html,
        renderer=_renderer(url),
        captured_at="2026-09-06T10:02:00Z",
        resources=[],
        resource_inventory_state="complete",
    )


def _evaluate(path: Path, **kwargs) -> dict:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        return evaluate(con, **kwargs)
    finally:
        con.close()


def _states(result: dict) -> dict[str, str]:
    return {item["raw_href"]: item["state"] for item in result["occurrences"]}


def _one(result: dict, raw_href: str) -> dict:
    matched = [item for item in result["occurrences"] if item["raw_href"] == raw_href]
    assert len(matched) == 1, f"expected exactly one {raw_href!r} occurrence"
    return matched[0]


def _native_audit(path: Path, config: dict, monkeypatch) -> dict:
    """Rebuild a retained scan into a native audit document, fully offline."""
    import seohead.recon.net as net
    import seohead.sf.core.sitemap_coverage as sitemap_coverage
    import seohead.tools.render as render
    from seohead.crawl.sql_sitemap import prepare_sitemap_reconciliation
    from seohead.crawl.sqlite_adapter import retained_start_gate
    from seohead.servers import handlers
    from seohead.servers.scan_handlers import _rebuild_page_result

    def fail(*_args, **_kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "socket", fail)
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    monkeypatch.setattr(net, "http_client", fail)
    monkeypatch.setattr(sitemap_coverage, "_fetch", fail)
    monkeypatch.setattr(sitemap_coverage, "http_client", fail)
    monkeypatch.setattr(render, "render_check", fail)
    monkeypatch.setattr(render, "render_document", fail)

    with NativeScan.open(path) as scan:
        result = _rebuild_page_result(scan)
        result.start_page_evidence = retained_start_gate(scan, config) or {}
        with prepare_sitemap_reconciliation(scan.con, start_url=f"{BASE}/") as sitemap:
            _unused, audit = handlers._audit_crawl_result(
                result,
                settings=config,
                url=f"{BASE}/",
                sitemap_seed={"sitemap_url": None, "sitemap_urls": [], "declared": []},
                discovery={
                    "mode": "spider",
                    "directive_policy": config["robots"]["policy"],
                    "robots_blocked": 0,
                    "sitemap_url": None,
                    "sitemap_urls": [],
                    "sitemap_seeded": 0,
                },
                stored_scan=scan,
                stored_sitemap=sitemap,
            )
    return audit


def test_same_page_targets_resolve_or_report_missing(tmp_path):
    path = tmp_path / "scan.sqlite"
    html = (
        '<html><body><a href="#section">ok</a><a href="#absent">missing</a>'
        '<div id="section">x</div></body></html>'
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", html)

    result = _evaluate(path)
    assert _states(result) == {"#section": "resolved", "#absent": "missing"}
    assert _one(result, "#section")["match"] == {"kind": "element_id", "value": "section"}
    assert _one(result, "#absent")["reason"] == "no_matching_fragment_target"
    assert result["states"] == {"resolved": 1, "missing": 1, "skipped": 0}
    assert result["coverage"]["state"] == "complete"


def test_cross_page_resolution_and_query_string_distinction(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(
            scan,
            f"{BASE}/",
            '<html><body><a href="/guide#section">a</a>'
            '<a href="/guide?x=1#section">b</a></body></html>',
        )
        _commit(scan, f"{BASE}/guide", '<html><body><div id="section"></div></body></html>')
        _commit(scan, f"{BASE}/guide?x=1", "<html><body><p>different</p></body></html>")

    result = _evaluate(path)
    assert _one(result, "/guide#section")["state"] == "resolved"
    query_hit = _one(result, "/guide?x=1#section")
    assert query_hit["state"] == "missing"
    assert query_hit["destination_url"] == f"{BASE}/guide?x=1"


def test_absent_and_non_http_destinations_are_named_skips(tmp_path):
    path = tmp_path / "scan.sqlite"
    html = (
        '<html><body><a href="/absent#x">internal</a>'
        '<a href="https://other.test/p#x">external</a>'
        '<a href="mailto:ops@example.test#x">mailto</a></body></html>'
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", html)

    result = _evaluate(path)
    assert _one(result, "/absent#x")["reason"] == "destination_not_in_scan"
    assert _one(result, "https://other.test/p#x")["reason"] == "destination_not_in_scan"
    assert _one(result, "mailto:ops@example.test#x")["reason"] == "destination_scheme_unsupported"
    assert result["states"]["missing"] == 0
    # An unreadable destination is unanswered evidence: coverage can never
    # read complete while named skips exist.
    assert result["coverage"]["state"] == "partial"
    assert result["coverage"]["occurrences_skipped"] == 3
    assert result["coverage"]["skip_reasons"] == {
        "destination_not_in_scan": 2,
        "destination_scheme_unsupported": 1,
    }


def test_case_sensitive_ids_and_duplicate_sources(tmp_path):
    path = tmp_path / "scan.sqlite"
    html = (
        '<html><body><a href="#section">lower</a><a href="#Section">upper</a>'
        '<a href="#absent">a</a><a href="#absent">b</a>'
        '<div id="Section"></div><span id="Section"></span></body></html>'
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", html)

    result = _evaluate(path)
    assert _one(result, "#section")["state"] == "missing"  # `Section` does not satisfy `section`
    assert _one(result, "#Section")["state"] == "resolved"  # duplicate ids resolve once
    missing = [item for item in result["occurrences"] if item["state"] == "missing"]
    assert [item["ordinal"] for item in missing] == [0, 2, 3]
    grouped = findings(result)
    assert len(grouped) == 2
    absent = next(g for g in grouped if g["fragment"] == "absent")
    assert absent["occurrences_count"] == 2
    assert [loc["ordinal"] for loc in absent["locations"]] == [2, 3]


def test_serialized_fragment_match_wins_before_decoding(tmp_path):
    path = tmp_path / "scan.sqlite"
    dest = '<html><body><div id="%73ection"></div><div id="section"></div></body></html>'
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", '<html><body><a href="/d#%73ection">x</a></body></html>')
        _commit(scan, f"{BASE}/d", dest)

    hit = _one(_evaluate(path), "/d#%73ection")
    # Serialized-first: the literal id `%73ection` wins over decoded `section`.
    assert hit["state"] == "resolved"
    assert hit["match"] == {"kind": "element_id", "value": "%73ection"}


def test_decoded_match_when_no_serialized_id(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", '<html><body><a href="/d#%73ection">x</a></body></html>')
        _commit(scan, f"{BASE}/d", '<html><body><div id="section"></div></body></html>')

    hit = _one(_evaluate(path), "/d#%73ection")
    assert hit["match"] == {"kind": "element_id", "value": "section"}
    assert hit["decoded_fragment"] == "section"


def test_serialized_anchor_name_wins_before_decoding(tmp_path):
    """The serialized stage tries `<a name>` too: `name="%41"` beats a decoded `id="A"`."""
    path = tmp_path / "scan.sqlite"
    dest = '<html><body><a name="%41"></a><div id="A"></div></body></html>'
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", '<html><body><a href="/d#%41">x</a></body></html>')
        _commit(scan, f"{BASE}/d", dest)

    hit = _one(_evaluate(path), "/d#%41")
    assert hit["state"] == "resolved"
    assert hit["match"] == {"kind": "anchor_name", "value": "%41"}


def test_serialized_anchor_name_resolves_without_a_decoded_target(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", '<html><body><a href="/d#%41">x</a></body></html>')
        _commit(scan, f"{BASE}/d", '<html><body><a name="%41"></a></body></html>')

    hit = _one(_evaluate(path), "/d#%41")
    assert hit["match"] == {"kind": "anchor_name", "value": "%41"}


def test_percent_decoding_utf8_plus_and_malformed(tmp_path):
    path = tmp_path / "scan.sqlite"
    dest = (
        '<html><body><div id="日本語"></div><div id="a+b"></div>'
        '<div id="a%2Bb"></div><div id="a b"></div><div id="100%"></div>'
        "</body></html>"
    )
    src = (
        '<html><body><a href="/d#%E6%97%A5%E6%9C%AC%E8%AA%9E">utf8</a>'
        '<a href="/d#a%2Bb">encoded-plus</a><a href="/d#a+b">literal-plus</a>'
        '<a href="/d#a%20b">space</a><a href="/d#100%25">percent</a>'
        '<a href="/d#%zz">malformed</a><a href="/d#%FF">invalid-utf8</a>'
        "</body></html>"
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", src)
        _commit(scan, f"{BASE}/d", dest)

    result = _evaluate(path)
    assert _one(result, "/d#%E6%97%A5%E6%9C%AC%E8%AA%9E")["state"] == "resolved"
    # `a%2Bb`: literal id `a%2Bb` wins serialized-first over decoded `a+b`.
    assert _one(result, "/d#a%2Bb")["match"] == {"kind": "element_id", "value": "a%2Bb"}
    # `a+b`: `+` is a literal plus -- decoded `a+b`, never `a b`.
    assert _one(result, "/d#a+b")["match"] == {"kind": "element_id", "value": "a+b"}
    assert _one(result, "/d#a%20b")["match"] == {"kind": "element_id", "value": "a b"}
    assert _one(result, "/d#100%25")["match"] == {"kind": "element_id", "value": "100%"}
    malformed = _one(result, "/d#%zz")
    assert malformed["state"] == "missing"
    assert malformed["decoded_fragment"] == "%zz"  # malformed escapes pass through
    invalid = _one(result, "/d#%FF")
    assert invalid["state"] == "missing"
    assert invalid["decoded_fragment"] == "�"  # invalid UTF-8 becomes U+FFFD


def test_empty_fragment_top_and_empty_id(tmp_path):
    path = tmp_path / "scan.sqlite"
    src = (
        '<html><body><a href="#">empty</a><a href="#top">top</a>'
        '<a href="#TOP">upper-top</a><div id=""></div></body></html>'
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", src)
        _commit(
            scan,
            f"{BASE}/with-top-id",
            '<html><body><div id="top"></div></body></html>',
        )
        _commit(scan, f"{BASE}/p", '<html><body><a href="/with-top-id#top">x</a></body></html>')

    result = _evaluate(path)
    assert _one(result, "#")["match"] == {"kind": "top", "value": ""}
    assert _one(result, "#top")["match"] == {"kind": "top", "value": "top"}
    assert _one(result, "#TOP")["match"] == {"kind": "top", "value": "top"}
    # A real `top` element wins before the top-of-document fallback.
    assert _one(result, "/with-top-id#top")["match"] == {"kind": "element_id", "value": "top"}


def test_anchor_name_legacy_policy(tmp_path):
    path = tmp_path / "scan.sqlite"
    html = (
        '<html><body><a name="legacy"></a><a href="#legacy">by-name</a>'
        '<a href="#unknown">none</a></body></html>'
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", html)

    result = _evaluate(path)
    assert _one(result, "#legacy")["match"] == {"kind": "anchor_name", "value": "legacy"}
    assert _one(result, "#unknown")["state"] == "missing"


def test_static_and_rendered_lanes_are_independent(tmp_path):
    path = tmp_path / "scan.sqlite"
    static_html = '<html><body><a href="#dyn">x</a><a href="#also">y</a></body></html>'
    rendered_html = (
        '<html><body><a href="#dyn">x</a><a href="#also">y</a>'
        '<a href="#rendered-only">z</a><div id="dyn"></div><div id="rendered-only"></div>'
        "</body></html>"
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", static_html)
        _render(scan, f"{BASE}/", rendered_html)

    result = _evaluate(path)
    by_key = {
        (item["raw_href"], item["source_representation"]): item for item in result["occurrences"]
    }
    assert by_key[("#dyn", "static")]["state"] == "missing"
    assert by_key[("#dyn", "rendered")]["match"] == {"kind": "element_id", "value": "dyn"}
    assert by_key[("#also", "static")]["state"] == "missing"
    assert by_key[("#also", "rendered")]["state"] == "missing"
    # A link that only exists in the rendered DOM is measured only there.
    assert ("#rendered-only", "static") not in by_key
    assert by_key[("#rendered-only", "rendered")]["state"] == "resolved"
    assert result["coverage"]["source_documents_evaluated"] == 2


def test_complete_empty_destination_proves_target_absent(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", '<html><body><a href="/empty#x">x</a></body></html>')
        _commit(scan, f"{BASE}/empty", b"")

    hit = _one(_evaluate(path), "/empty#x")
    assert hit["state"] == "missing"


def test_incomplete_evidence_is_named_never_a_finding(tmp_path):
    path = tmp_path / "scan.sqlite"
    src = (
        '<html><body><a href="/truncated#x">t</a><a href="/omitted#x">o</a>'
        '<a href="/failed#x">f</a><a href="/nodoc#x">n</a><a href="/doc.pdf#x">p</a>'
        "</body></html>"
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", src)
        _commit(
            scan,
            f"{BASE}/truncated",
            b"<html><body>partial",
            event={"body_state": "truncated", "body_reason": "truncated"},
        )
        _commit(
            scan,
            f"{BASE}/omitted",
            b"<html><body>budget</body></html>",
            event={"body_state": "omitted", "body_reason": "body_budget_exhausted"},
        )
        _commit(
            scan,
            f"{BASE}/failed",
            b"",
            event={
                "body_state": "unavailable",
                "body_reason": "fetch_failed",
                "body_fidelity": "unavailable",
            },
        )
        _commit(scan, f"{BASE}/nodoc")  # no captures: no document row at all
        _commit(
            scan,
            f"{BASE}/doc.pdf",
            b"%PDF-1.4",
            record=_record(f"{BASE}/doc.pdf", content_type="application/pdf"),
            event={
                "content_type": "application/pdf",
                "response_headers": (("content-type", "application/pdf"),),
                "effective_headers": (("content-type", "application/pdf"),),
            },
        )

    result = _evaluate(path)
    reasons = {
        item["raw_href"].split("/")[-1].split("#")[0]: item["reason"]
        for item in result["occurrences"]
    }
    assert reasons["truncated"] == "destination_body_truncated"
    assert reasons["omitted"] == "destination_body_omitted"
    assert reasons["failed"] == "destination_body_unavailable"
    assert reasons["nodoc"] == "destination_document_absent"
    assert reasons["doc.pdf"] == "destination_not_html"
    assert result["states"]["missing"] == 0
    assert result["states"]["skipped"] == 5
    assert result["coverage"]["state"] == "partial"
    assert result["coverage"]["occurrences_skipped"] == 5
    assert result["coverage"]["skip_reasons"] == {
        "destination_body_omitted": 1,
        "destination_body_truncated": 1,
        "destination_body_unavailable": 1,
        "destination_document_absent": 1,
        "destination_not_html": 1,
    }


def test_non_html_media_types_are_named_not_substring_matched(tmp_path):
    """`text/html` is a media type, not a substring: `application/xhtml+xml`
    follows XML fragment rules and `application/nothtml` is not HTML either,
    so both are named skips rather than HTML-evaluated destinations."""
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(
            scan,
            f"{BASE}/",
            '<html><body><a href="/x#t">x</a><a href="/n#t">n</a></body></html>',
        )
        for path_part, content_type in (
            ("/x", "application/xhtml+xml"),
            ("/n", "application/nothtml"),
        ):
            _commit(
                scan,
                f"{BASE}{path_part}",
                '<html><body><div id="t"></div></body></html>',
                record=_record(f"{BASE}{path_part}", content_type=content_type),
                event={
                    "content_type": content_type,
                    "response_headers": (("content-type", content_type),),
                    "effective_headers": (("content-type", content_type),),
                },
            )

    result = _evaluate(path)
    assert _one(result, "/x#t")["reason"] == "destination_not_html"
    assert _one(result, "/n#t")["reason"] == "destination_not_html"
    assert result["states"]["missing"] == 0
    assert result["coverage"]["state"] == "partial"


def test_unavailable_source_document_is_named(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(
            scan,
            f"{BASE}/broken-source",
            b"<html><body>partial",
            event={"body_state": "truncated", "body_reason": "truncated"},
        )
        _commit(scan, f"{BASE}/no-capture")

    result = _evaluate(path)
    assert result["occurrences"] == []
    unavailable = {
        (item["url"], item["reason"]) for item in result["coverage"]["unavailable_sources"]
    }
    assert (f"{BASE}/broken-source", "body_truncated/truncated") in unavailable
    assert (f"{BASE}/no-capture", "document_absent") in unavailable
    assert result["coverage"]["state"] == "partial"


def test_template_content_is_inert(tmp_path):
    path = tmp_path / "scan.sqlite"
    html = (
        '<html><body><template><div id="ghost"></div>'
        '<a href="#ghost2">inert-link</a></template>'
        '<a href="#ghost">live-link</a></body></html>'
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", html)

    result = _evaluate(path)
    assert [item["raw_href"] for item in result["occurrences"]] == ["#ghost"]
    assert _one(result, "#ghost")["state"] == "missing"


def test_base_href_resolution(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(
            scan,
            f"{BASE}/dir/page",
            '<html><head><base href="/base/"></head><body><a href="guide#sec">x</a></body></html>',
        )
        _commit(scan, f"{BASE}/base/guide", '<html><body><div id="sec"></div></body></html>')

    hit = _one(_evaluate(path), "guide#sec")
    assert hit["state"] == "resolved"
    assert hit["destination_url"] == f"{BASE}/base/guide"


def test_redirect_destination_uses_landing_document(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(
            scan,
            f"{BASE}/",
            '<html><body><a href="/old#sec">follow</a><a href="/gone#sec">gone</a></body></html>',
        )
        _commit(
            scan,
            f"{BASE}/old",
            b"<html><body>moved</body></html>",
            record=_record(f"{BASE}/old", status_code=301, redirect_url=f"{BASE}/new"),
            event={"status_code": 301},
        )
        _commit(scan, f"{BASE}/new", '<html><body><div id="sec"></div></body></html>')
        _commit(
            scan,
            f"{BASE}/gone",
            b"<html><body>moved</body></html>",
            record=_record(f"{BASE}/gone", status_code=301, redirect_url=f"{BASE}/absent"),
            event={"status_code": 301},
        )

    result = _evaluate(path)
    followed = _one(result, "/old#sec")
    assert followed["state"] == "resolved"
    assert followed["destination_document_url"] == f"{BASE}/new"
    assert _one(result, "/gone#sec")["reason"] == "destination_redirect_target_absent"


def test_fragment_directive_is_a_named_skip(tmp_path):
    path = tmp_path / "scan.sqlite"
    html = (
        '<html><body><a href="#:~:text=quoted">text-only</a>'
        '<a href="#sec:~:text=quoted">element-plus</a><div id="sec"></div>'
        '<a href="#gap:~:text=quoted">gap</a></body></html>'
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", html)

    result = _evaluate(path)
    text_only = _one(result, "#:~:text=quoted")
    assert text_only["state"] == "skipped"
    assert text_only["reason"] == "fragment_directive_unverified"
    element_plus = _one(result, "#sec:~:text=quoted")
    assert element_plus["state"] == "resolved"
    assert element_plus["note"] == "fragment_directive_unverified"
    assert _one(result, "#gap:~:text=quoted")["reason"] == "fragment_directive_unverified"


def test_pagination_is_stable_and_reports_coverage(tmp_path):
    path = tmp_path / "scan.sqlite"
    anchors = "".join(f'<a href="#m{i}">m{i}</a>' for i in range(5))
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", f"<html><body>{anchors}</body></html>")

    capped = _evaluate(path, max_occurrences=3)
    assert len(capped["occurrences"]) == 3
    assert capped["coverage"]["occurrences_omitted"] == 2
    assert capped["coverage"]["occurrences_seen"] == 5
    assert capped["coverage"]["state"] == "partial"

    full = _evaluate(path)
    first = paginate(full, offset=0, limit=2)
    again = paginate(full, offset=0, limit=2)
    assert first == again
    assert first["total"] == 5 and first["returned"] == 2 and first["next_offset"] == 2
    assert [item["raw_href"] for item in first["occurrences"]] == ["#m0", "#m1"]
    tail = paginate(full, offset=4, limit=2)
    assert tail["returned"] == 1 and tail["next_offset"] is None
    missing_only = paginate(full, state="missing")
    assert missing_only["total"] == 5 and missing_only["states"]["missing"] == 5
    empty = paginate(full, state="resolved")
    assert empty["total"] == 0 and empty["occurrences"] == []


def test_extraction_caps_mark_coverage_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(fragment_links, "MAX_ANCHORS_PER_DOCUMENT", 2)
    path = tmp_path / "scan.sqlite"
    html = '<html><body><a href="#a">1</a><a href="#b">2</a><a href="#c">3</a></body></html>'
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", html)

    result = _evaluate(path)
    assert result["coverage"]["anchors_seen"] == 3
    assert result["coverage"]["anchors_omitted"] == 1
    assert result["coverage"]["state"] == "partial"


def test_truncated_target_inventory_is_a_skip_not_a_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(fragment_links, "MAX_TARGETS_PER_DOCUMENT", 1)
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", '<html><body><a href="/d#b">x</a></body></html>')
        _commit(scan, f"{BASE}/d", '<html><body><div id="a"></div><div id="b"></div></body></html>')

    result = _evaluate(path)
    hit = _one(result, "/d#b")
    assert hit["state"] == "skipped"
    assert hit["reason"] == "destination_target_inventory_truncated"
    assert result["coverage"]["state"] == "partial"


def test_handler_cli_and_mcp_return_the_same_result(tmp_path, capsys):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(
            scan,
            f"{BASE}/",
            '<html><body><a href="#yes">y</a><a href="#no">n</a><div id="yes"></div></body></html>',
        )

    from seohead import cli
    from seohead.servers import handlers
    from seohead.servers.mcp_server import build_server

    shared = handlers.scan_fragment_links(input_path=str(path))
    rc = cli.main(["scan-fragment-links", "--scan", str(path)])
    assert rc == 0
    cli_result = json.loads(capsys.readouterr().out)
    tool = build_server()._tool_manager.get_tool("seo_scan_fragment_links")
    mcp_result = tool.fn(input_path=str(path))
    for produced in (cli_result, mcp_result):
        assert produced["occurrences"] == shared["occurrences"]
        assert produced["states"] == shared["states"]
        assert produced["coverage"] == shared["coverage"]
        assert produced["total"] == shared["total"] == 2
    filtered = handlers.scan_fragment_links(
        input_path=str(path), state="missing", representation="static"
    )
    assert filtered["total"] == 1
    assert filtered["occurrences"][0]["fragment"] == "no"


def test_native_audit_reports_broken_bookmarks_and_summary(tmp_path, monkeypatch):
    path = tmp_path / "scan.sqlite"
    config = _config()
    with NativeScan.create(path, **_metadata(config)) as scan:
        _commit(
            scan,
            f"{BASE}/",
            '<html><body><a href="/guide#section">ok</a><a href="/guide#absent">bad</a>'
            "</body></html>",
        )
        _commit(scan, f"{BASE}/guide", '<html><body><div id="section"></div></body></html>')

    audit = _native_audit(path, config, monkeypatch)

    bookmarks = [issue for issue in audit["issues"] if issue["check"] == "BROKEN_BOOKMARK"]
    assert len(bookmarks) == 1
    issue = bookmarks[0]
    assert issue["target_url"] == f"{BASE}/guide"
    assert issue["occurrences_count"] == 1
    assert issue["details"]["fragment"] == "absent"
    assert issue["locations"][0]["source_url"] == f"{BASE}/"
    assert issue["locations"][0]["raw_href"] == "/guide#absent"
    summary = audit["summary"]["fragment_links"]
    assert summary["states"] == {"resolved": 1, "missing": 1, "skipped": 0}
    assert summary["coverage"]["state"] == "complete"


def test_native_audit_skips_the_check_when_no_complete_html(tmp_path, monkeypatch):
    path = tmp_path / "scan.sqlite"
    config = _config()
    with NativeScan.create(path, **_metadata(config)) as scan:
        _commit(
            scan,
            f"{BASE}/",
            b"<html><body>partial",
            event={"body_state": "truncated", "body_reason": "truncated"},
        )

    audit = _native_audit(path, config, monkeypatch)

    assert not [issue for issue in audit["issues"] if issue["check"] == "BROKEN_BOOKMARK"]
    skipped = {entry["id"]: entry["reason"] for entry in audit["run"]["checks_skipped"]}
    assert "fragment" in skipped["BROKEN_BOOKMARK"]
    assert audit["summary"]["fragment_links"]["coverage"]["state"] == "partial"


def test_native_audit_names_the_check_when_every_destination_is_unavailable(tmp_path, monkeypatch):
    """Complete sources whose destinations are all unreadable verified
    nothing: the check must be a named skip, never a silent clean."""
    path = tmp_path / "scan.sqlite"
    config = _config()
    with NativeScan.create(path, **_metadata(config)) as scan:
        _commit(
            scan,
            f"{BASE}/",
            '<html><body><a href="/absent#x">a</a><a href="/truncated#x">t</a></body></html>',
        )
        _commit(
            scan,
            f"{BASE}/truncated",
            b"<html><body>partial",
            event={"body_state": "truncated", "body_reason": "truncated"},
        )

    audit = _native_audit(path, config, monkeypatch)

    assert not [issue for issue in audit["issues"] if issue["check"] == "BROKEN_BOOKMARK"]
    skipped = {entry["id"]: entry["reason"] for entry in audit["run"]["checks_skipped"]}
    assert "BROKEN_BOOKMARK" in skipped
    assert "BROKEN_BOOKMARK" not in audit["summary"]["check_coverage"]["checks_silent_ids"]
    assert audit["summary"]["fragment_links"]["coverage"]["state"] == "partial"


def test_native_audit_names_partial_coverage_beside_resolved_links(tmp_path, monkeypatch):
    """A resolved link next to an unverifiable destination produces no finding
    and no clean pass either: the check must be a named skip, never silent."""
    path = tmp_path / "scan.sqlite"
    config = _config()
    with NativeScan.create(path, **_metadata(config)) as scan:
        _commit(
            scan,
            f"{BASE}/",
            '<html><body><a href="#ok">y</a><a href="/not-in-scan#x">x</a>'
            '<div id="ok"></div></body></html>',
        )

    audit = _native_audit(path, config, monkeypatch)
    assert not [issue for issue in audit["issues"] if issue["check"] == "BROKEN_BOOKMARK"]
    skipped = {entry["id"]: entry["reason"] for entry in audit["run"]["checks_skipped"]}
    assert "BROKEN_BOOKMARK" in skipped
    coverage = audit["summary"]["check_coverage"]
    assert "BROKEN_BOOKMARK" not in coverage["checks_silent_ids"]
    summary = audit["summary"]["fragment_links"]
    assert summary["states"] == {"resolved": 1, "missing": 0, "skipped": 1}
    assert summary["coverage"]["state"] == "partial"


def test_malformed_fragment_href_is_a_named_skip(tmp_path):
    """A fragment-bearing href the URL resolver refuses is unanswered
    evidence: dropping it would report the page as fully verified."""
    path = tmp_path / "scan.sqlite"
    html = (
        '<html><body><a href="#yes">y</a><a href="http://[bad#frag">bad</a>'
        '<div id="yes"></div></body></html>'
    )
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", html)

    result = _evaluate(path)
    bad = _one(result, "http://[bad#frag")
    assert bad["state"] == "skipped"
    assert bad["reason"] == "href_unresolvable"
    assert bad["resolved_url"] == ""
    assert bad["fragment"] == "frag"
    assert result["states"] == {"resolved": 1, "missing": 0, "skipped": 1}
    assert result["coverage"]["anchors_seen"] == 2
    assert result["coverage"]["state"] == "partial"
    assert result["coverage"]["skip_reasons"] == {"href_unresolvable": 1}


def test_native_audit_names_an_unresolvable_fragment_href(tmp_path, monkeypatch):
    """The malformed-href skip reaches the audit: partial evidence keeps
    BROKEN_BOOKMARK out of the silent bucket instead of reading clean."""
    path = tmp_path / "scan.sqlite"
    config = _config()
    with NativeScan.create(path, **_metadata(config)) as scan:
        _commit(
            scan,
            f"{BASE}/",
            '<html><body><a href="#ok">y</a><a href="http://[bad#frag">bad</a>'
            '<div id="ok"></div></body></html>',
        )

    audit = _native_audit(path, config, monkeypatch)
    assert not [issue for issue in audit["issues"] if issue["check"] == "BROKEN_BOOKMARK"]
    skipped = {entry["id"]: entry["reason"] for entry in audit["run"]["checks_skipped"]}
    assert "BROKEN_BOOKMARK" in skipped
    assert "BROKEN_BOOKMARK" not in audit["summary"]["check_coverage"]["checks_silent_ids"]
    summary = audit["summary"]["fragment_links"]
    assert summary["states"] == {"resolved": 1, "missing": 0, "skipped": 1}
    assert summary["coverage"]["anchors_seen"] == 2
    assert summary["coverage"]["skip_reasons"] == {"href_unresolvable": 1}
    assert summary["coverage"]["state"] == "partial"


def test_export_audit_names_the_check_unavailable(tmp_path):
    """SF exports carry no destination DOM inventory: skip, never a finding."""
    from seohead.sf.core.audit import run_audit
    from seohead.sf.core.registry import CHECKS

    assert "BROKEN_BOOKMARK" in CHECKS
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        writer = _csv.writer(f)
        writer.writerow(["Address", "Content Type", "Status Code", "Status", "Indexability"])
        writer.writerow(["https://example.test/", "text/html", "200", "OK", "Indexable"])

    res = run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)
    assert "BROKEN_BOOKMARK" in {s.id for s in res.skipped}
    assert "BROKEN_BOOKMARK" not in {i.check for i in res.issues}


def test_result_is_json_serializable_and_bounded(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(scan, f"{BASE}/", '<html><body><a href="#x">x</a></body></html>')

    result = _evaluate(path)
    encoded = json.dumps(result, ensure_ascii=False)
    assert json.loads(encoded)["analysis"] == "fragment_links.v1"


def test_evaluation_is_deterministic(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(_config())) as scan:
        _commit(
            scan,
            f"{BASE}/",
            '<html><body><a href="/b#x">1</a><a href="#y">2</a></body></html>',
        )
        _commit(scan, f"{BASE}/b", '<html><body><div id="x"></div></body></html>')

    assert _evaluate(path) == _evaluate(path)
