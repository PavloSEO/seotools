"""Regressions for three scenario docs that promised more than their evidence supports.

Each test pins the specific sentence-level claim an issue reported as false, plus a
neighbouring claim in the same doc that must stay untouched, so a future edit that reintroduces
the false promise (or collapses the doc's other content while "fixing" it) fails loudly.
"""

from __future__ import annotations

import pathlib

import pytest

from seohead.sf.core.registry import CHECK_REQUIRES

SCENARIOS = pathlib.Path(__file__).resolve().parent.parent / "docs" / "scenarios"


def _text(name: str) -> str:
    return (SCENARIOS / name).read_text(encoding="utf-8")


# ── #301: sitemap-health must not promise a native-only run raises
# SITEMAP_URL_NON_INDEXABLE, which reads an SF comparison export the native route never
# supplies. ───────────────────────────────────────────────────────────────────────────────


def test_sitemap_non_indexable_requires_the_export_it_actually_needs():
    assert CHECK_REQUIRES["SITEMAP_URL_NON_INDEXABLE"] == ("sitemap_non_indexable",), (
        "this test's premise (the check is export-only, with no native fallback) has changed; "
        "the doc claim below needs re-checking against the new requirement, not just re-asserted"
    )


def test_sitemap_health_names_the_export_prerequisite():
    doc = _text("sitemap-health.md")
    chain = doc.split("## The chain", 1)[1].split("## What comes out", 1)[0]
    # The step that used to say "Read SITEMAP_URL_NON_INDEXABLE" unconditionally must now name
    # the SF comparison export it depends on, and the fact that a native run skips it by name.
    assert "sitemap_non_indexable" in chain
    assert "skip" in chain.lower()
    assert "sf run --exports-dir" in chain
    # The old unconditional promise: a bare "Read `SITEMAP_URL_NON_INDEXABLE`." with nothing
    # about the export must not reappear.
    assert "**6. Read `SITEMAP_URL_NON_INDEXABLE`.**" not in chain


def test_sitemap_health_cannot_answer_still_names_the_export_gap():
    doc = _text("sitemap-health.md")
    limits = doc.split("## What it cannot answer", 1)[1]
    assert "SITEMAP_URL_NON_INDEXABLE" in limits
    assert "Non-Indexable URLs In Sitemap" in limits
    assert "comparison export" in limits
    # Neighbouring, unrelated limit must survive the edit untouched.
    assert "Whether Google fetched it." in limits


def test_sitemap_health_still_covers_the_registry_category():
    # The scenario keeps declaring the issue it now reaches through the export-mode route
    # instead of dropping coverage silently.
    covers = _text("sitemap-health.md").split("## Covers", 1)[1].split("## The chain", 1)[0]
    assert "Non-Indexable URLs In Sitemap" in covers


# ── #315: the Googlebot robots scenario must configure robots.user_agent_token explicitly, and
# use the same config for both the respecting and the report-only crawl. ──────────────────────


def test_robots_blocked_names_the_user_agent_token_setting():
    doc = _text("robots-blocked.md")
    chain = doc.split("## The chain", 1)[1].split("## What comes out", 1)[0]
    assert "robots.user_agent_token" in chain
    assert "Googlebot" in chain
    assert "Save the following JSON as `./config.json`" in chain


def test_robots_blocked_respecting_and_report_only_share_one_config():
    doc = _text("robots-blocked.md")
    chain = doc.split("## The chain", 1)[1].split("## What comes out", 1)[0]
    # Both crawl-site invocations must reference the same config file, not two ad-hoc calls.
    crawl_lines = [
        line
        for line in chain.splitlines()
        if line.strip().startswith("seohead crawl-site") and "--config" in line
    ]
    assert len(crawl_lines) == 2, chain
    configs = {line.split("--config", 1)[1].split()[0] for line in crawl_lines}
    assert len(configs) == 1, f"respecting and report-only runs use different configs: {configs}"
    assert configs == {"./config.json"}
    assert "--robots report_only" in chain


def test_robots_blocked_still_says_crawlability_is_not_indexation():
    limits = _text("robots-blocked.md").split("## What it cannot answer", 1)[1]
    assert "Whether a blocked URL is indexed." in limits


# ── #318: the schema scenario must not claim disconnected JSON-LD islands explain an absent
# Google rich result; islands stay a local structural finding. ────────────────────────────────


def _normalize(text: str) -> str:
    return " ".join(text.split())


def test_schema_validation_drops_the_causal_island_claim():
    doc = _text("schema-validation.md")
    body = _normalize(doc.split("## The chain", 1)[1].split("## What it costs", 1)[0])
    # The retracted sentence asserted islands *produce* the absence of a result.
    assert "produces nothing" not in body
    assert "local structural finding" in body
    assert "does not explain whether Google will show a rich result" in body


def test_schema_validation_still_flags_islands_as_local_structural_evidence():
    doc = _text("schema-validation.md")
    body = doc.split("## The chain", 1)[1].split("## What it costs", 1)[0]
    assert "Graph shape" in body
    assert "linked_by_id: 0" in body


def test_schema_validation_cannot_answer_still_names_googles_own_decision():
    limits = _text("schema-validation.md").split("## What it cannot answer", 1)[1]
    assert "Whether Google will show a rich result." in limits


@pytest.mark.parametrize("name", ["sitemap-health.md", "robots-blocked.md", "schema-validation.md"])
def test_touched_scenarios_still_have_all_required_sections(name: str):
    doc = _text(name)
    for heading in (
        "## The question",
        "## Covers",
        "## The chain",
        "## What comes out",
        "## What it costs",
        "## What it cannot answer",
    ):
        assert heading in doc, f"{name} lost its {heading!r} section"
