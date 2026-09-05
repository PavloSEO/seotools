"""REDIRECT_CHAIN / REDIRECT_LOOP — a present-but-malformed native export.

Issue #350: when export discovery finds a nonempty ``redirect_chains.csv``
whose headers have neither ``Address`` nor ``URL``, the address-column guard
recorded a named skip for REDIRECT_CHAIN only. REDIRECT_LOOP shares the same
unavailable per-row evidence -- neither verdict can be computed from that
report -- so it silently read as clean instead of also being named as
unavailable.
"""

from __future__ import annotations

import csv
import os

from seohead.sf.core.audit import run_audit

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def _write(tmp_path, redirect_chains_rows):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(
            [
                ["Address", "Content Type", "Status Code", "Indexability"],
                ["https://example.com/page", "text/html", 200, "Indexable"],
            ]
        )
    with open(d / "redirect_chains.csv", "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(redirect_chains_rows)
    return str(d)


def test_malformed_redirect_chains_export_skips_both_ids_not_just_one(tmp_path):
    exports_dir = _write(tmp_path, [["Unrelated column"], ["value"]])
    result = run_audit(
        input_mode="parse-exports", exports_dir=exports_dir, log=lambda m: None
    ).to_json()
    skipped = {entry["id"]: entry["reason"] for entry in result["run"]["checks_skipped"]}
    silent = set(result["summary"]["check_coverage"]["checks_silent_ids"])
    fired = {i["check"] for i in result["issues"]}

    assert skipped.get("REDIRECT_CHAIN")
    assert skipped.get("REDIRECT_LOOP")
    assert not ({"REDIRECT_CHAIN", "REDIRECT_LOOP"} & silent)
    assert not ({"REDIRECT_CHAIN", "REDIRECT_LOOP"} & fired)


def test_a_valid_native_redirect_chains_export_still_fires_normally(tmp_path):
    # positive control: a well-formed native report must be unaffected by
    # the malformed-header guard added above.
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(
            [
                ["Address", "Content Type", "Status Code", "Indexability"],
                ["https://example.com/a", "text/html", 301, "Non-Indexable"],
                ["https://example.com/b", "text/html", 301, "Non-Indexable"],
                ["https://example.com/c", "text/html", 200, "Indexable"],
            ]
        )
    with open(d / "redirect_chains.csv", "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(
            [
                ["Address", "Number of Redirects", "Final Address", "Loop"],
                ["https://example.com/a", 2, "https://example.com/c", "FALSE"],
            ]
        )
    result = run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None).to_json()
    chain_urls = {i["target_url"] for i in result["issues"] if i["check"] == "REDIRECT_CHAIN"}
    skipped = {entry["id"] for entry in result["run"]["checks_skipped"]}
    assert chain_urls == {"https://example.com/a"}
    assert "REDIRECT_CHAIN" not in skipped
    assert "REDIRECT_LOOP" not in skipped
