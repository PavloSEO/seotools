"""Validation helpers for operator-declared canonical URL policies."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

CANONICAL_POLICY_CATEGORIES = ("pagination", "filters")
_POLICIES = {
    "pagination": {"self", "first_page", "landing"},
    "filters": {"self", "landing"},
}
_RULE_KEYS = {"pattern", "policy", "target"}


def validate_canonical_policy(policy: Any, *, path: str = "canonical_policy") -> None:
    """Validate ordered URL-pattern rules before any audit evidence is consumed.

    Rules are operator policy, not inferred SEO defaults. For each category, the
    first matching regex wins. Non-self policies name their expected target
    explicitly so the checker never invents a first page or landing URL.
    """
    if not isinstance(policy, dict):
        raise ValueError(f"{path} must be an object")
    unknown_categories = set(policy) - set(CANONICAL_POLICY_CATEGORIES)
    if unknown_categories:
        raise ValueError(f"{path} has unknown categories {sorted(unknown_categories)}")

    for category in CANONICAL_POLICY_CATEGORIES:
        rules = policy.get(category, [])
        if not isinstance(rules, list):
            raise ValueError(f"{path}.{category} must be a list")
        for index, rule in enumerate(rules):
            where = f"{path}.{category}[{index}]"
            if not isinstance(rule, dict):
                raise ValueError(f"{where} must be an object")
            unknown_keys = set(rule) - _RULE_KEYS
            if unknown_keys:
                raise ValueError(f"{where} has unknown keys {sorted(unknown_keys)}")
            pattern = rule.get("pattern")
            if not isinstance(pattern, str) or not pattern:
                raise ValueError(f"{where}.pattern must be a non-empty regex string")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"{where}.pattern is not a valid regex: {exc}") from exc

            expected = rule.get("policy")
            if not isinstance(expected, str) or expected not in _POLICIES[category]:
                allowed = sorted(_POLICIES[category])
                raise ValueError(f"{where}.policy must be one of {allowed}")
            target = rule.get("target")
            if expected == "self":
                if target is not None:
                    raise ValueError(f"{where}.target is only valid for a non-self policy")
                continue
            if not isinstance(target, str) or not target:
                raise ValueError(f"{where}.target must be an absolute HTTP(S) URL")
            try:
                parsed = urlsplit(target)
                port = parsed.port
                valid_target = (
                    parsed.scheme.lower() in {"http", "https"}
                    and bool(parsed.hostname)
                    and parsed.username is None
                    and parsed.password is None
                    and (port is None or 1 <= port <= 65535)
                    and not parsed.fragment
                    and not any(char.isspace() or ord(char) < 0x20 for char in target)
                )
            except ValueError:
                valid_target = False
            if not valid_target:
                raise ValueError(
                    f"{where}.target must be an absolute HTTP(S) URL without credentials, "
                    "whitespace, or fragment"
                )


def matching_canonical_rule(
    policy: dict[str, Any], category: str, url: str
) -> dict[str, Any] | None:
    """Return the first configured rule matching the complete URL string."""
    for rule in policy.get(category, []):
        if re.search(rule["pattern"], url):
            return rule
    return None
