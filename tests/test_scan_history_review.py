"""Adversarial history tests over valid captures and the public preview envelope."""

import json

import pytest

from seohead.servers.history_handlers import scan_prune
from seohead.storage import ScanError, open_scan
from seohead.storage.history import prune_apply, prune_preview, snapshot_scan
from tests.test_scan_history import _finished


def test_preview_stdout_file_round_trips_to_explicit_apply(tmp_path):
    source = tmp_path / "old.sqlite"
    _finished(source, age_days=40)
    preview = scan_prune(str(tmp_path), keep_newest=0)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(preview))
    assert source.exists()
    result = scan_prune(str(tmp_path), plan=str(plan), apply=True)
    assert result == {"applied": True, "removed": [str(source)]}
    assert not source.exists()


def test_partial_corpus_is_not_an_automatic_retention_candidate(tmp_path):
    source = tmp_path / "partial.sqlite"
    _finished(source, age_days=40, captured=False)
    assert prune_preview(tmp_path, keep_newest=0)["candidates"] == []


def test_prune_rechecks_newest_group_after_another_baseline_disappears(tmp_path):
    old, recent = tmp_path / "old.sqlite", tmp_path / "recent.sqlite"
    _finished(old, age_days=50)
    _finished(recent, age_days=40)
    plan = prune_preview(tmp_path, keep_newest=1)
    assert [item["path"] for item in plan["candidates"]] == [str(old)]
    recent.unlink()
    with pytest.raises(ScanError, match="no longer eligible"):
        prune_apply(tmp_path, plan)
    assert old.exists()


def test_snapshot_directory_uses_the_copied_scan_uuid_and_no_clobber(tmp_path):
    source = tmp_path / "source.sqlite"
    _finished(source)
    destination = tmp_path / "history"
    destination.mkdir()
    result = snapshot_scan(source, destination)
    with open_scan(result, require_audit=False) as con:
        uuid = con.execute("SELECT scan_uuid FROM scan").fetchone()[0]
    assert uuid.replace("-", "")[:8] in result
    assert "example.test" in result


def test_snapshot_space_refusal_does_not_publish_an_output(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from seohead.storage import native_scan

    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    _finished(source)
    monkeypatch.setattr(
        native_scan.os, "statvfs", lambda _: SimpleNamespace(f_bavail=0, f_frsize=1)
    )
    with pytest.raises(ScanError, match="insufficient free space"):
        snapshot_scan(source, output)
    assert not output.exists()
