"""Sparse writer summaries stay exact without rescanning a growing empty corpus."""

from __future__ import annotations

import json
import sqlite3

from seohead.crawl.capture import CaptureEvent
from seohead.storage import corpus, native_scan
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _metadata, _record, _runtime


def _url(number: int) -> str:
    return f"https://example.test/p/{number}"


def _capture(url: str) -> CaptureEvent:
    return CaptureEvent(
        method="GET",
        requested_url=url,
        effective_url=url,
        redirect_history=(),
        requested_at="2026-10-03T00:00:00Z",
        received_at="2026-10-03T00:00:01Z",
        status_code=200,
        request_headers=(),
        credentials_used=False,
        response_headers=(("content-type", "text/html"),),
        content_type="text/html",
        content_encoding="",
        entity_bytes=b"<html>fixture</html>",
        body_fidelity="entity_bytes",
        body_state="complete",
        body_reason="none",
        error="",
        error_kind="",
        effective_status_code=200,
        effective_headers=(("content-type", "text/html"),),
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


def _record_for(url: str) -> dict:
    record = _record(url)
    record["status_code"] = 200
    return record


def _commit(scan: NativeScan, url: str, *, captured: bool = False) -> None:
    lease = scan.claim(1)[0]
    assert lease.url == url
    scan.commit_page(
        lease,
        _record_for(url),
        runtime=_runtime(),
        captures=[_capture(url)] if captured else (),
    )


def _assert_exact(scan: NativeScan, full_summary, full_reanalysis) -> None:
    row = scan.con.execute(
        "SELECT capabilities_json,corpus_partial,retention_json FROM scan"
    ).fetchone()
    saved = json.loads(row[0])
    complete = full_summary(scan.con, json.loads(row[2]))
    for name, expected in complete["capabilities"].items():
        assert saved[name] == expected
    assert saved["offline_reanalysis"] == full_reanalysis(scan.con)
    assert bool(row[1]) == complete["corpus_partial"]


def test_sparse_summary_is_reused_and_verified_after_rollback_and_reopen(monkeypatch, tmp_path):
    full_summary = corpus.corpus_summary
    full_reanalysis = native_scan._reanalysis_capability
    counts = {"summary": 0, "reanalysis": 0}

    def counted_summary(*args):
        counts["summary"] += 1
        return full_summary(*args)

    def counted_reanalysis(*args):
        counts["reanalysis"] += 1
        return full_reanalysis(*args)

    monkeypatch.setattr(corpus, "corpus_summary", counted_summary)
    monkeypatch.setattr(native_scan, "_reanalysis_capability", counted_reanalysis)
    path = tmp_path / "sparse.sqlite"
    metadata = _metadata(**{"storage.body_mode": "off"})
    with NativeScan.create(path, **metadata) as scan:
        scan.enqueue([(_url(i), 1) for i in range(4)])
        for i in range(3):
            _commit(scan, _url(i))
            _assert_exact(scan, full_summary, full_reanalysis)
        assert counts == {"summary": 1, "reanalysis": 1}

        lease = scan.claim(1)[0]

        def fail(point):
            if point == "before_commit":
                raise sqlite3.OperationalError("synthetic interrupted write")

        scan.failpoint = fail
        try:
            scan.commit_page(
                lease,
                _record_for(lease.url),
                runtime=_runtime(),
                captures=[_capture(lease.url)],
            )
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("the synthetic failpoint did not fire")
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 3
        scan.failpoint = None
        scan.commit_page(lease, _record_for(lease.url), runtime=_runtime())
        _assert_exact(scan, full_summary, full_reanalysis)
        assert counts == {"summary": 2, "reanalysis": 2}

    # Opening performs its own full validation. A new writer derives the sparse
    # invariant once more, then reuses it without trusting memory from a prior run.
    with NativeScan.open(path, expected_config=metadata["config"]) as scan:
        prior = dict(counts)
        scan.enqueue([(_url(4), 1), (_url(5), 1)])
        _commit(scan, _url(4))
        _commit(scan, _url(5))
        _assert_exact(scan, full_summary, full_reanalysis)
        assert counts["summary"] == prior["summary"] + 1
        assert counts["reanalysis"] == prior["reanalysis"] + 1
    assert NativeScan.inspect(path)["counts"]["pages"] == 6


def test_body_or_render_evidence_falls_back_to_full_summary(monkeypatch, tmp_path):
    full_summary = corpus.corpus_summary
    full_reanalysis = native_scan._reanalysis_capability
    calls = [0]

    def counted_summary(*args):
        calls[0] += 1
        return full_summary(*args)

    monkeypatch.setattr(corpus, "corpus_summary", counted_summary)
    path = tmp_path / "mixed.sqlite"
    with NativeScan.create(path, **_metadata(**{"storage.body_mode": "off"})) as scan:
        scan.enqueue([(_url(i), 1) for i in range(3)])
        _commit(scan, _url(0))
        assert calls[0] == 1
        _commit(scan, _url(1), captured=True)
        assert calls[0] == 2
        _assert_exact(scan, full_summary, full_reanalysis)
        _commit(scan, _url(2))
        assert calls[0] == 3  # earlier captured evidence remains present
        _assert_exact(scan, full_summary, full_reanalysis)
        rendered = _record_for(_url(1))
        rendered["representation"] = "rendered"
        scan.commit_render(
            _url(1),
            rendered,
            html="<html>rendered</html>",
            renderer=_renderer(_url(1)),
            captured_at="2026-10-03T00:00:02Z",
        )
        assert calls[0] == 4
        _assert_exact(scan, full_summary, full_reanalysis)


def test_retained_body_policy_never_uses_sparse_fast_path(monkeypatch, tmp_path):
    full_summary = corpus.corpus_summary
    full_reanalysis = native_scan._reanalysis_capability
    calls = [0]

    def counted_summary(*args):
        calls[0] += 1
        return full_summary(*args)

    monkeypatch.setattr(corpus, "corpus_summary", counted_summary)
    path = tmp_path / "retained.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([(_url(0), 1), (_url(1), 1)])
        _commit(scan, _url(0))
        _commit(scan, _url(1), captured=True)
        assert calls[0] == 2
        _assert_exact(scan, full_summary, full_reanalysis)


def test_resource_fetch_policy_keeps_full_coverage_calculation(monkeypatch, tmp_path):
    full_summary = corpus.corpus_summary
    full_reanalysis = native_scan._reanalysis_capability
    calls = [0]

    def counted_summary(*args):
        calls[0] += 1
        return full_summary(*args)

    monkeypatch.setattr(corpus, "corpus_summary", counted_summary)
    path = tmp_path / "resources.sqlite"
    metadata = _metadata(**{"storage.body_mode": "off", "resources.fetch": True})
    with NativeScan.create(path, **metadata) as scan:
        scan.enqueue([(_url(0), 1), (_url(1), 1)])
        _commit(scan, _url(0))
        _commit(scan, _url(1))
        assert calls[0] == 2
        _assert_exact(scan, full_summary, full_reanalysis)


def test_bounded_sparse_profile_does_one_full_summary_for_128_page_commits(monkeypatch, tmp_path):
    full_summary = corpus.corpus_summary
    calls = [0]

    def counted_summary(*args):
        calls[0] += 1
        return full_summary(*args)

    monkeypatch.setattr(corpus, "corpus_summary", counted_summary)
    path = tmp_path / "bounded-sparse.sqlite"
    with NativeScan.create(path, **_metadata(**{"storage.body_mode": "off"})) as scan:
        scan.enqueue((_url(i), 1) for i in range(128))
        for i in range(128):
            _commit(scan, _url(i))
        assert calls[0] == 1
        _assert_exact(scan, full_summary, native_scan._reanalysis_capability)
