"""Ensure explicit CLI source flags do not consume piped standard input.

The regression surfaced when
``while read u; do seohead parse --url "$u"; done < urls.txt`` consumed the
entire input file during the first iteration.
"""

import io
import json

import pytest

from seohead import cli
from seohead.servers import handlers


class _NeverReadStdin(io.StringIO):
    """Standard input that fails the test if anything attempts to read it."""

    def read(self, *a, **k):  # pragma: no cover - defensive test guard
        raise AssertionError("CLI read stdin even though a source flag was provided")

    def isatty(self):
        return False


def test_parse_with_url_flag_ignores_stdin(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", _NeverReadStdin("https://other.example/\n" * 100))
    monkeypatch.setitem(handlers.HANDLERS, "parse", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(["parse", "--url", "https://example.com/"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["echo"]["url"] == "https://example.com/"


def test_duplicate_check_still_reads_piped_stdin(monkeypatch, capsys):
    """Without source flags, piped input continues to work as before."""
    payload = {"items": [{"id": "a", "text": "x " * 50}], "threshold": 0.9}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setitem(handlers.HANDLERS, "duplicate_check", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(["duplicate-check"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["echo"]["items"] and out["echo"]["threshold"] == 0.9


def test_segment_diff_target_keeps_stdin_json_as_its_input(monkeypatch, capsys):
    """A target segment names a comparison side; it is not an audit input itself."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO('{"audit":"/tmp/audit.json"}'))
    monkeypatch.setitem(handlers.HANDLERS, "segment_diff", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(["segment-diff", "--source", "en", "--target", "fr"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["echo"] == {
        "audit": "/tmp/audit.json",
        "source": "en",
        "target": "fr",
    }


def test_explicit_input_still_wins_over_stdin_with_segment_names(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO('{"audit":"/tmp/stdin.json"}'))
    monkeypatch.setitem(handlers.HANDLERS, "segment_diff", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(
        [
            "segment-diff",
            "--input",
            '{"audit":"/tmp/explicit.json"}',
            "--source",
            "en",
            "--target",
            "fr",
        ]
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["echo"]["audit"] == "/tmp/explicit.json"


def test_images_optimize_output_dir_maps_to_settings(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", _NeverReadStdin())
    monkeypatch.setitem(handlers.HANDLERS, "images_optimize", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(["images-optimize", "--files", "a.png,b.png", "--output-dir", "/tmp/out"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["echo"]["files"] == ["a.png", "b.png"]
    assert out["echo"]["settings"]["out_dir"] == "/tmp/out"


def test_duplicate_check_fingerprints_flag(monkeypatch, capsys):
    payload = {"items": [{"id": "a", "text": "x " * 50}]}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setitem(handlers.HANDLERS, "duplicate_check", lambda **kw: {"ok": True, "echo": kw})
    rc = cli.main(["duplicate-check", "--fingerprints"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["echo"]["with_fingerprints"] is True


# Issue #156: these flags each identify a command's whole input just as --url does, but were
# missing from source-flag registration, so a per-line loop over any one of them silently stopped after its
# first iteration (the exact failure the comment above SOURCE_FLAGS warns about).
FORMERLY_MISSING_SOURCE_FLAGS = [
    ("keywords-expand", ["--phrase", "floor heating"]),
    ("keywords-seasonality", ["--phrase", "floor heating"]),
    ("keywords-exact", ["--keywords", "floor heating"]),
    ("serp-fetch", ["--query", "floor heating"]),
    ("serp-fetch", ["--queries", "floor heating,floor screed"]),
    ("google-keywords", ["--keywords", "floor heating"]),
    ("google-keywords", ["--seed", "floor heating"]),
    ("google-serp", ["--query", "floor heating"]),
    ("metrika-setup", ["--counter", "12345678"]),
    ("metrika-report", ["--counter", "12345678"]),
    ("compare-crawls", ["--before", "before.json", "--after", "after.json"]),
]


@pytest.mark.parametrize("command,flag_args", FORMERLY_MISSING_SOURCE_FLAGS)
def test_formerly_missing_source_flags_are_recognized(command, flag_args):
    """``_has_source_flag`` must return True from the flag alone — derived from its parser
    (``_source_flag``), not from a hand-kept list that can silently omit a new flag again."""
    args = cli.build_parser().parse_args([command, *flag_args])
    assert cli._has_source_flag(args)


def test_source_flag_tracking_is_scoped_to_its_command():
    segment_args = cli.build_parser().parse_args(
        ["segment-diff", "--source", "en", "--target", "fr"]
    )
    backlinks_args = cli.build_parser().parse_args(
        ["backlinks-check", "--target", "https://example.com/"]
    )
    assert not cli._has_source_flag(segment_args)
    assert cli._has_source_flag(backlinks_args)


def test_a_loop_over_a_formerly_missing_flag_runs_every_line(monkeypatch, capsys):
    """Reproduces the exact bug report: ``while read p; do seohead keywords-expand --phrase
    "$p"; done < phrases.txt`` used to process only the first of three lines because
    ``--phrase`` was absent from SOURCE_FLAGS and the CLI blocked on/consumed stdin."""
    monkeypatch.setitem(handlers.HANDLERS, "keywords_expand", lambda **kw: {"ok": True, "echo": kw})
    phrases = ["floor heating", "underfloor heating", "floor screed"]
    seen = []
    for phrase in phrases:
        # A fresh guard per line stands in for the shared, still-open file descriptor a real
        # shell loop reads from: the CLI must not touch it once --phrase already supplies input.
        monkeypatch.setattr(cli.sys, "stdin", _NeverReadStdin("leftover\n" * 100))
        rc = cli.main(["keywords-expand", "--phrase", phrase])
        assert rc == 0
        seen.append(json.loads(capsys.readouterr().out)["echo"]["phrase"])
    assert seen == phrases
