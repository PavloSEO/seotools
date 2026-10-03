from __future__ import annotations

import json
import os
import sys

import pytest

from scripts import linux_lifecycle_smoke as smoke


def test_revision_requires_an_exact_lowercase_commit_sha():
    revision = "a" * 40
    assert smoke.validate_revision(revision) == revision
    for value in ("b" * 39, "B" * 40, "g" * 40, "a" * 41, "main"):
        with pytest.raises(ValueError, match="40-character Git commit SHA"):
            smoke.validate_revision(value)


def test_invalid_revision_is_rejected_before_temporary_paths_are_created(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "linux_lifecycle_smoke.py",
            "--base-ref",
            "main",
            "--metrics-out",
            str(tmp_path / "metrics.json"),
        ],
    )
    monkeypatch.setattr(
        smoke.tempfile,
        "mkdtemp",
        lambda **_kwargs: pytest.fail("temporary path created before revision validation"),
    )

    with pytest.raises(SystemExit) as exc:
        smoke.main()

    assert exc.value.code == 2
    assert not list(tmp_path.iterdir())


def test_active_release_switch_rejects_paths_outside_the_release_root(tmp_path):
    install_root = tmp_path / "install"
    releases = tmp_path / "releases"
    old_release = releases / ("a" * 40)
    outside_release = tmp_path / "outside"
    install_root.mkdir()
    releases.mkdir()
    old_release.mkdir()
    outside_release.mkdir()
    current = install_root / "current"
    smoke.switch_current(
        current, old_release, "base", release_root=releases, install_root=install_root
    )

    with pytest.raises(ValueError, match="outside its owned root"):
        smoke.switch_current(
            current,
            outside_release,
            "bad",
            release_root=releases,
            install_root=install_root,
        )

    assert current.resolve() == old_release.resolve()


def test_active_release_switch_rejects_a_symlink_escape(tmp_path):
    install_root = tmp_path / "install"
    releases = tmp_path / "releases"
    outside_release = tmp_path / "outside"
    install_root.mkdir()
    releases.mkdir()
    outside_release.mkdir()
    escaped = releases / ("b" * 40)
    escaped.symlink_to(outside_release, target_is_directory=True)

    with pytest.raises(ValueError, match="outside its owned root"):
        smoke.switch_current(
            install_root / "current",
            escaped,
            "escape",
            release_root=releases,
            install_root=install_root,
        )


def test_run_measured_drains_large_stdout_and_stderr_without_deadlock(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "process_group_rss_kib", lambda _group: 0)
    code = "import sys; print('O'*200000); print('E'*200000, file=sys.stderr)"

    stdout, measurements = smoke.run_measured(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=os.environ.copy(),
        timeout=10,
    )

    assert len(stdout) == 200_000
    assert stdout == "O" * 200_000
    assert measurements["timed_out"] is False
    assert measurements["stdout_bytes"] > 200_000
    assert measurements["stderr_bytes"] > 200_000


def test_run_measured_keeps_stdout_stderr_and_metrics_on_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "process_group_rss_kib", lambda _group: 0)
    code = (
        "import sys, time; print('stdout timeout marker', flush=True); "
        "print('stderr timeout marker', file=sys.stderr, flush=True); time.sleep(10)"
    )

    with pytest.raises(smoke.MeasuredCommandError) as exc:
        smoke.run_measured(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            env=os.environ.copy(),
            timeout=0.2,
        )

    assert "stdout timeout marker" in exc.value.stdout_tail
    assert "stderr timeout marker" in exc.value.stderr_tail
    assert exc.value.measurements["timed_out"] is True
    assert exc.value.measurements["exit_code"] is not None


def test_main_writes_metrics_artifact_when_installation_fails(tmp_path, monkeypatch):
    candidate = "b" * 40
    base = "a" * 40
    metrics_path = tmp_path / "metrics.json"
    real_mkdtemp = smoke.tempfile.mkdtemp

    def fake_run(args, **_kwargs):
        if args == ["git", "status", "--porcelain"]:
            return ""
        if args == ["git", "rev-parse", "HEAD"]:
            return candidate
        if args[:2] == ["git", "cat-file"]:
            return ""
        if args == ["uv", "--version"]:
            raise RuntimeError("synthetic dependency installation failure")
        raise AssertionError(f"unexpected command: {args!r}")

    monkeypatch.setattr(
        sys,
        "argv",
        ["linux_lifecycle_smoke.py", "--base-ref", base, "--metrics-out", str(metrics_path)],
    )
    monkeypatch.setattr(smoke, "has_linux_procfs", lambda: True)
    monkeypatch.setattr(smoke, "run", fake_run)
    monkeypatch.setattr(
        smoke.tempfile, "mkdtemp", lambda **kwargs: real_mkdtemp(dir=tmp_path, **kwargs)
    )

    assert smoke.main() == 1
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["failure"] == {
        "type": "RuntimeError",
        "message": "synthetic dependency installation failure",
    }
    assert not list(tmp_path.glob("seohead-linux-lifecycle-*"))
