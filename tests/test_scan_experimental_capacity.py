"""Storage-only admission above the stable crawler ceiling."""

from __future__ import annotations

import copy
import json

import pytest

from seohead.crawl import settings
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.servers.handlers import crawl_site
from seohead.storage import open_scan
from seohead.storage.history import inspect_scan, snapshot_scan
from seohead.storage.native_scan import NativeScan, _resume_fingerprint
from tests.test_scan_native import _metadata, _record, _runtime

ROOT = "https://example.test/"


def _experimental(pages: int) -> dict:
    return settings.load(
        overrides={
            "limits.max_urls": pages,
            "storage.capacity_profile": "experimental_synthetic",
        }
    )


def test_stable_ceiling_and_experimental_admission_are_explicit():
    with pytest.raises(settings.ConfigError, match="50,000"):
        settings.load(overrides={"limits.max_urls": 50_001})
    for pages in (50_001, 100_000, 1_000_000):
        resolved = _experimental(pages)
        assert resolved["limits"]["max_urls"] == pages
        assert settings.manifest(resolved)["storage.capacity_profile"] == ("experimental_synthetic")
        with pytest.raises(ValueError, match="50,000"):
            settings.checked_url_budget(pages)
    with pytest.raises(settings.ConfigError, match="experimental synthetic ceiling"):
        _experimental(1_000_001)
    with pytest.raises(settings.ConfigError, match=r"storage\.capacity_profile"):
        settings.load(overrides={"storage.capacity_profile": "unbounded"})


@pytest.mark.parametrize("declared_pages", (100_000, 1_000_000))
def test_experimental_artifact_reopens_and_keeps_its_declared_profile(tmp_path, declared_pages):
    path = tmp_path / "synthetic.sqlite"
    metadata = _metadata(
        **{
            "limits.max_urls": declared_pages,
            "storage.capacity_profile": "experimental_synthetic",
        }
    )
    with NativeScan.create(path, **metadata) as scan:
        scan.enqueue([(ROOT, 0)])
        scan.commit_page(scan.claim(1)[0], _record(ROOT), runtime=_runtime())
        scan.finish_capture()
    assert NativeScan.inspect(path)["scan"]["lifecycle"] == "finished"
    with open_scan(path, require_audit=False) as con:
        assert con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1
        recorded = json.loads(con.execute("SELECT config_json FROM scan").fetchone()[0])
        assert recorded["storage"]["capacity_profile"] == "experimental_synthetic"
    assert inspect_scan(path, table="pages")["rows"][0]["url"] == ROOT
    snapshot = tmp_path / "snapshot.sqlite"
    snapshot_scan(path, snapshot)
    assert NativeScan.inspect(snapshot)["counts"]["pages"] == 1


def test_experimental_interruption_reopens_with_identical_config(tmp_path):
    path = tmp_path / "interrupted.sqlite"
    metadata = _metadata(
        **{
            "limits.max_urls": 100_000,
            "storage.capacity_profile": "experimental_synthetic",
        }
    )
    with NativeScan.create(path, **metadata) as scan:
        scan.enqueue([(ROOT, 0), (ROOT + "next", 1)])
        scan.commit_page(scan.claim(1)[0], _record(ROOT), runtime=_runtime())
        scan.interrupt("synthetic checkpoint")
    with NativeScan.open(path, expected_config=metadata["config"]) as resumed:
        assert resumed.resume_snapshot()["counts"]["pages"] == 1
        assert resumed.claim(1)[0].url == ROOT + "next"


def test_old_recorded_config_without_profile_remains_compatible():
    current = settings.load()
    old = copy.deepcopy(current)
    del old["storage"]["capacity_profile"]
    assert _resume_fingerprint(current, old) == settings.fingerprint(old)


@pytest.mark.parametrize("pages", (50_000, 100_000))
def test_live_crawl_paths_refuse_experimental_population_before_io(tmp_path, pages):
    path = tmp_path / "must-not-exist.sqlite"
    config = _experimental(pages)
    with pytest.raises(ValueError, match="storage-only"):
        crawl_site(
            url=ROOT,
            scan_out=str(path),
            overrides={
                "limits.max_urls": pages,
                "storage.capacity_profile": "experimental_synthetic",
            },
        )
    assert not path.exists()
    with pytest.raises(ValueError, match="storage-only"):
        crawl_to_scan(
            ROOT,
            scan_out=str(path),
            settings=config,
            producer_version="test",
            producer_revision="a" * 40,
            runtime_versions={
                "python": "test",
                "sqlite": "test",
                "httpx": "test",
                "lxml": "test",
                "beautifulsoup4": "test",
            },
        )
    assert not path.exists()
