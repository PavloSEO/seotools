"""The Google guidance review record stays auditable, dated and unexaggerated.

The record is evidence, not prose: it is the only thing in the repository that
says which Google guides were read, when, and what changed because of them. Its
first assembly silently dropped read dates from five rows and labelled rules
from nine, and left a repair marked pending after it had merged -- none of which
any gate would have noticed. These assertions are those three failures.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORD = ROOT / "docs" / "google-search-review.json"
PAGE = ROOT / "docs" / "GOOGLE_GUIDANCE_REVIEW.md"

LABELS = {"requirement", "eligibility", "recommendation", "heuristic", "explanation"}


def _record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def test_every_row_is_read_and_dated():
    rows = _record()["rows"]

    undated = [row["canonical_url"] for row in rows if not row["read_dates"]]
    unread = [row["canonical_url"] for row in rows if not row["actual_read"]]

    assert not unread, f"rows claim a review without being read: {unread}"
    assert not undated, f"rows are read but carry no read date: {undated}"


def test_every_row_labels_its_guidance():
    rows = _record()["rows"]

    unlabelled = [row["canonical_url"] for row in rows if not row["key_applicable_rules"]]
    wrong = sorted(
        {
            rule["label"]
            for row in rows
            for rule in row["key_applicable_rules"]
            if rule["label"] not in LABELS
        }
    )

    assert not unlabelled, f"rows carry no requirement/heuristic labels: {unlabelled}"
    assert not wrong, f"rules use labels outside the record's vocabulary: {wrong}"


def test_backfilled_rules_say_where_they_came_from():
    """A rule copied from an itemized list and one labelled from prose are not the same
    evidence, and a reader has to be able to tell them apart."""
    rows = _record()["rows"]

    derived = {
        rule["derived_from"]
        for row in rows
        for rule in row["key_applicable_rules"]
        if "derived_from" in rule
    }

    assert derived <= {"source review (itemized)", "source review (narrative summary)"}


def test_no_repair_is_left_pending():
    record = _record()
    states = {
        issue.get("state") for row in record["rows"] for issue in row["disposition"]["issues"]
    }
    states |= {issue.get("state") for issue in record["encoded_space_candidate"]["issues"]}

    assert "pending" not in states, "a repair merged into main is still recorded as pending"
    assert not record["unexplained_rows"]


def test_coverage_counters_match_the_rows():
    record = _record()
    coverage = record["coverage"]

    assert len(record["rows"]) == coverage["matrix_rows"] == coverage["expected_total"]
    assert coverage["actual_read_rows"] == len(record["rows"])
    assert not coverage["unexplained_or_unread_rows"]


def test_the_record_dates_itself_and_claims_no_certification():
    record = _record()
    # Unwrapped, because the disclaimer is a wrapped Markdown blockquote: a
    # line-scoped search would miss the sentence purely on where it wrapped, and
    # the ">" markers sit mid-sentence once the lines are joined.
    lines = (line.lstrip("> ") for line in PAGE.read_text(encoding="utf-8").splitlines())
    page = " ".join(" ".join(lines).split())

    assert record["snapshot_date"] == "2026-09-09"
    assert record["source_root"].startswith("https://developers.google.com/search/docs")
    for text in (record["statement"], page):
        assert "not a Google certification" in text
        assert "no ranking or indexing guarantee" in text
    assert record["snapshot_date"] in page
