"""The committed examples/ audit must match what the documented command produces.

Nothing else kept examples/audit.json, audit.md, tasks.json and tasks.md in sync with the
audit contract (#488): every other doc gate in this repository derives its expectation from
the code, but the shipped example was hand-committed and drifted silently, going out with the
three summary fields that qualify a health score missing.

This gate regenerates the example from examples/exports with the exact command README.md
documents and fails if the committed copy differs in anything but volatile values
(generated_at, absolute paths).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from seohead.sf.cli import main as sf_main

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"

# Matches an ISO-8601 UTC timestamp like the one audit.json/tasks.json stamp into
# generated_at and the human-readable date line in the .md reports.
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _run_fresh(out_dir: Path) -> None:
    rc = sf_main(
        [
            "run",
            "--exports-dir",
            "examples/exports",
            "--out",
            str(out_dir),
            "--tasks",
        ]
    )
    assert rc == 0, f"seohead sf run exited {rc}"


def _normalize_json(data: dict) -> dict:
    """Strip fields whose value is expected to vary run-to-run, not structure.

    Recurses because tasks.json nests a copy of the audit's `run`/`generated_at`
    under `source`, alongside the top-level ones audit.json carries directly.
    """
    if isinstance(data, dict):
        return {
            key: ("<normalized>" if key == "generated_at" else _normalize_json(value))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_normalize_json(item) for item in data]
    return data


def _normalize_text(text: str) -> str:
    """Strip volatile substrings (timestamps, absolute paths) from a rendered .md file."""
    text = _TIMESTAMP_RE.sub("<normalized>", text)
    # Any absolute filesystem path (starts with "/") becomes a placeholder so a run
    # under a different checkout or tmp directory can't fail this gate on path alone.
    text = re.sub(r"/[^\s`\"']*", lambda m: "<path>" if "/" in m.group(0)[1:] else m.group(0), text)
    return text


def test_examples_match_a_fresh_run(tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    out_dir = tmp_path / "fresh"
    _run_fresh(out_dir)

    for name in ("audit.json", "tasks.json"):
        committed = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
        fresh = json.loads((out_dir / name).read_text(encoding="utf-8"))
        assert _normalize_json(committed) == _normalize_json(fresh), (
            f"examples/{name} is stale: regenerate it with "
            "`seohead sf run --exports-dir examples/exports --out examples --tasks`"
        )

    for name in ("audit.md", "tasks.md"):
        committed = _normalize_text((EXAMPLES / name).read_text(encoding="utf-8"))
        fresh = _normalize_text((out_dir / name).read_text(encoding="utf-8"))
        assert committed == fresh, (
            f"examples/{name} is stale: regenerate it with "
            "`seohead sf run --exports-dir examples/exports --out examples --tasks`"
        )


def test_negative_control_generated_at_and_absolute_path_alone_do_not_fail(tmp_path, monkeypatch):
    """A run whose only differences are a fresh timestamp and a different absolute output
    path must still pass — proving the gate tests drift, not volatility."""
    monkeypatch.chdir(ROOT)
    out_dir = tmp_path / "fresh"
    _run_fresh(out_dir)

    committed_audit = json.loads((EXAMPLES / "audit.json").read_text(encoding="utf-8"))
    fresh_audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    # Sanity: the raw generated_at values genuinely differ (this run just happened).
    assert committed_audit["run"]["generated_at"] != fresh_audit["run"]["generated_at"]
    assert _normalize_json(committed_audit) == _normalize_json(fresh_audit)

    committed_md = (EXAMPLES / "audit.md").read_text(encoding="utf-8")
    fresh_md = (out_dir / "audit.md").read_text(encoding="utf-8")
    # Different absolute tmp_path per test run stands in for "a different absolute
    # output path" varying between two runs of the same command.
    assert str(out_dir) not in committed_md or str(out_dir) != str(EXAMPLES)
    assert _normalize_text(committed_md) == _normalize_text(fresh_md)
