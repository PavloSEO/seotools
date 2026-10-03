"""Ordered URL-pattern exclusions for audit findings.

These rules run after collection and check evaluation. They never change which
URLs the crawler requests; the caller keeps suppressed findings beside the
active view with the matching rule and its reason.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from re import Pattern
from typing import Any

_RULE_KEYS = frozenset({"id", "pattern", "checks", "reason"})
MAX_RULES = 100
MAX_RULE_ID_CHARS = 64
MAX_PATTERN_CHARS = 500
MAX_REASON_CHARS = 500
MAX_CHECKS_PER_RULE = 200


def validate_rules(
    value: Any, *, known_checks: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Validate and normalize the JSON policy, preserving its precedence order."""
    if not isinstance(value, list):
        raise ValueError("finding_exclusions must be a list")
    if len(value) > MAX_RULES:
        raise ValueError(f"finding_exclusions may contain at most {MAX_RULES} rules")
    allowed = set(known_checks) if known_checks is not None else None
    rules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(value):
        where = f"finding_exclusions[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} must be an object")
        extra = set(raw) - _RULE_KEYS
        if extra:
            raise ValueError(f"{where} has unknown keys {sorted(extra)}")
        rule_id = raw.get("id")
        pattern = raw.get("pattern")
        reason = raw.get("reason")
        checks = raw.get("checks", [])
        if not isinstance(rule_id, str) or not rule_id.strip():
            raise ValueError(f"{where}.id must be a non-empty string")
        rule_id = rule_id.strip()
        if len(rule_id) > MAX_RULE_ID_CHARS:
            raise ValueError(f"{where}.id must be at most {MAX_RULE_ID_CHARS} characters")
        if rule_id in seen_ids:
            raise ValueError(f"{where}.id duplicates {rule_id!r}")
        seen_ids.add(rule_id)
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{where}.pattern must be a non-empty regex string")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(f"{where}.pattern must be at most {MAX_PATTERN_CHARS} characters")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{where}.pattern {pattern!r} is invalid: {exc}") from exc
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{where}.reason must be a non-empty string")
        reason = reason.strip()
        if len(reason) > MAX_REASON_CHARS:
            raise ValueError(f"{where}.reason must be at most {MAX_REASON_CHARS} characters")
        if not isinstance(checks, list) or any(
            not isinstance(check_id, str) or not check_id for check_id in checks
        ):
            raise ValueError(f"{where}.checks must be a list of non-empty check IDs")
        if len(checks) > MAX_CHECKS_PER_RULE:
            raise ValueError(f"{where}.checks may contain at most {MAX_CHECKS_PER_RULE} IDs")
        if len(checks) != len(set(checks)):
            raise ValueError(f"{where}.checks contains duplicate check IDs")
        if allowed is not None:
            unknown = sorted(set(checks) - allowed)
            if unknown:
                raise ValueError(f"{where}.checks names unknown checks {unknown}")
        rules.append(
            {
                "id": rule_id,
                "pattern": pattern,
                "checks": list(checks),
                "reason": reason,
            }
        )
    return rules


def compile_rules(rules: list[dict[str, Any]]) -> list[tuple[dict[str, Any], Pattern[str]]]:
    """Compile an already-validated policy once per audit."""
    return [(rule, re.compile(rule["pattern"])) for rule in rules]


def matching_rule(
    check_id: str,
    target_url: str | None,
    rules: list[tuple[dict[str, Any], Pattern[str]]],
) -> dict[str, Any] | None:
    """Return the first matching rule; empty ``checks`` means every check.

    Patterns use Python ``re.search`` against the finding's exact target URL.
    A finding without a target URL cannot match a URL-pattern exclusion.
    """
    if not target_url:
        return None
    for rule, pattern in rules:
        checks = rule["checks"]
        if checks and check_id not in checks:
            continue
        if pattern.search(target_url):
            return rule
    return None


def annotate_suppressed_finding(
    finding: Mapping[str, Any], rule: Mapping[str, Any]
) -> dict[str, Any]:
    """Retain the complete issue payload and make the suppression auditable."""
    out = dict(finding)
    out["suppression"] = {
        "rule_id": rule["id"],
        "pattern": rule["pattern"],
        "reason": rule["reason"],
        "matched_url": finding.get("target_url"),
    }
    return out
