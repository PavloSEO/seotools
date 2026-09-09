"""Google-backed guidance keeps conditional canonical and translation semantics."""

from __future__ import annotations

from pathlib import Path

from seohead.sf.core.registry import CHECKS

ROOT = Path(__file__).resolve().parent.parent


def test_js_only_canonical_guidance_is_not_unconditionally_critical():
    skill = (ROOT / ".claude" / "skills" / "js-render-check" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert '| "canonical is injected by a script" | critical |' not in skill
    assert "when source HTML has no canonical and JavaScript adds one" in skill
    assert "do not treat injection alone as critical" in skill
    assert (
        "https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls"
        in skill
    )


def test_rendering_guidance_keeps_js_links_and_an_all_clear_snapshot_conditional():
    skill = (ROOT / ".claude" / "skills" / "js-render-check" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert '| "links appear only after JS" | critical | site traversal breaks' not in skill
    assert "JS insertion alone does not prove Google cannot discover them" in skill
    assert '| "rendering changes nothing" | okay | SSR works;' not in skill
    assert "does not prove SSR or whole-site health" in skill
    assert (
        "https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics"
        in skill
    )
    assert "specialist triage heuristics, not Google\nindexing requirements" in skill


def test_notranslate_remains_notice_and_names_search_feature_opt_out():
    check = CHECKS["NOTRANSLATE"]
    scenario = (ROOT / "docs" / "scenarios" / "robots-directives.md").read_text(encoding="utf-8")

    assert check["severity"] == "notice"
    assert check["fix"] == (
        "Confirm that opting out of translation-related Google Search features is intentional."
    )
    assert "translation-related Google Search features" in scenario
    assert "https://developers.google.com/search/docs/appearance/translated-results" in scenario


def test_encoded_space_guidance_keeps_the_warning_but_excludes_mechanical_deletion():
    check = CHECKS["URL_CONTAINS_SPACE"]
    scenario = (ROOT / "docs" / "scenarios" / "url-hygiene.md").read_text(encoding="utf-8")

    assert check["severity"] == "warning"
    assert "q=red%20shoes" in check["fix"]
    assert "do not mechanically remove percent-encoded values" in check["fix"]
    assert "URL-hygiene heuristic" in scenario
    assert "Google cannot index the URL" in scenario
