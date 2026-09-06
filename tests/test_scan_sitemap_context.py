"""Persisted selected-root validation and interrupted discovery."""

import json
import sqlite3

import pytest

from seohead.servers import scan_sitemaps
from seohead.storage import ScanError
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _metadata


def test_collection_cannot_erase_an_unfetched_selected_root(tmp_path):
    with NativeScan.create(
        tmp_path / "scan.sqlite",
        initial_sitemaps=(("https://example.test/a.xml", "explicit"),),
        **_metadata(),
    ) as scan:
        scan.begin_collection()
        assert scan.resume_snapshot()["scan"]["crawl_partial"] == 1


def test_discovery_replays_all_selected_roots_after_interrupted_declaration(tmp_path, monkeypatch):
    roots = ["https://example.test/a.xml", "https://example.test/b.xml"]
    with NativeScan.create(tmp_path / "scan.sqlite", **_metadata()) as scan:
        scan.declare_sitemap(roots[0], "robots", 0)
        monkeypatch.setattr(
            scan,
            "read_context",
            lambda *_args: {"fetch_state": "fetched", "parsed": {"sitemaps": roots}},
        )
        captured = []

        def capture(selected, **_kwargs):
            captured.extend(root.url for root in selected)
            return []

        monkeypatch.setattr(scan_sitemaps, "capture_declared_roots", capture)
        scan_sitemaps.load_sitemaps(
            scan,
            lambda _urls: None,
            settings={"sitemaps": {"auto_discover": True}},
            result={},
        )
        assert captured == roots
        assert [root["url"] for root in scan.sitemap_roots()] == roots


@pytest.mark.parametrize("mutation", ["duplicate_root", "overflow_id"])
def test_invalid_persisted_selected_root_has_scan_diagnostic(tmp_path, mutation):
    path = tmp_path / "scan.sqlite"
    with NativeScan.create(
        path,
        initial_sitemaps=(("https://example.test/a.xml", "explicit"),),
        **_metadata(),
    ):
        pass
    with sqlite3.connect(path) as con:
        payload = json.loads(
            con.execute(
                "SELECT payload_json FROM context_items WHERE kind='sitemap_declaration'"
            ).fetchone()[0]
        )
        if mutation == "duplicate_root":
            payload["ordinal"] = 1
            con.execute(
                "INSERT INTO context_items SELECT kind,'ordinal:1',payload_version,?,completeness,reason "
                "FROM context_items WHERE kind='sitemap_declaration'",
                (json.dumps(payload),),
            )
        else:
            payload["sitemap_url_id"] = 2**100
            con.execute(
                "UPDATE context_items SET payload_json=? WHERE kind='sitemap_declaration'",
                (json.dumps(payload),),
            )
    with pytest.raises(ScanError, match="sitemap"):
        NativeScan.inspect(path)
