"""H2_DUPLICATE and H2_TOO_LONG (#385): the H2-1 column is already read by both an SF
export and a native crawl, so both checks work directly off ``Internal:All`` -- no new
extraction needed, unlike the other rows from #385/#386.
"""

from __future__ import annotations

import csv

from seohead.sf.core.audit import run_audit

_COLS = ["Address", "Content Type", "Status Code", "Indexability", "H2-1"]


def _audit_with(tmp_path, rows):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_COLS)
        for row in rows:
            writer.writerow(row)
    return run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)


def _issues(res, check):
    return [i for i in res.issues if i.check == check]


def test_h2_duplicate_fires_on_two_pages_sharing_an_h2(tmp_path):
    shared = "https://example.com/a", "https://example.com/b"
    res = _audit_with(
        tmp_path,
        [
            [shared[0], "text/html", "200", "Indexable", "Specifications"],
            [shared[1], "text/html", "200", "Indexable", "Specifications"],
            ["https://example.com/c", "text/html", "200", "Indexable", "A different heading"],
        ],
    )
    issues = _issues(res, "H2_DUPLICATE")
    assert {i.target_url for i in issues} == set(shared)
    gids = {i.group_id for i in issues}
    assert len(gids) == 1


def test_h2_duplicate_negative_control_distinct_headings(tmp_path):
    """Positive control above pairs with this: two pages, two different H2s, no finding."""
    res = _audit_with(
        tmp_path,
        [
            ["https://example.com/a", "text/html", "200", "Indexable", "First heading"],
            ["https://example.com/b", "text/html", "200", "Indexable", "Second heading"],
        ],
    )
    assert _issues(res, "H2_DUPLICATE") == []


def test_h2_too_long_fires_past_seventy_characters(tmp_path):
    long_h2 = "A subheading that runs on for far longer than seventy characters should ever need"
    assert len(long_h2) > 70
    res = _audit_with(
        tmp_path, [["https://example.com/long", "text/html", "200", "Indexable", long_h2]]
    )
    issues = _issues(res, "H2_TOO_LONG")
    assert len(issues) == 1
    assert issues[0].target_url == "https://example.com/long"
    assert issues[0].details["length"] == len(long_h2)


def test_h2_too_long_stays_silent_under_the_threshold(tmp_path):
    """Negative control paired with the positive case above: a normal-length H2 is silent."""
    res = _audit_with(
        tmp_path, [["https://example.com/short", "text/html", "200", "Indexable", "Specifications"]]
    )
    assert _issues(res, "H2_TOO_LONG") == []


def test_h2_checks_are_silent_not_erroring_when_h2_column_is_absent(tmp_path):
    """No H2-1 column at all: neither check invents a finding out of a column the run
    never had (mirrors the #205 convention every other length/duplicate check follows)."""
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Address", "Content Type", "Status Code", "Indexability"])
        writer.writerow(["https://example.com/x", "text/html", "200", "Indexable"])
    res = run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)
    assert _issues(res, "H2_DUPLICATE") == []
    assert _issues(res, "H2_TOO_LONG") == []
