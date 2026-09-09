"""Unexecuted regression coverage for source-derived capability prerequisites."""

from __future__ import annotations

from seohead.sf.core.evidence_contract import capability_rows


def test_capability_prerequisites_follow_declared_sources_not_check_name_heuristics():
    rows = {row["check"]: row for row in capability_rows({"summary": {}, "run": {}})}

    response = rows["BROKEN_PAGE_4XX"]["prerequisites"]
    assert response["required_evidence"] == {
        "state": "derived",
        "items": [{"kind": "screaming_frog_export", "source": "SF:Response Codes:4xx"}],
        "reason": "",
    }
    assert response["representation"]["state"] == "unknown"
    assert any(ref["name"] == "Internal Client Error (4XX)" for ref in response["issue_map_refs"])

    sitemap = rows["SITEMAP_DESYNC"]["prerequisites"]
    assert sitemap["population"] == {
        "state": "derived",
        "value": "sitemap declarations",
        "reason": "",
    }
    assert sitemap["representation"]["state"] == "not_applicable"

    derived = rows["TITLE_MISSING"]["prerequisites"]
    assert derived["source_tag"] == "SF-derived"
    assert derived["required_evidence"]["state"] == "unknown"
    assert derived["population"]["state"] == "unknown"
    assert derived["representation"]["state"] == "unknown"
