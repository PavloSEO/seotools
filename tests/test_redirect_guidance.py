"""The shipped audit method must not infer signal-loss percentages from status codes."""

from __future__ import annotations

import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "seohead/skills/seo-audit-page/SKILL.md"


def test_redirect_guidance_preserves_intent_without_inventing_signal_loss():
    text = SKILL.read_text(encoding="utf-8")
    permanent = re.search(r"^\| 301 \|.*$", text, re.MULTILINE)
    temporary = re.search(r"^\| 302 \|.*$", text, re.MULTILINE)
    assert permanent and temporary
    assert not re.search(r"\d+(?:\.\d+)?\s*%", permanent.group())
    assert "does not pass link equity" not in temporary.group().lower()
    assert "canonical" in permanent.group().lower()
    assert "temporary" in temporary.group().lower()
    assert "https://developers.google.com/search/docs/crawling-indexing/301-redirects" in text
    assert "https://developers.google.com/search/docs/crawling-indexing/website-testing" in text
