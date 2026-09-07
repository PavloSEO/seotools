"""Issues #218 and #219: the documented ``--input`` JSON contract must actually reach every
handler, and an explicit CLI flag must actually override it — even when the flag's valid value
is falsy (0, 0.0).

#218: ``log-scan`` and ``compare-crawls`` declared their identifying flags with
``required=True``, so argparse rejected a JSON-only ``--input`` call before ``_build_kwargs``
ever ran. The handlers already validate their own required arguments (raising ``ValueError``,
caught by ``main()`` as an ordinary exit-1 failure), so the parser-level requirement was both
redundant and the thing actually breaking the documented contract.

#219: several commands only copied a numeric flag into the handler kwargs when the flag was
truthy (``if args.limit:``), which drops an explicitly passed ``0`` — indistinguishable from the
flag never having been given. This silently keeps whatever ``--input`` supplied (or the
handler's default) instead of the value the caller asked for, with no error and no warning.
"""

from __future__ import annotations

import json

import pytest

from seohead import cli
from seohead.servers import handlers


def _capture(monkeypatch, handler_name):
    captured: dict[str, object] = {}
    monkeypatch.setitem(
        handlers.HANDLERS, handler_name, lambda **kw: captured.update(kw) or {"ok": True}
    )
    return captured


# --- #218: JSON-only input must dispatch, not be rejected by argparse -------------------------


def test_log_scan_json_only_input_dispatches(monkeypatch, capsys):
    captured = _capture(monkeypatch, "log_scan")
    rc = cli.main(["log-scan", "--input", json.dumps({"run": "synthetic-run"})])
    assert rc == 0
    assert captured["run"] == "synthetic-run"


def test_compare_crawls_json_only_input_dispatches(monkeypatch, capsys):
    captured = _capture(monkeypatch, "compare_crawls")
    payload = {"before": {"pages": []}, "after": {"pages": []}}
    rc = cli.main(["compare-crawls", "--input", json.dumps(payload)])
    assert rc == 0
    assert captured["before"] == {"pages": []}
    assert captured["after"] == {"pages": []}


def test_compare_crawls_force_flag_reaches_the_handler(monkeypatch, capsys):
    captured = _capture(monkeypatch, "compare_crawls")
    rc = cli.main(["compare-crawls", "--before", "before.json", "--after", "after.json", "--force"])
    assert rc == 0
    assert captured["force"] is True


def test_log_scan_flag_still_works_and_overrides_json(monkeypatch, capsys):
    """Path flags remain supported and take precedence over --input, per the issue's acceptance
    criteria — the flag path must not have regressed while fixing the JSON-only path."""
    captured = _capture(monkeypatch, "log_scan")
    rc = cli.main(["log-scan", "--input", json.dumps({"run": "from-json"}), "--run", "from-flag"])
    assert rc == 0
    assert captured["run"] == "from-flag"


def test_log_scan_missing_run_everywhere_is_an_ordinary_failure_not_a_parser_exit():
    """Without ``required=True``, a call missing ``run`` from both --input and flags must still
    fail clearly — via the handler's own ValueError (exit 1) — not silently succeed."""
    rc = cli.main(["log-scan"])
    assert rc == 1


# --- #219: an explicit falsy numeric flag must override --input, not be ignored ----------------

# (command, base_json, cli_flag_args, kwarg_name, expected_value)
FALSY_NUMERIC_OVERRIDE_CASES = [
    (
        "duplicate-check",
        "duplicate_check",
        {"items": [{"id": "one", "text": "one"}], "threshold": 0.9},
        ["--threshold", "0"],
        "threshold",
        0.0,
    ),
    (
        "sitemap-crawl",
        "sitemap_crawl",
        {"url": "https://example.com/sitemap.xml", "concurrency": 5},
        ["--concurrency", "0"],
        "concurrency",
        0,
    ),
    (
        "site-audit",
        "site_audit",
        {"url": "https://example.com/", "limit": 25},
        ["--limit", "0"],
        "limit",
        0,
    ),
    (
        "site-audit",
        "site_audit",
        {"url": "https://example.com/", "concurrency": 5},
        ["--concurrency", "0"],
        "concurrency",
        0,
    ),
    (
        "regions-check",
        "regions_check",
        {"url": "https://example.com/", "limit": 12},
        ["--limit", "0"],
        "limit",
        0,
    ),
    (
        "backlinks-check",
        "backlinks_check",
        {"target": "https://example.com/", "donors": ["https://donor.example/"], "concurrency": 3},
        ["--concurrency", "0"],
        "concurrency",
        0,
    ),
    (
        "keywords-expand",
        "keywords_expand",
        {"phrase": "floor heating", "limit": 300},
        ["--limit", "0"],
        "limit",
        0,
    ),
    (
        "keywords-exact",
        "keywords_exact",
        {"keywords": ["floor heating"], "region": 225},
        ["--region", "0"],
        "region",
        0,
    ),
    (
        "serp-fetch",
        "serp_fetch",
        {"query": "floor heating", "top": 10},
        ["--top", "0"],
        "top",
        0,
    ),
    (
        "google-keywords",
        "google_keywords",
        {"keywords": ["floor heating"], "location_code": 2840},
        ["--location-code", "0"],
        "location_code",
        0,
    ),
    (
        "google-keywords",
        "google_keywords",
        {"keywords": ["floor heating"], "limit": 100},
        ["--limit", "0"],
        "limit",
        0,
    ),
    (
        "google-serp",
        "google_serp",
        {"query": "floor heating", "location_code": 2840},
        ["--location-code", "0"],
        "location_code",
        0,
    ),
    (
        "google-serp",
        "google_serp",
        {"query": "floor heating", "depth": 10},
        ["--depth", "0"],
        "depth",
        0,
    ),
    (
        "metrika-report",
        "metrika_report",
        {"counter_id": 12345678, "limit": 100},
        ["--limit", "0"],
        "limit",
        0,
    ),
]


@pytest.mark.parametrize(
    "command,handler_name,base_json,flag_args,kwarg_name,expected",
    FALSY_NUMERIC_OVERRIDE_CASES,
    ids=[f"{c}:{f[0]}" for c, _, _, f, _, _ in FALSY_NUMERIC_OVERRIDE_CASES],
)
def test_explicit_zero_flag_overrides_input_json(
    monkeypatch, capsys, command, handler_name, base_json, flag_args, kwarg_name, expected
):
    captured = _capture(monkeypatch, handler_name)
    rc = cli.main([command, "--input", json.dumps(base_json), *flag_args])
    assert rc == 0
    assert captured[kwarg_name] == expected
