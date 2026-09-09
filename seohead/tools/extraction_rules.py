"""Closed declarative extraction rules over one already captured document.

Rules deliberately have no callable, import, template, shell or regular
expression field. They can inspect bounded text, HTML attributes and JSON-LD
paths and return typed values or explicit unavailable states; they cannot
execute user code.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError, compile as compile_selector

VERSION = "extraction_rules.v1"
MAX_RULES = 100
MAX_MATCHES = 100
MAX_VALUE_BYTES = 8192
MAX_GLOB_PATTERN_CHARS = 256
MAX_GLOB_VALUE_CHARS = 2048
MAX_GLOB_STEPS = 16_384
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
        if (
            type(rule_id) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", rule_id)
            or rule_id in seen
        ):
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
        if rule["operator"] == "matches" and len(rule["value"]) > MAX_GLOB_PATTERN_CHARS:
            raise ValueError("extraction rule glob pattern is too long")
        if rule["kind"] != "structured" and rule["selector"]:
            selector = (
                rule["selector"].rsplit("@", 1)[0]
                if rule["kind"] == "attribute"
                else rule["selector"]
            )
            try:
                compile_selector(selector)
            except SelectorSyntaxError as exc:
                raise ValueError("extraction rule CSS selector is invalid") from exc
        rules.append(dict(rule))
    return sorted(rules, key=lambda rule: rule["id"])


def _glob_match(value: str, pattern: str) -> bool | None:
    """Match the closed ``*``/``?`` glob grammar with a deterministic step cap."""
    if len(value) > MAX_GLOB_VALUE_CHARS:
        return None
    value_index = pattern_index = 0
    star_index = retry_index = -1
    steps = 0
    while value_index < len(value):
        steps += 1
        if steps > MAX_GLOB_STEPS:
            return None
        if pattern_index < len(pattern) and (
            pattern[pattern_index] == "?" or pattern[pattern_index] == value[value_index]
        ):
            value_index += 1
            pattern_index += 1
        elif pattern_index < len(pattern) and pattern[pattern_index] == "*":
            star_index = pattern_index
            pattern_index += 1
            retry_index = value_index
        elif star_index >= 0:
            pattern_index = star_index + 1
            retry_index += 1
            value_index = retry_index
        else:
            return False
    while pattern_index < len(pattern) and pattern[pattern_index] == "*":
        pattern_index += 1
    return pattern_index == len(pattern)


def _match(values: list[str], operator: str, expected: str) -> tuple[bool | None, str]:
    if operator == "exists":
        return bool(values), ""
    if operator == "equals":
        return any(value == expected for value in values), ""
    if operator == "contains":
        return any(expected in value for value in values), ""
    for value in values:
        matched = _glob_match(value, expected)
        if matched is None:
            return None, "glob match exceeded the bounded input or step limit"
        if matched:
            return True, ""
    return False, ""


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
    return [
        json.dumps(item, sort_keys=True, ensure_ascii=False)
        if isinstance(item, (dict, list))
        else str(item)
        for item in current
    ]


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
            "rules": [
                {"id": rule["id"], "state": "unavailable", "reason": "document unavailable"}
                for rule in selected
            ],
        }
    soup = BeautifulSoup(html, features="lxml")
    results = []
    for rule in selected:
        raw_values: list[str]
        if rule["kind"] == "structured":
            raw_values = []
            for block in parsed.get("jsonld") or []:
                raw_values.extend(_pointer(block, rule["selector"]))
        elif rule["kind"] == "attribute":
            selector, attribute = rule["selector"].rsplit("@", 1)
            raw_values = [
                str(tag.get(attribute)) for tag in soup.select(selector) if tag.has_attr(attribute)
            ]
        elif rule["kind"] in {"text", "presence", "count"}:
            tags = soup.select(rule["selector"]) if rule["selector"] else [soup]
            raw_values = [" ".join(tag.get_text(" ").split()) for tag in tags]
        else:
            raw_values = []
        values = [value for value in raw_values if len(value.encode("utf-8")) <= MAX_VALUE_BYTES]
        stored_values = values[: rule["max_matches"]]
        typed: Any = (
            len(raw_values)
            if rule["kind"] == "count"
            else bool(raw_values)
            if rule["kind"] == "presence"
            else stored_values
        )
        unavailable_reason = ""
        if (
            rule["kind"] not in {"count", "presence"}
            and rule["operator"] != "exists"
            and len(values) != len(raw_values)
        ):
            unavailable_reason = "comparison has an oversized candidate value"
        elif (
            rule["kind"] not in {"count", "presence"}
            and rule["operator"] != "exists"
            and len(values) > len(stored_values)
        ):
            unavailable_reason = "comparison has more candidates than the rule cap"
        if rule["operator"] == "exists":
            matched, match_reason = bool(raw_values), ""
        else:
            match_values = (
                [str(typed).lower()] if rule["kind"] in {"count", "presence"} else stored_values
            )
            matched, match_reason = _match(match_values, rule["operator"], rule["value"])
        unavailable_reason = unavailable_reason or match_reason
        if unavailable_reason:
            results.append(
                {
                    "id": rule["id"],
                    "state": "unavailable",
                    "reason": unavailable_reason,
                    "value": typed,
                    "count": len(raw_values),
                }
            )
        else:
            results.append(
                {
                    "id": rule["id"],
                    "state": "complete",
                    "reason": "",
                    "matched": matched,
                    "value": typed,
                    "count": len(raw_values),
                }
            )
    unavailable = [row for row in results if row["state"] == "unavailable"]
    if not results:
        state = "complete"
    elif len(unavailable) == len(results):
        state = "unavailable"
    elif unavailable:
        state = "partial"
    else:
        state = "complete"
    return {
        "schema_version": VERSION,
        "representation": representation,
        "state": state,
        "reason": "some extraction rules were unavailable" if unavailable else "",
        "rules": results,
    }


def validate_result(value: Any) -> None:
    """Validate a persisted extraction result without rerunning selectors or globs."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "representation",
        "state",
        "reason",
        "rules",
    }:
        raise ValueError("extraction evidence has unsupported fields")
    if value["schema_version"] != VERSION or value["representation"] not in {
        "static",
        "rendered",
        "legacy_fragment",
    }:
        raise ValueError("extraction evidence version or representation is invalid")
    if (
        value["state"] not in {"complete", "partial", "unavailable"}
        or not isinstance(value["reason"], str)
        or not isinstance(value["rules"], list)
    ):
        raise ValueError("extraction evidence state is invalid")
    if len(value["rules"]) > MAX_RULES:
        raise ValueError("extraction evidence exceeds rule limit")
    ids = set()
    for row in value["rules"]:
        if not isinstance(row, dict) or set(row) - {
            "id",
            "state",
            "reason",
            "matched",
            "value",
            "count",
        }:
            raise ValueError("extraction evidence rule row is invalid")
        if (
            type(row.get("id")) is not str
            or row["id"] in ids
            or row.get("state") not in {"complete", "unavailable"}
        ):
            raise ValueError("extraction evidence rule identity is invalid")
        ids.add(row["id"])
        if not isinstance(row.get("reason", ""), str):
            raise ValueError("extraction evidence rule reason is invalid")
        if row["state"] == "complete" and (
            type(row.get("matched")) is not bool
            or type(row.get("count")) is not int
            or row["count"] < 0
        ):
            raise ValueError("complete extraction evidence row is invalid")

    states = [row["state"] for row in value["rules"]]
    expected = (
        "partial"
        if "complete" in states and "unavailable" in states
        else "unavailable"
        if states and all(state == "unavailable" for state in states)
        else "complete"
    )
    if states and value["state"] != expected:
        raise ValueError("extraction evidence summary disagrees with rule states")
