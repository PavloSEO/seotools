"""Adversarial transaction coverage for native rendered evidence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from seohead.storage import ScanError
from seohead.storage.native_scan import NativeScan
from tests.test_native_capture import _claim, _renderer
from tests.test_scan_native import _link, _metadata, _record, _runtime


def _rendered_record(url: str) -> dict:
    record = _record(url)
    record.update(
        representation="rendered",
        title="Rendered title",
        word_count=42,
        outlinks=2,
        external_outlinks=0,
    )
    return record


def _static_page(scan: NativeScan):
    lease = _claim(scan)
    scan.commit_page(
        lease,
        _record(lease.url),
        links=[_link(lease.url, "https://example.test/raw", "raw")],
        runtime=_runtime(),
    )
    return lease


@pytest.mark.parametrize("failpoint", ("after_render_body", "after_render_page"))
def test_render_failpoints_rollback_document_page_graph_and_revision(tmp_path, failpoint):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _static_page(scan)
        baseline = {
            table: scan.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("documents", "bodies", "links", "forms")
        }
        revision = scan.con.execute("SELECT evidence_revision FROM scan").fetchone()[0]

        def fail(point):
            if point == failpoint:
                raise RuntimeError(f"injected {point}")

        scan.failpoint = fail
        with pytest.raises(RuntimeError, match=failpoint):
            scan.commit_render(
                lease.url,
                _rendered_record(lease.url),
                html="<html><title>Rendered title</title><body>rendered</body></html>",
                renderer=_renderer(lease.url),
                captured_at="2026-09-06T11:00:00Z",
                links=[_link(lease.url, "https://example.test/rendered", "rendered")],
            )

        assert {
            table: scan.con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in baseline
        } == baseline
        assert tuple(scan.con.execute("SELECT representation,title FROM pages").fetchone()) == (
            "static",
            "Home",
        )
        assert scan.con.execute("SELECT evidence_revision FROM scan").fetchone()[0] == revision


def test_render_body_off_keeps_accepted_page_and_omitted_document(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata(**{"storage.body_mode": "off"})) as scan:
        lease = _static_page(scan)
        document_id = scan.commit_render(
            lease.url,
            _rendered_record(lease.url),
            html="<html><title>Rendered title</title><body>rendered</body></html>",
            renderer=_renderer(lease.url),
            captured_at="2026-09-06T11:00:00Z",
            links=[_link(lease.url, "https://example.test/rendered", "rendered")],
        )
        page = scan.con.execute(
            "SELECT document_id,representation,title FROM pages WHERE url_id=?", (lease.url_id,)
        ).fetchone()
        document = scan.con.execute(
            "SELECT fidelity,body_state,body_reason,body_sha256 FROM documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        capabilities = json.loads(
            scan.con.execute("SELECT capabilities_json FROM scan").fetchone()[0]
        )

    assert tuple(page) == (document_id, "rendered", "Rendered title")
    assert tuple(document) == ("unavailable", "omitted", "not_enabled", None)
    assert capabilities["rendered_bodies"] == {
        "state": "unavailable",
        "reason": "body retention is disabled",
    }


def test_render_keeps_raw_and_rendered_edge_occurrences_and_invalidates_audit(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _static_page(scan)
        before = scan.con.execute("SELECT evidence_revision FROM scan").fetchone()[0]
        scan.con.execute(
            "INSERT INTO audit(singleton,schema_version,evidence_revision,analyzer_version,"
            "analyzer_revision,created_at,sha256,document_json) VALUES(?,?,?,?,?,?,?,?)",
            (1, "crawl.v1", before, "test", "a" * 40, "2026-09-06T10:00:00Z", "0" * 64, "{}"),
        )
        scan.con.commit()
        scan.commit_render(
            lease.url,
            _rendered_record(lease.url),
            html="<html><title>Rendered title</title><body>rendered</body></html>",
            renderer=_renderer(lease.url),
            captured_at="2026-09-06T11:00:00Z",
            links=[_link(lease.url, "https://example.test/rendered", "rendered")],
        )
        edges = list(
            scan.con.execute(
                "SELECT u.url,l.evidence_representation FROM links l "
                "JOIN urls u ON u.url_id=l.destination_url_id ORDER BY l.evidence_representation"
            )
        )

        assert scan.con.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
        assert scan.con.execute("SELECT evidence_revision FROM scan").fetchone()[0] == before + 1
    assert [tuple(edge) for edge in edges] == [
        ("https://example.test/rendered", "rendered"),
        ("https://example.test/raw", "static"),
    ]


def test_render_no_store_policy_omits_dom_without_dropping_selected_page(tmp_path):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _static_page(scan)
        renderer = _renderer(lease.url)
        renderer["policy"] = {"credentials_used": False, "cache_control_no_store": True}
        document_id = scan.commit_render(
            lease.url,
            _rendered_record(lease.url),
            html="<html><title>Rendered title</title><body>rendered</body></html>",
            renderer=renderer,
            captured_at="2026-09-06T11:00:00Z",
        )
        page = scan.con.execute("SELECT document_id,representation FROM pages").fetchone()
        document = scan.con.execute(
            "SELECT body_state,body_reason,body_sha256 FROM documents WHERE document_id=?",
            (document_id,),
        ).fetchone()

    assert tuple(page) == (document_id, "rendered")
    assert tuple(document) == ("omitted", "cache_control_no_store", None)


def test_render_preflight_refuses_before_creating_document_or_replacing_page(tmp_path, monkeypatch):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _static_page(scan)
        monkeypatch.setattr(
            "seohead.storage.native_scan.shutil.disk_usage", lambda _path: SimpleNamespace(free=0)
        )
        with pytest.raises(ScanError, match="insufficient free disk"):
            scan.commit_render(
                lease.url,
                _rendered_record(lease.url),
                html="<html><body>rendered</body></html>",
                renderer=_renderer(lease.url),
                captured_at="2026-09-06T11:00:00Z",
            )
        assert scan.con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert tuple(scan.con.execute("SELECT representation,title FROM pages").fetchone()) == (
            "static",
            "Home",
        )


def test_render_refuses_large_dom_before_writing_under_low_disk(tmp_path, monkeypatch):
    path = tmp_path / "low-disk-render.sqlite"
    with NativeScan.create(
        path,
        **_metadata(
            **{
                "storage.max_body_bytes": 20 * 1024 * 1024,
                "storage.min_free_bytes": 0,
            }
        ),
    ) as scan:
        lease = _static_page(scan)
        monkeypatch.setattr(
            "seohead.storage.native_scan.shutil.disk_usage",
            lambda _path: SimpleNamespace(free=9 * 1024 * 1024),
        )

        with pytest.raises(ScanError, match="insufficient free disk"):
            scan.commit_render(
                lease.url,
                _rendered_record(lease.url),
                html=b"x" * (15 * 1024 * 1024),
                renderer=_renderer(lease.url),
                captured_at="2026-09-06T11:00:00Z",
            )

        assert scan.con.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert scan.con.execute("SELECT COUNT(*) FROM bodies").fetchone()[0] == 0
        assert tuple(scan.con.execute("SELECT representation,title FROM pages").fetchone()) == (
            "static",
            "Home",
        )


def test_unknown_renderer_failure_is_stored_as_unavailable_evidence(tmp_path):
    from seohead.crawl.settings import load
    from seohead.crawl.sqlite_render import _unknown_renderer

    path = tmp_path / "scan.sqlite"
    settings = load(overrides={"speed.min_delay_seconds": 0})
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _static_page(scan)
        document_id = scan.commit_render(
            lease.url,
            None,
            html=None,
            renderer=_unknown_renderer(lease.url, settings),
            captured_at="2026-09-06T11:00:00Z",
            body_state="unavailable",
            body_reason="fetch_failed",
        )
        assert tuple(
            scan.con.execute(
                "SELECT fidelity,body_state,body_reason FROM documents WHERE document_id=?",
                (document_id,),
            ).fetchone()
        ) == ("unavailable", "unavailable", "fetch_failed")
