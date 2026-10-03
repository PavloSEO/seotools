"""Targeted verification of saved audit findings against a measured URL subset.

This module classifies retained documents only. The shared handler owns optional
recrawling and files; neither a missing page nor an unmeasured check is a fix.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from seohead.sf.core.aggregate import (
    DEPTH_FINDING_CHECKS,
    GRAPH_WIDE_FINDING_CHECKS,
    UNLINKED_FINDING_CHECKS,
)
from seohead.sf.core.registry import CHECKS

MAX_URLS = 500
MAX_FINDINGS = 5_000
_NONLOCAL = (
    UNLINKED_FINDING_CHECKS
    | GRAPH_WIDE_FINDING_CHECKS
    | DEPTH_FINDING_CHECKS
    | {
        "INLINK_BOILERPLATE_ONLY",
        "TITLE_DUPLICATE",
        "DESC_DUPLICATE",
        "H1_DUPLICATE",
        "DUPLICATE_BY_HASH",
        "NEAR_DUPLICATE",
        "TITLE_TEMPLATED",
    }
)
_STATUS_CHECKS = {
    "BROKEN_PAGE_4XX",
    "SERVER_ERROR_5XX",
    "NO_RESPONSE",
    "BLOCKED_BY_ROBOTS",
    "BAD_REDIRECT_TYPE",
}
# Targeted collection intentionally changes workload size. These two bounds do
# not change how one fetched page is interpreted; all other recorded policies do.
_TARGET_BOUNDS = {"limits.max_urls", "limits.max_depth"}


def digest(value: Any) -> str:
    """Stable hash of a JSON-ready observation, independent of file layout."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def scan_identity(document: Mapping[str, Any]) -> str | None:
    """The retained scan identity, when an audit actually records one."""
    summary = document.get("summary")
    contract = summary.get("evidence_contract") if isinstance(summary, Mapping) else None
    run = document.get("run")
    value = (contract.get("scan_uuid") if isinstance(contract, Mapping) else None) or (
        run.get("scan_uuid") if isinstance(run, Mapping) else None
    )
    return value if isinstance(value, str) and value else None


def _generated_at(document: Mapping[str, Any]) -> datetime | None:
    run = document.get("run")
    value = run.get("generated_at") if isinstance(run, Mapping) else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def offline_observation_gap(before: Mapping[str, Any], after: Mapping[str, Any]) -> str | None:
    """Refuse an offline 'fix' without a distinct, later observation identity."""
    first, second = scan_identity(before), scan_identity(after)
    if first is None or second is None:
        return "baseline or after audit lacks a recorded scan UUID; a fresh observation is unproven"
    if first == second:
        return "after audit has the same scan UUID as baseline; no new observation is proven"
    before_time, after_time = _generated_at(before), _generated_at(after)
    if before_time is None or after_time is None:
        return "baseline or after audit lacks a timezone-aware generated_at timestamp"
    if after_time <= before_time:
        return "after audit generated_at is not later than the baseline observation"
    return None


def _strings(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{label} must be a list of nonempty strings")
    return list(dict.fromkeys(value))


def _local_issue(issue: Mapping[str, Any]) -> bool:
    check = issue.get("check")
    source = str((CHECKS.get(check) or {}).get("source") or "")
    return bool(
        check not in _NONLOCAL
        and not source.startswith(("inlinks:", "sitemap", "heuristic"))
        and source != "SF-derived+heuristic"
        and not issue.get("group_id")
    )


def select(
    baseline: Mapping[str, Any],
    *,
    finding_ids: list[str] | None = None,
    urls: list[str] | None = None,
    view: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve a bounded union of IDs, a saved view and affected URLs."""
    issues = baseline.get("issues")
    pages = baseline.get("pages")
    if not isinstance(issues, list) or not isinstance(pages, list):
        raise ValueError("baseline must be a saved audit with pages and issues")
    if view is not None:
        if not isinstance(view, Mapping) or view.get("schema_version") != "verification_view.v1":
            raise ValueError("view must be a verification_view.v1 object")
        view_ids = _strings(view.get("finding_ids"), "view.finding_ids")
        view_urls = _strings(view.get("urls"), "view.urls")
        checks = _strings(view.get("checks"), "view.checks")
    else:
        view_ids, view_urls, checks = [], [], []
    ids = set(_strings(finding_ids, "finding_ids") + view_ids)
    wanted_urls = set(_strings(urls, "urls") + view_urls)
    if not (ids or wanted_urls or checks):
        raise ValueError("select at least one finding ID, saved view, or affected URL")
    known_ids = {item.get("id") for item in issues if isinstance(item, dict)}
    unknown = ids - known_ids
    if unknown:
        raise ValueError(f"finding IDs are absent from baseline: {', '.join(sorted(unknown))}")
    unknown_checks = set(checks) - CHECKS.keys()
    if unknown_checks:
        raise ValueError(f"view names unknown checks: {', '.join(sorted(unknown_checks))}")
    baseline_urls = {item.get("url") for item in pages if isinstance(item, dict)}
    unknown_urls = wanted_urls - baseline_urls
    if unknown_urls:
        raise ValueError(f"URLs are absent from baseline pages: {', '.join(sorted(unknown_urls))}")
    chosen = [
        item
        for item in issues
        if isinstance(item, dict)
        and (
            item.get("id") in ids
            or item.get("target_url") in wanted_urls
            or item.get("check") in checks
        )
    ]
    if not chosen:
        raise ValueError("selection matches no baseline findings")
    invalid_checks = {item.get("check") for item in chosen if item.get("check") not in CHECKS}
    if invalid_checks:
        raise ValueError(
            f"baseline findings name unknown checks: {sorted(map(str, invalid_checks))}"
        )
    if len(chosen) > MAX_FINDINGS:
        raise ValueError(f"selection exceeds {MAX_FINDINGS} findings")
    targets = list(
        dict.fromkeys(
            item["target_url"]
            for item in chosen
            if isinstance(item.get("target_url"), str)
            and item["target_url"] in baseline_urls
            and _local_issue(item)
        )
    )
    if len(targets) > MAX_URLS:
        raise ValueError(f"selection exceeds {MAX_URLS} URLs")
    return chosen, targets


def _issue_signature(issue: Mapping[str, Any]) -> str:
    evidence = issue.get("evidence")
    plain_evidence = dict(evidence) if isinstance(evidence, Mapping) else {}
    # Saved scan IDs differ on every run; the observation itself is the operand.
    plain_evidence.pop("contract", None)
    return digest(
        {
            "check": issue.get("check"),
            "status_code": issue.get("status_code"),
            "details": issue.get("details") or {},
            "locations": issue.get("locations") or [],
            "evidence": plain_evidence,
        }
    )


def _representation(page: Mapping[str, Any]) -> str | None:
    metrics = page.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get("representation")
    return value if isinstance(value, str) and value else None


def _policy_gap(before: Mapping[str, Any], after: Mapping[str, Any]) -> str | None:
    old = (before.get("run") or {}).get("crawl_config")
    new = (after.get("run") or {}).get("crawl_config")
    if not isinstance(old, dict) or not isinstance(new, dict):
        return "one audit lacks its results-affecting crawl configuration"
    changed = sorted(
        key for key in (old.keys() | new.keys()) - _TARGET_BOUNDS if old.get(key) != new.get(key)
    )
    if changed:
        return "results-affecting settings changed: " + ", ".join(changed)
    if (after.get("run") or {}).get("cache_replay"):
        return "recrawl reused cached responses rather than measuring the page now"
    return None


def _check_unmeasured(after: Mapping[str, Any], check: str) -> str | None:
    run = after.get("run") or {}
    for label in ("checks_skipped", "checks_disabled"):
        for item in run.get(label) or []:
            if isinstance(item, Mapping) and item.get("id") == check:
                return f"{check} was {label.removeprefix('checks_')}: {item.get('reason') or 'no evidence'}"
    return None


def _source_gap(after: Mapping[str, Any], url: str) -> str | None:
    source = (after.get("run") or {}).get("source")
    if isinstance(source, str) and source.startswith(("http://", "https://")):
        origin, target = urlsplit(source), urlsplit(url)
        if (origin.scheme, origin.netloc.lower()) != (target.scheme, target.netloc.lower()):
            return "recrawl source origin differs from the affected URL"
    return None


def classify(
    baseline: Mapping[str, Any],
    selected: list[dict[str, Any]],
    observations: Mapping[str, Mapping[str, Any]],
    *,
    missing_reason: str = "selected URL was not recrawled",
) -> list[dict[str, Any]]:
    """Classify each selected baseline finding with its URL's after audit."""
    old_pages = {
        page.get("url"): page
        for page in baseline.get("pages") or []
        if isinstance(page, dict) and page.get("url")
    }
    results = []
    for issue in selected:
        url, check = issue.get("target_url"), issue.get("check")
        status, reason = "not_verifiable", ""
        after_issue = None
        after_page = None
        after = observations.get(url) if isinstance(url, str) else None
        if not isinstance(url, str) or url not in old_pages:
            reason = "finding has no affected baseline page to recrawl"
        elif not _local_issue(issue):
            reason = "finding needs site-wide or relation evidence; a selected-URL recrawl cannot clear it"
        elif after is None:
            reason = missing_reason
        elif (
            after.get("schema_version") != "2.0"
            or not isinstance(after.get("pages"), list)
            or not isinstance(after.get("issues"), list)
        ):
            reason = "recrawl has no complete audit pages and issues document"
        elif (gap := _policy_gap(baseline, after)) is not None or (
            gap := _source_gap(after, url)
        ) is not None:
            reason = gap
        else:
            after_pages = {
                page.get("url"): page
                for page in after.get("pages") or []
                if isinstance(page, dict) and page.get("url")
            }
            after_page = after_pages.get(url)
            if after_page is None:
                reason = "selected URL has no page observation in the recrawl"
            elif (gap := _check_unmeasured(after, check)) is not None:
                reason = gap
            elif _representation(old_pages[url]) != _representation(after_page) or (
                check not in _STATUS_CHECKS and not _representation(old_pages[url])
            ):
                reason = "page representation changed or was not retained in both runs"
            else:
                after_issue = next(
                    (
                        item
                        for item in after.get("issues") or []
                        if isinstance(item, dict)
                        and item.get("check") == check
                        and item.get("target_url") == url
                    ),
                    None,
                )
                if after_issue is not None:
                    status = (
                        "persisting"
                        if _issue_signature(issue) == _issue_signature(after_issue)
                        else "changed"
                    )
                else:
                    coverage = (after.get("summary") or {}).get("check_coverage") or {}
                    silent = coverage.get("checks_silent_ids") or []
                    if check not in silent:
                        reason = "recrawl has no measured clean verdict for this check"
                    elif (after.get("run") or {}).get("requires_rendering") and _representation(
                        after_page
                    ) == "static":
                        reason = "recrawl requires rendering before a clean verdict can be trusted"
                    elif (
                        not isinstance(after_page.get("status_code"), int)
                        or after_page["status_code"] < 100
                    ):
                        reason = "recrawl has no measured HTTP response for the affected URL"
                    elif check not in _STATUS_CHECKS and (
                        not 200 <= after_page["status_code"] < 300
                        or "html" not in str(after_page.get("content_type") or "").lower()
                    ):
                        status, reason = (
                            "changed",
                            "response is no longer a successful HTML page; the original page check was not retested",
                        )
                    elif check in _STATUS_CHECKS and not (
                        200 <= after_page["status_code"] < 300
                        or (
                            check == "BAD_REDIRECT_TYPE" and after_page["status_code"] in {301, 308}
                        )
                    ):
                        status, reason = (
                            "changed",
                            "HTTP response changed but is not a verified clean response",
                        )
                    elif (
                        old_pages[url].get("status_code") != after_page.get("status_code")
                        and check not in _STATUS_CHECKS
                    ):
                        status, reason = (
                            "changed",
                            "HTTP status changed; the original page condition was not retested on the same response",
                        )
                    else:
                        status = "resolved"
        results.append(
            {
                "finding_id": issue.get("id"),
                "check": check,
                "url": url,
                "status": status,
                "reason": reason,
                "before": issue,
                "after": after_issue,
                "before_page": old_pages.get(url),
                "after_page": after_page,
            }
        )
    return results


def markdown(document: Mapping[str, Any]) -> str:
    """Focused human report; the JSON artifact retains full evidence objects."""
    counts = document["summary"]
    lines = [
        "# Targeted fix verification",
        "",
        f"Observed: {document['observed_at']}",
        f"Baseline audit SHA-256: `{document['baseline']['audit_sha256']}`",
        "",
        "| Status | Check | URL | Evidence |",
        "|---|---|---|---|",
    ]
    for item in document["findings"]:
        evidence = item["reason"] or (
            "same finding and evidence" if item["status"] == "persisting" else "see JSON evidence"
        )
        cells = [item["status"], str(item["check"]), str(item["url"]), evidence]
        lines.append(
            "| " + " | ".join(cell.replace("|", "\\|").replace("\n", " ") for cell in cells) + " |"
        )
    lines.extend(
        [
            "",
            "Counts: "
            + ", ".join(
                f"{name}={counts[name]}"
                for name in ("resolved", "persisting", "changed", "not_verifiable")
            ),
        ]
    )
    for item in document["findings"]:
        before_page = item.get("before_page") or {}
        after_page = item.get("after_page") or {}
        evidence = {
            "status": item["status"],
            "reason": item["reason"],
            "before": {
                "finding": item["before"],
                "page": {
                    key: before_page.get(key) for key in ("url", "status_code", "content_type")
                },
            },
            "after": {
                "finding": item["after"],
                "page": {
                    key: after_page.get(key) for key in ("url", "status_code", "content_type")
                },
            },
        }
        lines.extend(
            [
                "",
                f"## {item['check']} — {item['url'] or 'audit-wide'}",
                "",
                *(
                    "    " + line
                    for line in json.dumps(evidence, ensure_ascii=False, indent=2).splitlines()
                ),
            ]
        )
    lines.append("")
    return "\n".join(lines)
