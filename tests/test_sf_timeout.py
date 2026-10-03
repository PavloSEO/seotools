"""A Screaming Frog run that gives up must not lose the crawl silently.

Screaming Frog writes its exports when the crawl ends, so the timeout is a
deadline rather than a safety valve: set it below what the crawl needs and the
whole crawl is discarded. A rate limit makes that the default outcome — 3 000
URLs at 1.5 URL/s is 33 minutes of request time before any overhead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from seohead.sf.core import runner


# --- deriving the deadline --------------------------------------------------
def test_an_unknown_count_leaves_the_configured_timeout_alone():
    assert runner.derive_timeout_minutes(30, None, 1.5) == (30, "")


def test_no_rate_limit_leaves_the_configured_timeout_alone():
    assert runner.derive_timeout_minutes(30, 3000, None) == (30, "")


def test_the_timeout_is_raised_to_what_the_crawl_needs():
    minutes, note = runner.derive_timeout_minutes(30, 3000, 1.5)
    # 3000 / 1.5 = 2000 s = 33.3 min of requests, doubled, plus startup.
    assert minutes == pytest.approx(3000 / 1.5 / 60 * 2 + 5)
    assert minutes > 30
    assert "3000 URLs at 1.5/s" in note
    assert "Raising the timeout" in note


def test_a_sufficient_timeout_is_kept_and_explained():
    minutes, note = runner.derive_timeout_minutes(120, 900, 2)
    assert minutes == 120
    assert "timeout is 120 min" in note


# --- counting what the run will request -------------------------------------
def test_a_url_list_is_counted(tmp_path):
    listing = tmp_path / "urls.txt"
    listing.write_text("https://example.com/a\n\nhttps://example.com/b\n", encoding="utf-8")
    assert runner.expected_url_count("crawl-list", str(listing), {}) == 2


def test_an_explicit_count_wins():
    config = {"sf_cli": {"expected_urls": 4200}}
    assert runner.expected_url_count("crawl", "https://example.com/", config) == 4200


def test_a_sitemap_failure_is_not_fatal():
    def explode(_url):
        raise OSError("no network")

    said: list[str] = []
    count = runner.expected_url_count(
        "crawl", "https://example.com/", {}, said.append, sitemap_counter=explode
    )
    assert count is None
    assert any("sitemap URL count unavailable" in line for line in said)


def test_a_crawl_uses_the_sitemap_count():
    assert (
        runner.expected_url_count(
            "crawl", "https://example.com/", {}, lambda _m: None, sitemap_counter=lambda _u: 862
        )
        == 862
    )


# --- stopping the crawler ---------------------------------------------------
@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX here")
def test_a_timeout_kills_the_process_and_everything_it_started(tmp_path):
    # A parent that outlives its child is exactly the orphan case: the launcher
    # exits, the crawler it started keeps requesting the site.
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(120)"
    )
    with pytest.raises(subprocess.TimeoutExpired) as err:
        runner._run_watched([sys.executable, "-c", script], 2.0, str(tmp_path), lambda _m: None)
    assert "terminated" in (err.value.output or "") or "killed" in (err.value.output or "")

    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _alive(grandchild):
        time.sleep(0.1)
    assert not _alive(grandchild), "the crawler the launcher started outlived the run"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_a_finished_process_is_reported_as_already_gone(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert runner._terminate_tree(proc) == "the crawler had already exited"


def test_a_completed_run_returns_its_output(tmp_path):
    done = runner._run_watched(
        [sys.executable, "-c", "print('hello')"], 30.0, str(tmp_path), lambda _m: None
    )
    assert done.returncode == 0
    assert "hello" in done.stdout


# --- what the operator is told ----------------------------------------------
def test_the_timeout_error_names_the_budget_and_the_crawler_s_fate(tmp_path, monkeypatch):
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    monkeypatch.setattr(runner, "resolve_cli", lambda *a, **k: "/bin/sf")
    monkeypatch.setattr(runner, "_apply_rate_limit", lambda config, folder, log, **_kwargs: config)

    def timed_out(cmd, timeout, output_folder, log):
        raise subprocess.TimeoutExpired(
            cmd, timeout, output="the crawler process group was terminated"
        )

    monkeypatch.setattr(runner, "_run_watched", timed_out)

    with pytest.raises(RuntimeError) as err:
        runner.run_sf(
            mode="crawl",
            source="https://example.com/",
            output_folder=str(tmp_path / "exports"),
            config={"sf_cli": {"max_urls_per_second": 1.5, "expected_urls": 3000}},
            log=lambda _m: None,
        )
    message = str(err.value)
    assert "3000 URLs at 1.5/s" in message
    assert "process group was terminated" in message
    assert "exports only when a crawl ends" in message


def test_the_derived_timeout_is_logged_before_the_crawl_starts(tmp_path, monkeypatch):
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    monkeypatch.setattr(runner, "resolve_cli", lambda *a, **k: "/bin/sf")
    monkeypatch.setattr(runner, "_apply_rate_limit", lambda config, folder, log, **_kwargs: config)
    said: list[str] = []

    def fake(cmd, timeout, output_folder, log):
        os.makedirs(output_folder, exist_ok=True)
        with open(os.path.join(output_folder, "internal_all.csv"), "w") as handle:
            handle.write("Address\n")
        said.append(f"timeout={timeout}")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(runner, "_run_watched", fake)
    runner.run_sf(
        mode="crawl",
        source="https://example.com/",
        output_folder=str(tmp_path / "exports"),
        config={"sf_cli": {"max_urls_per_second": 1.5, "expected_urls": 3000}},
        log=said.append,
    )
    assert any("Raising the timeout" in line for line in said)
    # The raised value, not the configured 30 minutes, is what SF is given.
    assert any(line.startswith("timeout=") and float(line[8:]) > 30 * 60 for line in said)
