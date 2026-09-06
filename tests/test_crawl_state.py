"""Crawl checkpoint safety and correctness. No network.

A resumable crawl only earns its name if a hostile or corrupt state file can
never do more than force a fresh start, and if the state directory itself
cannot be written to by anything else on the machine.
"""

import json
import os
import pickle
import stat

import pytest

from seohead.crawl import state as crawl_state


def test_a_missing_checkpoint_means_start_fresh(tmp_path):
    result, note = crawl_state.load(str(tmp_path / "nope.json"), "https://example.com/")
    assert result is None
    assert "fresh" in note


def test_round_trip_preserves_the_frontier(tmp_path):
    path = str(tmp_path / "state.json")
    original = crawl_state.CrawlState(
        start_url="https://example.com/",
        queue=[("https://example.com/a", 1), ("https://example.com/b", 2)],
        seen=["https://example.com/", "https://example.com/a", "https://example.com/b"],
        max_depth_reached=2,
        config_fingerprint="abc123",
    )
    crawl_state.save(path, original)
    loaded, note = crawl_state.load(path, "https://example.com/", config_fingerprint="abc123")
    assert loaded is not None
    assert loaded.queue == original.queue
    assert loaded.seen == original.seen
    assert loaded.max_depth_reached == 2
    assert "resuming" in note


def test_a_hostile_file_cannot_execute_code_and_is_refused(tmp_path):
    """The classic unsafe-resume bug: a queue deserialised with pickle is RCE
    the moment the state directory is writable by anything else. Proves the
    loader never gets far enough to unpickle a crafted payload."""
    marker = tmp_path / "pwned"

    class Payload:
        def __reduce__(self):
            return (os.system, (f"touch {marker}",))

    path = tmp_path / "state.json"
    path.write_bytes(pickle.dumps(Payload()))

    result, note = crawl_state.load(str(path), "https://example.com/")

    assert result is None
    assert "fresh" in note
    assert not marker.exists(), "the loader must never deserialize the file's bytes as code"


def test_a_truncated_json_file_is_refused_not_raised(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"schema_version": "crawl_state.v1", "start_url": "x", "queue": [', "utf-8")
    result, note = crawl_state.load(str(path), "https://example.com/")
    assert result is None
    assert "fresh" in note


def test_a_schema_version_change_refuses_to_resume(tmp_path):
    path = str(tmp_path / "state.json")
    crawl_state.save(
        path, crawl_state.CrawlState(start_url="https://example.com/", queue=[("u", 0)])
    )
    # Simulate an older/newer build's format.
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    raw["schema_version"] = "crawl_state.v0"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)

    result, note = crawl_state.load(str(path), "https://example.com/")
    assert result is None
    assert "schema" in note


def test_a_different_start_url_refuses_to_resume(tmp_path):
    path = str(tmp_path / "state.json")
    crawl_state.save(
        path, crawl_state.CrawlState(start_url="https://example.com/", queue=[("u", 0)])
    )
    result, note = crawl_state.load(str(path), "https://other.com/")
    assert result is None
    assert "different start URL" in note


def test_a_config_fingerprint_change_refuses_to_resume(tmp_path):
    path = str(tmp_path / "state.json")
    crawl_state.save(
        path,
        crawl_state.CrawlState(
            start_url="https://example.com/", queue=[("u", 0)], config_fingerprint="v1"
        ),
    )
    result, note = crawl_state.load(str(path), "https://example.com/", config_fingerprint="v2")
    assert result is None
    assert "changed" in note


def test_a_world_writable_state_directory_is_refused(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    os.chmod(directory, 0o777)
    with pytest.raises(PermissionError):
        crawl_state.ensure_safe_dir(str(directory))


def test_a_group_writable_state_directory_is_refused(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    os.chmod(directory, 0o775)
    with pytest.raises(PermissionError):
        crawl_state.ensure_safe_dir(str(directory))


def test_a_private_state_directory_is_accepted(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    crawl_state.ensure_safe_dir(str(directory))  # must not raise


def test_an_owner_only_writable_state_directory_is_accepted(tmp_path):
    directory = tmp_path / "state755"
    directory.mkdir(mode=0o755)
    crawl_state.ensure_safe_dir(str(directory))  # must not raise: group/other read-only is fine


def test_clear_removes_a_checkpoint_and_is_idempotent(tmp_path):
    path = str(tmp_path / "state.json")
    crawl_state.save(
        path, crawl_state.CrawlState(start_url="https://example.com/", queue=[("u", 0)])
    )
    assert os.path.exists(path)
    crawl_state.clear(path)
    assert not os.path.exists(path)
    crawl_state.clear(path)  # must not raise on a second call


def test_save_is_atomic_and_leaves_no_tmp_file_behind(tmp_path):
    path = str(tmp_path / "state.json")
    crawl_state.save(
        path, crawl_state.CrawlState(start_url="https://example.com/", queue=[("u", 0)])
    )
    assert not os.path.exists(path + ".tmp")


def test_ensure_safe_dir_creates_a_non_world_writable_directory(tmp_path):
    directory = tmp_path / "new_state_dir"
    crawl_state.ensure_safe_dir(str(directory))
    mode = os.stat(directory).st_mode
    assert not (mode & stat.S_IWOTH)
