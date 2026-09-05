"""A running SF audit must not block the stdio server or outlive its cancellation (#369).

``sf_audit_run`` used to call its blocking live-crawl work directly on FastMCP's single
asyncio event loop: no other request could be served while it ran, and a client's
``CancelledNotification`` had no running task to interrupt, so the spawned Screaming
Frog process kept going until the runner's own timeout. This uses a synthetic
executable in place of the licensed SF CLI, exactly the shape of the issue's own
reproduction, to prove the fix without needing Screaming Frog installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import stat
import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Registers its pid, then blocks forever -- a stand-in for a licensed SF CLI that
# never finishes on its own, so only an explicit stop (ours, or the runner's own
# timeout) ends it.
_SLEEPING_SF = """#!/usr/bin/env python3
import os, time
from pathlib import Path
Path(os.environ["FAKE_SF_PID_FILE"]).write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(0.1)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _cleanup(pid: int | None) -> None:
    if pid is None or not _alive(pid):
        return
    with contextlib.suppress(OSError, ProcessLookupError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)


async def _wait_for_pid(pid_file: Path, timeout: float = 5.0) -> int:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if pid_file.exists():
            return int(pid_file.read_text(encoding="utf-8"))
        await asyncio.sleep(0.02)
    raise AssertionError("synthetic sf executable did not start in time")


def _sleeping_sf_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    fake = tmp_path / "fake_sf.py"
    fake.write_text(_SLEEPING_SF, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    pid_file = tmp_path / "pid"
    out = tmp_path / "out"
    # A generous runner deadline: long enough that only our own cancellation cleanup,
    # not the runner's unrelated timeout (#9), can explain the process dying in time.
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"sf_cli": {"path": str(fake), "timeout_minutes": 5}}))
    return fake, urls, pid_file, out, config


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX here")
def test_cancelling_a_live_crawl_stops_the_child_and_frees_the_server(tmp_path):
    """Positive control: a client cancellation kills the crawl's process group well
    before any timeout, and the server keeps answering other requests meanwhile."""

    async def run() -> dict:
        _, urls, pid_file, out, config = _sleeping_sf_fixture(tmp_path)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "seohead.servers.mcp_server"],
            cwd=ROOT,
            env={**os.environ, "FAKE_SF_PID_FILE": str(pid_file)},
        )
        pid = None
        try:
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                call = asyncio.create_task(
                    session.call_tool(
                        "sf_audit_run",
                        {
                            "mode": "crawl-list",
                            "input": str(urls),
                            "config": str(config),
                            "out": str(out),
                        },
                    )
                )
                pid = await _wait_for_pid(pid_file)
                # `initialize` is request 0, so the running tool call is request 1.
                await session.send_notification(
                    types.ClientNotification(
                        types.CancelledNotification(
                            params=types.CancelledNotificationParams(
                                requestId=1, reason="test deadline"
                            )
                        )
                    )
                )
                # The server must still answer a second, unrelated sf_* call while
                # the (now-cancelled) crawl is still winding down.
                served_while_active = await session.call_tool(
                    "sf_list_exports",
                    {"exports_dir": str(tmp_path)},
                    read_timeout_seconds=timedelta(seconds=2),
                )
                try:
                    await asyncio.wait_for(call, timeout=8)
                    terminal = "completed"
                except McpError:
                    terminal = "cancelled"
                await asyncio.sleep(0.5)  # let cleanup finish killing the child
                return {
                    "served_while_active": served_while_active.isError,
                    "terminal": terminal,
                    "child_alive_after": _alive(pid),
                    "audit_json_exists": (out / "audit.json").exists(),
                    "audit_md_exists": (out / "audit.md").exists(),
                }
        finally:
            _cleanup(pid)

    result = asyncio.run(run())

    assert result["served_while_active"] is False, "a second sf_* call must stay serviceable"
    assert result["terminal"] == "cancelled"
    assert result["child_alive_after"] is False, "cancellation must reach the process group"
    # A pre-export cancellation must never look like a completed audit.
    assert result["audit_json_exists"] is False
    assert result["audit_md_exists"] is False


@pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX here")
def test_an_uncancelled_crawl_still_completes_normally(tmp_path):
    """Negative control: the neighbouring legitimate case -- a crawl nobody cancels --
    must still run to completion and write its reports exactly as before."""
    exports_fixture = os.path.join(ROOT, "tests", "fixtures")

    async def run() -> dict:
        out = tmp_path / "out"
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "seohead.servers.mcp_server"], cwd=ROOT
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "sf_audit_run",
                {"mode": "parse-exports", "input": exports_fixture, "out": str(out)},
            )
            return {"isError": result.isError}

    result = asyncio.run(run())
    assert result["isError"] is False
    assert (tmp_path / "out" / "audit.json").exists()
    assert (tmp_path / "out" / "audit.md").exists()


def test_cancelling_a_crawl_leaves_unrelated_child_processes_alone():
    """The negative control the first shape of this fix could not have passed.

    Reaching the crawler by replacing ``subprocess.Popen`` for the duration of a
    crawl also collects every unrelated child started anywhere in the process
    during that window -- ``subprocess.run`` resolves that module global at call
    time -- and cancelling the crawl then sends SIGTERM to each one's process
    group. Registering the process where it is created cannot do that, and this
    is what says so.
    """
    import subprocess
    import time

    from seohead.sf.core import runner

    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        # Nothing registered it, because nothing in this repository starts a
        # child except the runner -- so a cancellation has nothing to reach.
        assert runner.terminate_live_crawls() == []
        time.sleep(0.2)
        assert bystander.poll() is None, "an unrelated process was killed by the crawl teardown"
    finally:
        bystander.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            bystander.wait(timeout=5)
