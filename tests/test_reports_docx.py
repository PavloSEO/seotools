"""Issue #363: the client DOCX must not assert current ranking loss.

The ``seohead.site-audit/1`` contract (examples/reports/README.md) carries no
ranking measurement anywhere — severity is assigned by the aggregator rules,
not by any tool that measured search performance. The DOCX writer used to
print a categorical "these issues are preventing the site from ranking
correctly now" sentence for every critical finding regardless of that, which
is a claim the input never supports.
"""

from __future__ import annotations

import json
from pathlib import Path

from docx import Document

from seohead.reports import build_report

FIXTURE = Path(__file__).parent.parent / "examples" / "reports" / "full.json"


def _docx_text(tmp_path) -> str:
    audit = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert "ranking" not in json.dumps(audit).lower()  # sanity: no ranking evidence in the input

    output = tmp_path / "audit.docx"
    result = build_report(audit, fmt="docx", path=str(output))
    assert result["ok"], result
    return "\n".join(p.text for p in Document(output).paragraphs)


def test_critical_section_does_not_claim_current_ranking_loss(tmp_path):
    text = _docx_text(tmp_path)
    assert "preventing the site from ranking correctly now" not in text.lower()
    # The replacement prose may name "rankings" only to disclaim measuring
    # them — it must never assert the site has already lost any.
    assert "already cost the site" in text.lower() or "does not measure current" in text.lower()
    assert "these issues have already cost the site any" in text.lower()


def test_critical_section_still_flags_severity_without_overclaiming(tmp_path):
    """Positive control: critical findings must still be called out — the fix
    must not silently drop the warning, only its unsupported claim."""
    text = _docx_text(tmp_path)
    assert "highest-severity issues" in text.lower()


def test_severity_note_stays_consistent_with_the_critical_section(tmp_path):
    """The document's own severity_note says severity is rule-assigned, not
    measured by a tool; the critical-section prose must not contradict that
    by asserting a measured outcome (ranking loss) it never established."""
    audit = json.loads(FIXTURE.read_text(encoding="utf-8"))
    note = audit["summary"]["severity_note"]
    assert "aggregator rules" in note

    text = _docx_text(tmp_path)
    assert "aggregator" in text.lower()


def test_word_still_groups_findings_by_severity(tmp_path):
    """Acceptance criterion: DOCX still groups findings by severity and keeps
    its narrative structure — the fix is scoped to one sentence's wording."""
    text = _docx_text(tmp_path)
    assert "Critical" in text
    assert "Warning" in text
    assert "Notice" in text
