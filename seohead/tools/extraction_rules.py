"""Closed declarative extraction rules over one already captured document.

Rules deliberately have no callable, import, template or shell field.  They
can inspect bounded text, HTML attributes and JSON-LD paths and return typed
values or explicit unavailable states; they cannot execute user code.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

VERSION = "extraction_rules.v1"
MAX_RULES = 100
MAX_MATCHES = 100
MAX_VALUE_BYTES = 8192
_KINDS = {"text", "attribute", "structured", "presence", "count"}
_OPERATORS = {"equals", "contains", "matches", "exists"}


def validate_rules(value: Any) -> list[dict[str, Any]]:
    """Validate and canonicalize a bounded data-only rule list."""
    if not isinstance(value, list) or len(value) > MAX_RULES:
        raise ValueError("extraction rules must be a bounded list")
    seen, rules = set(), []
    for rule in value:
        required = {"id", "kind", "selector", "operator", "value", "max_matches"}
        if not isinstance(rule, dict) or set(rule) != required:
            raise ValueError("extraction rule has unsupported fields")
        rule_id = rule["id"]
        if type(rule_id) is not str or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", rule_id) or rule_id in seen:
            raise ValueError("extraction rule id is invalid or duplicated")
        seen.add(rule_id)
        if rule["kind"] not in _KINDS or rule["operator"] not in _OPERATORS:
            raise ValueError("extraction rule kind or operator is invalid")
        if type(rule["selector"]) is not str or len(rule["selector"]) > 512:
            raise ValueError("extraction rule selector is invalid")
        if type(rule["value"]) is not str or len(rule["value"]) > 2048:
            raise ValueError("extraction rule value is invalid")
        if type(rule["max_matches"]) is not int or not 1 <= rule["max_matches"] <= MAX_MATCHES:
            raise ValueError("extraction rule max_matches is invalid")
        if rule["kind"] in {"text", "attribute"} and not rule["selector"]:
            raise ValueError("text and attribute rules require a CSS selector")
        if rule["kind"] == "attribute" and "@" not in rule["selector"]:
            raise ValueError("attribute selector must end in @attribute")
        if rule["kind"] == "structured" and not rule["selector"].startswith("/"):
            raise ValueError("structured selector must be a JSON pointer")
        if rule["operator"] == "matches":
            try:
                re.compile(rule["value"])
            except re.error as exc:
                raise ValueError("extraction rule regex is invalid") from exc
        rules.append(dict(rule))
    return sorted(rules, key=lambda rule: rule["id"])


def _match(values: list[str], operator: str, expected: str) -> bool:
    if operator == "exists":
        return bool(values)
    if operator == "equals":
        return any(value == expected for value in values)
    if operator == "contains":
        return any(expected in value for value in values)
    return any(re.search(expected, value) is not None for value in values)


def _pointer(value: Any, pointer: str) -> list[str]:
    current = [value]
    for token in pointer.lstrip("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        next_values = []
        for item in current:
            if isinstance(item, dict) and token in item:
                next_values.append(item[token])
            elif isinstance(item, list) and token.isdigit() and int(token) < len(item):
                next_values.append(item[int(token)])
        current = next_values
    return [json.dumps(item, sort_keys=True, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item) for item in current]


def evaluate(
    *, html: str | None, parsed: dict[str, Any] | None, rules: Any, representation: str
) -> dict[str, Any]:
    """Evaluate rules against one captured representation without fetching anything."""
    selected = validate_rules(rules)
    if not isinstance(html, str) or not isinstance(parsed, dict):
        return {
            "schema_version": VERSION,
            "representation": representation,
            "state": "unavailable",
            "reason": "captured document was not parsed",
            "rules": [{"id": rule["id"], "state": "unavailable", "reason": "document unavailable"} for rule in selected],
        }
    soup = BeautifulSoup(html, features="lxml")
    results = []
    for rule in selected:
        values: list[str]
        if rule["kind"] == "structured":
            values = []
            for block in parsed.get("jsonld") or []:
                values.extend(_pointer(block, rule["selector"]))
        elif rule["kind"] == "attribute":
            selector, attribute = rule["selector"].rsplit("@", 1)
            values = [str(tag.get(attribute)) for tag in soup.select(selector) if tag.has_attr(attribute)]
        elif rule["kind"] in {"text", "presence", "count"}:
            tags = soup.select(rule["selector"]) if rule["selector"] else [soup]
            values = [" ".join(tag.get_text(" ").split()) for tag in tags]
        else:
            values = []
        values = [value for value in values if len(value.encode("utf-8")) <= MAX_VALUE_BYTES][: rule["max_matches"]]
        matched = _match(values, rule["operator"], rule["value"])
        typed: Any = len(values) if rule["kind"] == "count" else values
        results.append({"id": rule["id"], "state": "complete", "matched": matched, "value": typed, "count": len(values)})
    return {"schema_version": VERSION, "representation": representation, "state": "complete", "reason": "", "rules": results}
