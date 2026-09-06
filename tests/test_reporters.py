"""Reporters: JSON validates against the schema; Markdown preserves localization."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from seohead.sf.config import ConfigError
from seohead.sf.core.audit import run_audit
from seohead.sf.reporters import write_json, write_markdown
from seohead.sf.reporters.jsonfile import validate
from seohead.sf.reporters.md import _esc


def test_json_validates_against_schema(result):
    assert validate(result) == []


def test_invalid_severity_override_is_rejected_before_audit_json_is_emitted(internal_only_dir):
    """Issue #211: an out-of-enum severity must never reach a check result.

    Left unchecked, TITLE_MISSING at "urgent" both dropped its issue out of
    by_severity/the weighted penalty (inflating the health score) and made
    the emitted document fail its own bundled schema.
    """
    with pytest.raises(ConfigError, match="TITLE_MISSING"):
        run_audit(
            input_mode="parse-exports",
            exports_dir=internal_only_dir,
            config_overrides={"severity_overrides": {"TITLE_MISSING": "urgent"}},
            log=lambda _: None,
        )


def test_valid_severity_override_still_produces_a_schema_valid_report(internal_only_dir):
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=internal_only_dir,
        config_overrides={"severity_overrides": {"TITLE_MISSING": "notice"}},
        log=lambda _: None,
    )
    assert validate(res) == []


def test_markdown_frequency_table_uses_overridden_severity_not_registry_default(
    internal_only_dir, tmp_path
):
    """Issue #460: the "Most frequent issues" table must show each check's
    actual severity among the counted issues, not the registry default —
    otherwise it contradicts the severity-count table and section headers
    in the same document, which do use the overridden severity.
    """
    res = run_audit(
        input_mode="parse-exports",
        exports_dir=internal_only_dir,
        config_overrides={"severity_overrides": {"TITLE_DUPLICATE": "notice"}},
        log=lambda _: None,
    )
    path = write_markdown(res, str(tmp_path / "audit.md"))
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    freq_line = next(
        line
        for line in text.splitlines()
        if "`TITLE_DUPLICATE`" in line and "|" in line and line.count("|") == 4
    )
    assert "notice" in freq_line
    assert "warning" not in freq_line


def test_markdown_frequency_table_keeps_registry_default_without_overrides(result, tmp_path):
    """Negative control: with no severity_overrides configured, the frequency
    table must be unchanged — still the registry default for each check.
    """
    path = write_markdown(result, str(tmp_path / "audit.md"))
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    freq_line = next(
        (
            line
            for line in text.splitlines()
            if "`BROKEN_INTERNAL_LINK`" in line and line.count("|") == 4
        ),
        None,
    )
    if freq_line is not None:
        from seohead.sf.core.registry import check_meta

        expected = check_meta("BROKEN_INTERNAL_LINK")["severity"]
        assert expected in freq_line


def test_json_roundtrip_utf8(result, tmp_path):
    path = write_json(result, str(tmp_path / "audit.json"))
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    assert data["schema_version"] == "2.0"
    assert data["summary"]["by_severity"]["critical"] >= 1
    # Text from the crawl export must survive the JSON round trip unchanged.
    with open(path, encoding="utf-8") as stream:
        raw = stream.read()
    assert "Page A — Shop Pumps Online" in raw


def test_ids_are_deterministic(result):
    ids = [i.id for i in result.issues]
    assert ids == sorted(ids)  # ISSUE-000001.. assigned in sorted order
    assert all(i.fingerprint for i in result.issues)


def test_markdown_has_broken_link_table(result, tmp_path):
    path = write_markdown(result, str(tmp_path / "audit.md"))
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    assert "BROKEN_INTERNAL_LINK" in text
    assert "/html/body/footer/nav/a[2]" in text  # XPath location details are rendered.
    assert "Sitemap & robots" not in text or "Health score" in text


def test_markdown_h1_multiple_texts(result, tmp_path):
    path = write_markdown(result, str(tmp_path / "audit.md"))
    with open(path, encoding="utf-8") as stream:
        text = stream.read()
    assert "Second H1 Heading" in text


def test_md_escape_backslash_then_pipe():
    assert _esc(r"foo\bar") == r"foo\\bar"
    assert _esc("a|b") == r"a\|b"
    assert _esc(r"c\|d") == r"c\\\|d"


def test_markdown_shows_score_basis_and_coverage_for_a_partial_run(result, tmp_path):
    """Issue #353: a valid but incomplete audit must carry its qualification
    (health_score_basis) and a coverage split next to the score, not just in
    the JSON. The fixture-backed ``result`` fixture only supplies two of the
    exports the full registry can use, so this is a real partial run."""
    coverage = result.summary["check_coverage"]
    assert coverage["checks_skipped"] > 0  # sanity: this run really is partial

    path = write_markdown(result, str(tmp_path / "audit.md"))
    text = Path(path).read_text(encoding="utf-8")

    assert result.summary["health_score_basis"] in text
    assert f"{coverage['checks_fired']} fired" in text
    assert f"{coverage['checks_skipped']} skipped" in text
    assert f"{coverage['checks_silent']} silent" in text
    assert f"{coverage['checks_disabled']} disabled" in text


def test_markdown_populates_skipped_and_disabled_appendices_from_the_result(result, tmp_path):
    """Issue #353: the appendices must read AuditResult.skipped/.disabled
    directly. Before the fix, the renderer read ``run["checks_skipped"]`` /
    ``run["checks_disabled"]`` — keys that only exist on the copy built by
    AuditResult.to_json(), never on the ``run`` mapping the renderer
    actually holds — so the appendix rendered as if nothing were skipped."""
    assert result.skipped  # sanity: this run has skipped checks to show

    path = write_markdown(result, str(tmp_path / "audit.md"))
    text = Path(path).read_text(encoding="utf-8")

    assert "## Appendix: skipped checks" in text
    first_skipped = result.skipped[0]
    assert f"`{first_skipped.id}`" in text
    assert first_skipped.reason in text
    # Negative control: no disabled checks in this run, so that appendix
    # must stay absent rather than printing an empty table.
    assert "## Appendix: disabled checks" not in text


def test_markdown_disabled_appendix_is_populated_when_a_check_is_turned_off(exports_dir, tmp_path):
    from seohead.sf.config import load_config
    from seohead.sf.core.audit import run_audit

    config = load_config(None)
    config.setdefault("checks", {})["BROKEN_INTERNAL_LINK"] = {"enabled": False}
    disabled_result = run_audit(
        input_mode="parse-exports",
        exports_dir=exports_dir,
        config=config,
        log=lambda _: None,
    )
    assert disabled_result.disabled  # sanity: the switch actually disabled something

    path = write_markdown(disabled_result, str(tmp_path / "audit.md"))
    text = Path(path).read_text(encoding="utf-8")

    assert "## Appendix: disabled checks" in text
    assert "`BROKEN_INTERNAL_LINK`" in text


def test_markdown_states_a_withheld_score_with_its_reason(tmp_path, result):
    """Issue #546: the aggregator withholds the score when coverage is too low and
    records why. The reporter used to print that withheld value, so the report read
    "Health score: None / 100" -- the word None rendered as a number -- and dropped
    health_score_reason, the one sentence explaining the absence. A missing
    measurement must be stated, never disguised as one."""
    result.summary["health_score"] = None
    result.summary["health_score_reason"] = (
        "only 75 of 156 checks could run (48% coverage); too little evidence to score"
    )

    path = write_markdown(result, str(tmp_path / "audit.md"))
    text = Path(path).read_text(encoding="utf-8")

    assert "None / 100" not in text
    assert "No health score" in text
    assert "48% coverage" in text
    # The rest of the section still renders: withholding the number is not a reason
    # to withhold the evidence the reader judges the run by.
    assert "Total issues:" in text


def test_markdown_still_prints_a_score_that_was_produced(tmp_path, result):
    """The control for the test above: a run with enough coverage keeps its number."""
    result.summary["health_score"] = 42

    path = write_markdown(result, str(tmp_path / "audit.md"))
    text = Path(path).read_text(encoding="utf-8")

    assert "**Health score: 42 / 100**" in text
    assert "No health score" not in text


def test_markdown_invalid_crawl_still_has_no_numeric_score(tmp_path):
    """Issue #353's fourth acceptance criterion: an invalid crawl must keep
    leading with its failure warning, with no numeric health score, even
    after the score-basis/coverage line is added to the valid-run branch."""
    import csv

    from seohead.sf.config import load_config
    from seohead.sf.core.aggregate import aggregate
    from seohead.sf.core.context import AuditContext
    from seohead.sf.core.loader import load_exports

    path = tmp_path / "internal_all.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["Address", "Content Type", "Status Code", "Status", "Indexability"])
        writer.writerow(["https://example.com/", "", "0", "No Response", "Non-Indexable"])
    ctx = AuditContext(load_exports(str(tmp_path)), load_config(None))
    run = {"input_mode": "crawl", "generated_at": "2026-09-03T00:00:00Z", "project": "example"}
    invalid_result = aggregate(ctx, run, {}, {})

    out = tmp_path / "audit.md"
    write_markdown(invalid_result, str(out))
    md = out.read_text(encoding="utf-8")

    assert "Crawl failed" in md
    assert "Health score:" not in md
    assert "checks_fired" not in md  # the coverage line only belongs to a valid run
