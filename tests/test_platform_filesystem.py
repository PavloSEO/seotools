"""Portable OS-backed writer exclusion and platform-specific durability boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from seohead import filesystem


def test_writer_lock_contends_across_processes_and_releases(tmp_path):
    path = tmp_path / "Работа со сканом.writer.lock"  # noqa: RUF001 - Unicode path fixture.
    fd = filesystem.open_lock(path)
    filesystem.lock_exclusive(fd)
    child = "from pathlib import Path; from seohead.filesystem import open_lock,lock_exclusive; import sys; fd=open_lock(Path(sys.argv[1])); lock_exclusive(fd)"
    try:
        busy = subprocess.run(
            [sys.executable, "-c", child, str(path)], capture_output=True, timeout=10
        )
        assert busy.returncode != 0
    finally:
        filesystem.unlock(fd)
        os.close(fd)
    ready = subprocess.run(
        [sys.executable, "-c", child, str(path)], capture_output=True, timeout=10
    )
    assert ready.returncode == 0, ready.stderr.decode(errors="replace")


def test_windows_crt_lock_uses_one_fixed_byte_without_retries(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(filesystem, "fcntl", None)
    monkeypatch.setattr(
        filesystem,
        "msvcrt",
        SimpleNamespace(
            LK_NBLCK=2,
            LK_UNLCK=0,
            locking=lambda fd, mode, size: calls.append((os.lseek(fd, 0, os.SEEK_CUR), mode, size)),
        ),
    )
    fd = filesystem.open_lock(tmp_path / "lock")
    try:
        os.lseek(fd, 8, os.SEEK_SET)
        filesystem.lock_exclusive(fd)
        os.lseek(fd, 4, os.SEEK_SET)
        filesystem.unlock(fd)
    finally:
        os.close(fd)
    assert calls == [(0, 2, 1), (0, 0, 1)]


def test_windows_directory_flush_does_not_open_unsupported_crt_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(filesystem, "_WINDOWS", True)
    monkeypatch.setattr(
        filesystem.os, "open", lambda *_args, **_kwargs: pytest.fail("directory CRT open")
    )
    filesystem.fsync_directory(tmp_path)


def test_lock_rejects_hardlink_alias(tmp_path):
    path, alias = tmp_path / "lock", tmp_path / "alias"
    path.write_bytes(b"")
    os.link(path, alias)
    with pytest.raises(OSError, match="unaliased"):
        filesystem.open_lock(alias)
