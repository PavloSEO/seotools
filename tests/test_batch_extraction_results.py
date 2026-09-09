"""Bounded extraction must not turn omitted candidates into a negative assertion."""

import pytest

from seohead.tools.extraction_rules import evaluate, validate_rules
from seohead.tools.parser import parse_html


def rule(kind="text", **kwargs):
    return {
        "id": "sample",
        "kind": kind,
        "selector": "p",
        "operator": "equals",
        "value": "2",
        "max_matches": 1,
        **kwargs,
    }


def test_count_and_presence_have_typed_assertions():
    html = "<p>first</p><p>second</p>"
    parsed = parse_html(html, "https://example.test/")
    count = evaluate(html=html, parsed=parsed, rules=[rule("count")], representation="static")[
        "rules"
    ][0]
    presence = evaluate(
        html=html, parsed=parsed, rules=[rule("presence", value="true")], representation="static"
    )["rules"][0]
    assert count["value"] == 2 and count["matched"] is True
    assert presence["value"] is True and presence["matched"] is True


def test_unseen_candidate_is_unavailable_not_false():
    html = "<p>first</p><p>second</p>"
    result = evaluate(
        html=html,
        parsed=parse_html(html, "https://example.test/"),
        rules=[rule(value="second")],
        representation="static",
    )
    assert result["state"] == "unavailable"
    assert "matched" not in result["rules"][0]


def test_invalid_css_is_rejected_before_collection():
    with pytest.raises(ValueError, match="CSS selector"):
        validate_rules([rule(selector="p[")])
