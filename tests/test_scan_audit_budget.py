"""Audit-document budget behavior keeps a completed native capture usable."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from seohead.crawl.collect import PageRecord
from seohead.crawl.sqlite_adapter import ScanRun
from seohead.servers import scan_handlers
from seohead.storage import MAX_JSON_BYTES
from seohead.storage.native_audit import AuditSizeError
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _link, _metadata, _runtime


class _Con:
    def __init__(self) -> None:
        self.counts = {"pages": 3, "links": 4, "forms": 0}
        self.audit_rows = 0

    def execute(self, statement, _params=()):
        if "FROM audit" in statement:
            return SimpleNamespace(fetchone=lambda: (1,) if self.audit_rows else None)
        return SimpleNamespace(fetchone=lambda: (self.counts[statement.rsplit(" ", 1)[-1]],))


class _Scan:
    current: _Scan

    def __init__(self, *, overflow: bool) -> None:
        self.con = _Con()
        self.overflow = overflow
        self.saved = None
        self.unavailable = []
        self.finished = []

    @classmethod
    def open(cls, *_args, **_kwargs):
        return cls.current

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def save_audit(self, document):
        if self.overflow:
            raise AuditSizeError("audit document exceeds the 64 MiB storage budget")
        self.saved = document
        self.con.audit_rows = 1

    def note_audit_unavailable(self, reason):
        self.unavailable.append(reason)

    def finish_capture(self, *, reason):
        self.finished.append(reason)
        return True

    def resume_snapshot(self, *, include_edges=False):
        assert include_edges
        return {"counts": self.con.counts}

    def sitemap_roots(self):
        return []


def _run() -> ScanRun:
    return ScanRun(
        path="scan.sqlite",
        pages=3,
        links=4,
        forms=0,
        lifecycle="running",
        finish_reason="finished",
        partial=False,
        start_page_gate={"html": "<html></html>", "outlinks": 0, "external_outlinks": 0},
    )


@pytest.fixture
def bridge(monkeypatch):
    def configure(*, overflow: bool):
        scan = _Scan(overflow=overflow)
        _Scan.current = scan
        monkeypatch.setattr("seohead.storage.native_scan.NativeScan", _Scan)
        monkeypatch.setattr(
            scan_handlers,
            "_producer_provenance",
            lambda _build: ("test", "a" * 40, {}),
        )
        monkeypatch.setattr(
            "seohead.crawl.sqlite_adapter.crawl_to_scan", lambda *_args, **_kwargs: _run()
        )

        @contextmanager
        def no_sitemap(*_args, **_kwargs):
            yield SimpleNamespace(available=False, reason="no saved sitemap declarations")

        monkeypatch.setattr("seohead.crawl.sql_sitemap.prepare_sitemap_reconciliation", no_sitemap)
        monkeypatch.setattr(
            scan_handlers,
            "_rebuild_page_result",
            lambda _scan: SimpleNamespace(
                partial=False,
                stopped_reason="",
                robots_blocked=[],
                seed_urls=[],
            ),
        )
        monkeypatch.setattr(
            "seohead.servers.handlers._audit_crawl_result",
            lambda *_args, **_kwargs: ({}, {"schema_version": "2.0", "pages": []}),
        )
        return scan

    return configure


def test_oversized_audit_is_named_unavailable_without_losing_native_capture(bridge):
    scan = bridge(overflow=True)

    response = scan_handlers.crawl_site_scan(
        "https://example.test/",
        scan_out="scan.sqlite",
        settings={"robots": {"policy": "respect"}},
        producer_build="a" * 40,
    )

    assert response["audit_available"] is False
    assert "budget" in response["audit_reason"]
    assert response["urls_collected"] == 3
    assert response["links_collected"] == 4
    assert scan.saved is None
    assert scan.con.audit_rows == 0
    assert scan.unavailable == [response["audit_reason"]]
    assert scan.finished == ["finished"]


def test_ordinary_audit_still_saves_after_preflight(bridge):
    scan = bridge(overflow=False)

    response = scan_handlers.crawl_site_scan(
        "https://example.test/",
        scan_out="scan.sqlite",
        settings={"robots": {"policy": "respect"}},
        producer_build="a" * 40,
    )

    assert response["audit_available"] is True
    assert response["audit_reason"] == ""
    assert scan.saved == {"schema_version": "2.0", "pages": []}
    assert scan.con.audit_rows == 1
    assert scan.unavailable == []


def test_small_budget_refuses_only_audit_document_and_keeps_native_rows(tmp_path, monkeypatch):
    path = tmp_path / "capture.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.seed_frontier(
            [
                {
                    "requested_url": "https://example.test/",
                    "frontier_url": "https://example.test/",
                    "depth": 0,
                    "reason": "",
                    "source": "start",
                    "reserve_query": False,
                    "seed": False,
                }
            ]
        )
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            vars(PageRecord(url=lease.url, content_type="text/html")),
            links=[_link(lease.url, "https://example.test/next")],
            runtime=_runtime(),
        )
        monkeypatch.setattr("seohead.storage.MAX_JSON_BYTES", 64)
        with pytest.raises(AuditSizeError, match="capture evidence is retained"):
            scan.save_audit({"oversized": "x" * 65})
        assert scan.con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1
        assert scan.con.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 1
        assert scan.con.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0

    assert MAX_JSON_BYTES == 64 * 1024 * 1024
