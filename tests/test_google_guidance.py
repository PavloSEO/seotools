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


def test_notranslate_remains_notice_and_names_search_feature_opt_out():
    check = CHECKS["NOTRANSLATE"]

    assert check["severity"] == "notice"
    assert check["fix"] == (
        "Confirm that opting out of translation-related Google Search features is intentional."
    )
