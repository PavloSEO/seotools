"""crawl-site's flag surface: --help stays short, --config-help covers the rest.

See issue #27: every subsequent crawler build-out issue was adding a new direct
CLI flag, putting crawl-site on track for 25+ flags and an unreadable --help.
The fix is not fewer settings, it is fewer of them as *flags* — --config carries
the rest, and --config-help makes that config surface discoverable.
"""

import io

import pytest

from seohead import cli
from seohead.crawl import settings as crawl_settings
from seohead.servers import handlers

# A generous but real ceiling: CI fails if crawl-site --help grows past this,
# which is the point — a new setting from here on must go through --config
# rather than becoming another direct flag.
#
# Raised by one for --urls (issue #21), and the reason is the distinction this
# guard rests on: --urls names where the crawl's input comes from, alongside
# --url, --sitemap and --input, rather than configuring how the crawl behaves.
# Settings still go through --config; if this number moves again for anything
# that is not a new input source, the flag is the thing to reconsider, not the
# ceiling.
#
# Raised again for --set and --max-urls-per-second, and neither is an input
# source, so each owes an argument.
#
# --set is not an exception to #27, it is what makes #27 hold. The fear there was
# "every subsequent crawler build-out issue adding a new direct flag, on track for
# 25+". One flag reaches all thirty-nine settings, so a setting added tomorrow
# needs no CLI change at all -- a one-time cost that removes the pressure
# permanently rather than paying it per setting.
#
# --max-urls-per-second is a safety flag, not a convenience one. A site owner
# states a rate; expressing it as speed.min_delay_seconds makes the operator
# invert it, and a decimal point in the wrong place is somebody's site under
# load. 'sf run' has carried the same flag for the same reason.
#
# The rule above still stands for anything else.
HELP_LINE_CEILING = 36


def _help_lines(capsys):
    with pytest.raises(SystemExit):
        cli.main(["crawl-site", "--help"])
    return capsys.readouterr().out.splitlines()


def test_help_output_stays_under_the_line_ceiling(capsys):
    lines = _help_lines(capsys)
    assert len(lines) <= HELP_LINE_CEILING, "\n".join(lines)


def test_help_points_at_config_and_config_help(capsys):
    lines = "\n".join(_help_lines(capsys))
    assert "--config" in lines
    assert "--config-help" in lines


def test_help_no_longer_advertises_max_depth_or_min_delay(capsys):
    """Depth and delay are exactly the settings #27 asks to move off --help.

    They must still *work* as flags (see the back-compat test below) — only
    their visibility in --help goes away.
    """
    lines = "\n".join(_help_lines(capsys))
    assert "--max-depth" not in lines
    assert "--min-delay" not in lines


def test_config_help_lists_every_setting_from_the_config_module(capsys):
    rc = cli.main(["crawl-site", "--config-help"])
    assert rc == 0
    out = capsys.readouterr().out
    for row in crawl_settings.describe_settings():
        assert row["path"] in out, row["path"]


def test_config_help_does_not_require_a_url_or_read_stdin(monkeypatch, capsys):
    class _NeverReadStdin(io.StringIO):
        def read(self, *a, **k):  # pragma: no cover - defensive guard
            raise AssertionError("--config-help must not touch stdin")

        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdin", _NeverReadStdin())
    rc = cli.main(["crawl-site", "--config-help"])
    assert rc == 0
    assert capsys.readouterr().out


# ── back-compat: flags that predate --config-help keep working ──────────────


def test_max_depth_and_min_delay_still_work_as_direct_flags(monkeypatch, capsys):
    """Old spellings are hidden from --help but not removed.

    Scripts written against crawl-site before this change pass --max-depth and
    --min-delay directly; #27 asks for a quieter --help, not a breaking change.
    """
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    monkeypatch.setitem(handlers.HANDLERS, "crawl_site", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(
        [
            "crawl-site",
            "--url",
            "https://example.com/",
            "--max-depth",
            "3",
            "--min-delay",
            "1.5",
        ]
    )
    assert rc == 0
    echo = _read_echo(capsys)
    assert echo["max_depth"] == 3
    assert echo["min_delay"] == 1.5


def test_max_urls_out_dir_and_robots_still_work_as_direct_flags(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    monkeypatch.setitem(handlers.HANDLERS, "crawl_site", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(
        [
            "crawl-site",
            "--url",
            "https://example.com/",
            "--max-urls",
            "50",
            "--out-dir",
            "/tmp/out",
            "--robots",
            "ignore",
        ]
    )
    assert rc == 0
    echo = _read_echo(capsys)
    assert echo["max_urls"] == 50
    assert echo["out_dir"] == "/tmp/out"
    assert echo["robots"] == "ignore"


def _read_echo(capsys):
    import json

    return json.loads(capsys.readouterr().out)["echo"]


# ── effective rate printed at startup (#14 acceptance criteria) ─────────────


def test_the_effective_rate_is_printed_before_the_crawl_runs(monkeypatch, capsys):
    """Politeness is a combination; the derived number must be visible up front."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    monkeypatch.setitem(handlers.HANDLERS, "crawl_site", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(["crawl-site", "--url", "https://example.com/", "--min-delay", "2"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "0.50 req/s" in err


def test_a_zero_delay_prints_as_unbounded_not_a_math_error(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    monkeypatch.setitem(handlers.HANDLERS, "crawl_site", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(["crawl-site", "--url", "https://example.com/", "--min-delay", "0"])
    assert rc == 0
    assert "unbounded" in capsys.readouterr().err


def test_the_rate_reflects_a_config_file_too_not_only_direct_flags(monkeypatch, capsys, tmp_path):
    import json as _json

    path = tmp_path / "crawl.json"
    path.write_text(_json.dumps({"speed": {"min_delay_seconds": 4}}))
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    monkeypatch.setitem(handlers.HANDLERS, "crawl_site", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(["crawl-site", "--url", "https://example.com/", "--config", str(path)])
    assert rc == 0
    assert "0.25 req/s" in capsys.readouterr().err
