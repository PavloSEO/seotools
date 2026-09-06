"""Configuration defaults and loading.

Every threshold, severity and ``enabled`` flag lives here so the audit is tuned
from JSON, never from code. User config is deep-merged over these defaults.
"""

from __future__ import annotations

import copy
import json
import math
import os
from typing import Any

# The only severities the schema and the scoring weights know about (issue
# #211): anything else silently drops out of by_severity and the weighted
# penalty, which inflates the health score exactly when a check is supposed
# to be hurting it.
ALLOWED_SEVERITIES = ("critical", "warning", "notice")


class ConfigError(ValueError):
    """A config value that would corrupt scoring or violate the audit schema."""


DEFAULT_CONFIG: dict[str, Any] = {
    "sf_cli": {
        "path": "",
        "search_paths": [
            "/Applications/Screaming Frog SEO Spider.app/Contents/MacOS/ScreamingFrogSEOSpiderLauncher",
            "/Applications/Screaming Frog SEO Spider.app/Contents/MacOS/ScreamingFrogSEOSpiderCli",
            "/usr/bin/screamingfrogseospider",
            r"C:\Program Files (x86)\Screaming Frog SEO Spider\ScreamingFrogSEOSpiderCli.exe",
            r"C:\Program Files\Screaming Frog SEO Spider\ScreamingFrogSEOSpiderCli.exe",
        ],
        # Auto-used when this file exists in the working dir (set up once, all sites).
        # Create it once in SF (see skill sf-config) to unlock the module checks.
        "seospiderconfig": "audit.seospiderconfig",
        "export_format": "csv",
        # A deadline, not a safety valve: Screaming Frog writes its exports when
        # the crawl ends, so cutting a run short discards all of it. When a rate
        # limit is set and the URL count is known, the runner raises this to
        # what the crawl actually needs and says so.
        "timeout_minutes": 30,
        # How many URLs the run will request, when the caller knows better than
        # the sitemap does. 0 means "work it out".
        "expected_urls": 0,
    },
    "profile": "full",  # lite | full | custom
    "exports": {
        "tabs": [
            "Internal:All",
            "Response Codes:Client Error (4xx)",
            "Response Codes:Server Error (5xx)",
            "Response Codes:Redirection (3xx)",
            "Sitemaps:URLs In Sitemap",
            "Sitemaps:URLs Not In Sitemap",
            "Sitemaps:Orphan URLs",
            "Sitemaps:Non-Indexable URLs In Sitemap",
            "Page Titles:Multiple",
            # Unlocked by audit.seospiderconfig modules; empty (skipped) without it.
            "Structured Data:Validation Errors",
            "Structured Data:Validation Warnings",
            "Security:Mixed Content",
            "Images:Missing Alt Text",
            "Images:Missing Size Attributes",
        ],
        "bulk": [
            "Response Codes:Client Error (4xx) Inlinks",
            "Response Codes:Server Error (5xx) Inlinks",
            "Response Codes:Redirection (3xx) Inlinks",
        ],
        # Crawl Overview is deliberately not requested: SF writes it as a
        # two-column metadata header followed by a five-column table in the
        # same CSV, a shape no consumer parses (#286), so registering it only
        # produced a false "read error" for a file that was written correctly.
        "reports": ["Redirects:Redirect Chains"],
        "fetch_all_inlinks": False,
    },
    "input": {"mode": "auto", "exports_dir": "exports", "html_store_dir": None},
    "filters": {
        "content_type_include": ["text/html"],
        "exclude_extensions": [
            ".js",
            ".css",
            ".json",
            ".xml",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".svg",
            ".pdf",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
            ".ico",
        ],
    },
    "thresholds": {
        "title_max_chars": 60,
        "title_max_px": 561,
        "title_min_chars": 30,
        "desc_max_chars": 160,
        "desc_max_px": 985,
        "desc_min_chars": 70,
        "h1_max_chars": 70,
        "thin_content_words": 200,
        "low_text_ratio_pct": 10,
        "url_max_chars": 115,
        "crawl_depth_max": 4,
        "orphan_inlinks_min": 1,
        "response_time_max_s": 1.5,
        "large_html_abs_kb": 200,
        "large_html_x_median": 3.0,
        "dom_depth_max": 32,
        "dom_nodes_max": 1500,
        "img_max_kb": 150,
        "sitemap_lastmod_stale_days": 365,
        "sitemap_desync_pct_warn": 20,
        "templated_title_share": 0.6,
        "bytes_per_word_x_median": 4.0,
        "readability_flesch_min": 30,
        "avg_words_per_sentence_max": 25,
        "high_external_outlinks": 100,
        "high_outlinks": 300,
        "redirect_hop_cap": 20,
        "link_score_low_ratio": 0.25,
        "near_duplicate_similarity": 0.92,
    },
    "requirements": {
        "require_canonical": True,
        "require_h2": False,
        "require_hreflang": False,
        "require_structured_data": False,
        "require_og": False,
    },
    "live_recheck": {
        "enabled": False,
        "use": "auto",  # auto | advertools | stdlib
        "user_agent": "Mozilla/5.0 (compatible; SEOHEAD-Tools/3.0; +https://seohead.tech/seotools)",
        "max_urls": 5000,
        "timeout_s": 10,
    },
    "tasks_pipeline": {
        "include_severities": ["critical", "warning", "notice"],
        "group_by": "check",  # check (one task per problem type) | issue (one per URL)
        "priority_map": {"critical": "P1", "warning": "P2", "notice": "P3"},
        "effort_map": {"critical": "high", "warning": "medium", "notice": "low"},
        "max_urls_per_task": 25,
        "min_occurrences": 1,
        "include_checks": [],  # empty = all enabled checks
        "exclude_checks": [],
    },
    "checks": {},  # per-check overrides: {"CHECK_ID": {"enabled": false, "severity": "notice"}}
    "severity_overrides": {},  # {"CHECK_ID": "notice"}
    "scoring": {"weights": {"critical": 5, "warning": 2, "notice": 0.5}},
    "output": {
        "json_path": "audit.json",
        "md_path": "audit.md",
        "keep_raw_exports": True,
        "max_locations_per_issue": 200,
        "max_pages_in_json": 100000,
    },
}

# Lite profile: a fast subset for regular monitoring.
LITE_EXPORTS = {
    "tabs": [
        "Internal:All",
        "Response Codes:Client Error (4xx)",
        "Response Codes:Server Error (5xx)",
        "Response Codes:Redirection (3xx)",
        "Sitemaps:URLs In Sitemap",
    ],
    "bulk": [
        "Response Codes:Client Error (4xx) Inlinks",
        "Response Codes:Server Error (5xx) Inlinks",
        "Response Codes:Redirection (3xx) Inlinks",
    ],
    # See the comment on the full profile's "reports" default: Crawl Overview
    # is not requested because nothing parses its two-section CSV yet (#286).
    "reports": [],
    "fetch_all_inlinks": False,
}


def deep_merge(base: dict, overrides: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(path: str | None) -> dict[str, Any]:
    """Load and deep-merge a config file over the defaults.

    A missing ``config.json`` is not an error (defaults are used); a missing
    explicitly-named file is.
    """
    if not path:
        return copy.deepcopy(DEFAULT_CONFIG)
    if not os.path.isfile(path):
        if os.path.basename(path) == "config.json":
            return copy.deepcopy(DEFAULT_CONFIG)
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, encoding="utf-8") as handle:
        user = json.load(handle)
    # Note: the profile is NOT expanded here. run_audit applies it once, AFTER
    # CLI/MCP overrides, so e.g. config profile=lite + --profile full works.
    return deep_merge(DEFAULT_CONFIG, user)


def apply_profile(cfg: dict[str, Any]) -> dict[str, Any]:
    """Resolve the ``profile`` shortcut into a concrete ``exports`` set.

    Idempotent and called once after all overrides. ``lite`` swaps in the lean
    export set; ``full``/``custom`` leave the (default or user-pinned) set alone.
    """
    profile = cfg.get("profile", "full")
    if profile == "lite":
        cfg = deep_merge(cfg, {"exports": LITE_EXPORTS})
    return cfg


def validate_config(cfg: dict[str, Any]) -> None:
    """Reject a config that would corrupt scoring or the audit.json schema.

    Called once, after every CLI/MCP override has landed and before a single
    check runs: a bad severity or weight must never reach an ``Issue``,
    because by the time the health score exists the corruption is invisible
    in the number (issue #211) and the report still validates against
    nothing.
    """
    from seohead.sf.core.registry import CHECKS

    errors: list[str] = []

    def _bad_severity(where: str, check_id: str, severity: Any) -> None:
        errors.append(
            f"{where}[{check_id!r}] severity is {severity!r}; must be one of {ALLOWED_SEVERITIES}"
        )

    for check_id, severity in cfg.get("severity_overrides", {}).items():
        if check_id not in CHECKS:
            errors.append(f"severity_overrides names unknown check {check_id!r}")
        elif severity not in ALLOWED_SEVERITIES:
            _bad_severity("severity_overrides", check_id, severity)

    for check_id, check_cfg in cfg.get("checks", {}).items():
        if check_id not in CHECKS:
            errors.append(f"checks names unknown check {check_id!r}")
            continue
        if "severity" in check_cfg and check_cfg["severity"] not in ALLOWED_SEVERITIES:
            _bad_severity("checks", check_id, check_cfg["severity"])
        if "enabled" in check_cfg and not isinstance(check_cfg["enabled"], bool):
            errors.append(
                f"checks[{check_id!r}].enabled is {check_cfg['enabled']!r}; must be true or false"
            )

    for severity, weight in cfg.get("scoring", {}).get("weights", {}).items():
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            errors.append(f"scoring.weights[{severity!r}] is {weight!r}; must be a number")
        elif not math.isfinite(weight) or weight < 0:
            errors.append(f"scoring.weights[{severity!r}] is {weight!r}; must be finite and >= 0")

    if errors:
        raise ConfigError("; ".join(errors))
