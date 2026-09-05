"""Issue #155: a handler's own-reported failure (``ok: false``) must reach the exit code.

Before this fix, every command whose handler caught a network/parse/provider failure and
returned ``{"ok": False, ...}`` instead of raising (the documented invariant in
``docs/ARCHITECTURE.md``) printed that JSON and exited 0 regardless — a pipeline gating on
``$?`` could not tell success from failure. ``log-scan``'s exit 2 for a self-contradicting run
is a separate, deliberately distinct signal and must keep working unchanged.
"""

from __future__ import annotations

import json

from seohead import cli
from seohead.audit.site import SCHEMA
from seohead.servers import handlers


def test_ok_false_handler_result_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setitem(
        handlers.HANDLERS, "robots_check", lambda **kw: {"ok": False, "error": "boom"}
    )
    rc = cli.main(["robots-check", "--url", "https://example.com"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {"ok": False, "error": "boom"}


def test_ok_true_handler_result_still_exits_zero(monkeypatch, capsys):
    monkeypatch.setitem(handlers.HANDLERS, "robots_check", lambda **kw: {"ok": True})
    rc = cli.main(["robots-check", "--url", "https://example.com"])
    assert rc == 0


def test_a_handler_with_no_ok_field_exits_zero(monkeypatch, capsys):
    """Not every result carries ``ok`` (e.g. ``images_download``); absence is not failure."""
    monkeypatch.setitem(handlers.HANDLERS, "robots_check", lambda **kw: {"count": 0})
    rc = cli.main(["robots-check", "--url", "https://example.com"])
    assert rc == 0


def test_a_real_forced_failure_exits_nonzero(capsys):
    """The reported reproduction: an unresolvable URL forces the tool layer's own ``ok: false``
    path (no monkeypatching), and the CLI must surface that in the exit code, not just stdout."""
    rc = cli.main(["ai-bots-check", "--url", "not a url"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False


def test_log_scan_missing_run_dir_exits_one_not_two(tmp_path, capsys):
    """log-scan's own ``ok: false`` (no run to scan) is an ordinary failure — exit 1 — distinct
    from anomaly_count>0, which alone earns exit 2 (see test_logscan.py for that gate)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    rc = cli.main(["log-scan", "--run", str(empty)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["anomaly_count"] == 0


def test_site_audit_report_write_failure_exits_nonzero(monkeypatch, capsys, tmp_path):
    """Issue #347: ``site-audit --report`` builds a report from the in-memory audit and stores
    it under ``result["report"]``. The audit itself succeeding must not paper over that nested
    report write failing -- a pipeline gating on ``$?`` needs the requested deliverable's own
    status, not just the outer audit's."""
    monkeypatch.setitem(
        handlers.HANDLERS,
        "site_audit",
        lambda **kw: {
            "ok": True,
            "schema": SCHEMA,
            "domain": "example.test",
            "findings": [],
            "pages": [],
            "summary": {},
        },
    )
    # A directory where a file is expected forces the renderer's own ok:false path (no
    # monkeypatching of the report writer itself, matching the reported reproduction).
    rc = cli.main(
        ["site-audit", "--url", "https://example.test/", "--report", "xlsx", "--out", str(tmp_path)]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["report"]["ok"] is False
    assert str(tmp_path) in out["report"]["error"]
    assert rc == 1


def test_site_audit_report_success_still_exits_zero(monkeypatch, capsys, tmp_path):
    """The positive control for #347: a report that is actually written must not regress to a
    nonzero exit just because the nested-failure check now exists."""
    monkeypatch.setitem(
        handlers.HANDLERS,
        "site_audit",
        lambda **kw: {
            "ok": True,
            "schema": SCHEMA,
            "domain": "example.test",
            "findings": [],
            "pages": [],
            "summary": {},
        },
    )
    out_file = tmp_path / "audit.md"
    rc = cli.main(
        ["site-audit", "--url", "https://example.test/", "--report", "md", "--out", str(out_file)]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["report"]["ok"] is True
    assert rc == 0
    assert out_file.exists()


def test_report_build_directly_is_unaffected_by_the_nested_check(monkeypatch, capsys, tmp_path):
    """The direct ``report-build`` command has no nested ``result["report"]`` -- its own
    top-level ``ok`` must keep gating the exit status exactly as before."""
    rc = cli.main(["report-build", "--audit", "does-not-exist.json", "--format", "xlsx"])
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "report" not in out
    assert rc == 1


def test_an_uncaught_exception_still_exits_one_with_stderr_message(monkeypatch, capsys):
    """The crash path (a bug, not a reported failure) is unchanged: nothing on stdout, a concise
    message on stderr, exit 1 — same code as an ``ok: false`` result, per docs/USAGE.md."""

    def _boom(**kw):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(handlers.HANDLERS, "robots_check", _boom)
    rc = cli.main(["robots-check", "--url", "https://example.com"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "kaboom" in captured.err
