"""Google-specific skill guidance must remain sourced and precisely scoped (#704)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAQ_DEPRECATION = "https://developers.google.com/search/updates#faq-deprecation"
HOWTO_CHANGES = "https://developers.google.com/search/blog/2023/08/howto-faq-changes"
STRUCTURED_DATA_POLICIES = (
    "https://developers.google.com/search/docs/appearance/structured-data/sd-policies"
)
ROBOTS_SPEC = "https://developers.google.com/crawling/docs/robots-txt/robots-txt-spec"
JAVASCRIPT_SEO_BASICS = (
    "https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics"
)
NOINDEX_GUIDANCE = "https://developers.google.com/search/docs/crawling-indexing/block-indexing"


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    following = re.search(rf"^#{{1,{level}}} ", text[start + len(heading) :], re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return text[start:end]


def test_structured_data_guidance_cites_current_faq_and_historical_howto_sources():
    skill = _text("seohead/skills/seo-markup/SKILL.md")
    status = _section(skill, "### Google rich-result status")
    types = _section(skill, "### Choosing a Schema Type")
    faq = _section(skill, "#### FAQPage")
    howto = _section(skill, "#### HowTo")
    product = _section(skill, "#### Product")
    rules = _section(skill, "### General Schema Rules")
    errors = _section(skill, "### Common Errors")

    assert FAQ_DEPRECATION in status
    assert HOWTO_CHANGES in status
    assert STRUCTURED_DATA_POLICIES in status
    assert (
        "https://developers.google.com/search/docs/appearance/structured-data/search-gallery"
        in types
    )
    assert FAQ_DEPRECATION in faq
    assert HOWTO_CHANGES in howto
    assert (
        "https://developers.google.com/search/docs/appearance/structured-data/review-snippet"
        in product
    )
    assert STRUCTURED_DATA_POLICIES in rules
    assert STRUCTURED_DATA_POLICIES in errors
    assert "No Google rich result since 7 May 2026" in skill
    assert "Google retired the FAQ rich result on 7 May 2026" in skill
    assert "Google retired both" not in skill
    assert "HowTo rich results no longer appear" in skill
    assert "may make the page ineligible for a rich result" in errors.lower()
    assert "may lead to a structured data manual action" in errors.lower()


def test_googlebot_claims_in_workflow_skills_cite_primary_guidance():
    robots = _text(".claude/skills/robots-audit/SKILL.md")
    tech = _text(".claude/skills/tech-audit/SKILL.md")

    assert ROBOTS_SPEC in _section(robots, "## Preconditions")
    workflow = _section(robots, "## Workflow")
    assert ROBOTS_SPEC in workflow
    assert JAVASCRIPT_SEO_BASICS in workflow
    assert NOINDEX_GUIDANCE in _section(robots, "## What to deliver to the user")
    assert JAVASCRIPT_SEO_BASICS in _section(tech, "## Anti-trigger")
