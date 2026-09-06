"""Turn an audit into a prioritized, actionable task backlog.

A thin, configurable pipeline on top of ``audit.json``: it groups issues into
work items, maps severity to priority/effort, caps the URL lists and emits both
a machine-readable ``tasks.json`` and a readable ``tasks.md``. Drives the
``sf-tasks`` skill and the ``sf-analyzer tasks`` CLI / ``sf_audit_tasks`` MCP tool.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from .config import DEFAULT_CONFIG
from .core.registry import check_meta

_PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}
LINK_CHECKS = {"BROKEN_INTERNAL_LINK", "LINK_TO_5XX", "INTERNAL_LINK_TO_REDIRECT"}


def _pipeline_cfg(config: dict[str, Any] | None) -> dict[str, Any]:
    base = dict(DEFAULT_CONFIG["tasks_pipeline"])
    if config and isinstance(config.get("tasks_pipeline"), dict):
        base.update(config["tasks_pipeline"])
    return base


def _task_id(check: str, key: str) -> str:
    digest = hashlib.sha1(f"{check}|{key}".encode(), usedforsecurity=False).hexdigest()[:8]
    return "TASK-" + digest


def build_tasks(audit: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a task backlog from an ``audit.json`` dict (``AuditResult.to_json()``)."""
    cfg = _pipeline_cfg(config)
    sev_filter = set(cfg["include_severities"])
    include = set(cfg["include_checks"])
    exclude = set(cfg["exclude_checks"])
    prio = cfg["priority_map"]
    effort = cfg["effort_map"]
    cap = cfg["max_urls_per_task"]
    # A separate location cap (#309): max_urls_per_task caps the number of
    # distinct target URLs a task lists, and must not also silently cap how
    # many source locations a broken-link task carries. Absent an explicit
    # override, locations are capped at the pipeline's own default target
    # cap rather than whatever value the caller passed for max_urls_per_task,
    # so lowering the target cap alone cannot reduce location coverage.
    loc_cap = cfg.get(
        "max_locations_per_task", DEFAULT_CONFIG["tasks_pipeline"]["max_urls_per_task"]
    )
    min_occ = cfg["min_occurrences"]
    group_by = cfg["group_by"]

    issues = [
        i
        for i in audit.get("issues", [])
        if i["severity"] in sev_filter
        and (not include or i["check"] in include)
        and i["check"] not in exclude
    ]

    tasks: list[dict[str, Any]] = (
        _group_by_check(issues, prio, effort, cap, loc_cap, min_occ)
        if group_by == "check"
        else _per_issue(issues, prio, effort, cap, loc_cap, min_occ)
    )
    tasks.sort(key=lambda t: (_PRIORITY_ORDER.get(t["priority"], 9), -t["affected_count"]))

    by_priority: dict[str, int] = {}
    for t in tasks:
        by_priority[t["priority"]] = by_priority.get(t["priority"], 0) + 1

    run = audit.get("run", {})
    summary = audit.get("summary", {})
    return {
        "schema_version": "1.0",
        "source": {
            "project": run.get("project"),
            "generated_at": run.get("generated_at"),
            "health_score": summary.get("health_score"),
            "crawl_valid": run.get("crawl_valid", True),
            "crawl_invalid_reason": run.get("crawl_invalid_reason"),
            # Carried through, not recomputed (#308): a partial or
            # coverage-limited audit must keep saying so all the way to the
            # backlog a developer actually reads, instead of a normal-looking
            # scored task list quietly losing what it was scored against.
            "crawl_partial": run.get("crawl_partial", False),
            "crawl_finish_reason": run.get("crawl_finish_reason"),
            "health_score_scope": summary.get("health_score_scope"),
            "health_score_basis": summary.get("health_score_basis"),
            "check_coverage": summary.get("check_coverage"),
        },
        "pipeline": cfg,
        "summary": {"tasks_total": len(tasks), "by_priority": by_priority},
        "tasks": tasks,
    }


def _group_by_check(issues, prio, effort, cap, loc_cap, min_occ) -> list[dict[str, Any]]:
    groups: dict[str, list[dict]] = {}
    for issue in issues:
        groups.setdefault(issue["check"], []).append(issue)

    tasks: list[dict[str, Any]] = []
    for check, group in groups.items():
        urls = [i["target_url"] for i in group if i.get("target_url")]
        unique_urls = list(dict.fromkeys(urls))  # stable de-dupe
        occurrences = sum(i.get("occurrences_count", 1) for i in group)
        # Threshold on the summed occurrence count, not the number of issue
        # records: one BROKEN_INTERNAL_LINK record can itself represent many
        # link occurrences (#224), so counting records undercounts and drops
        # findings min_occurrences was meant to keep.
        if occurrences < min_occ:
            continue
        meta = check_meta(check)
        severity = group[0]["severity"]
        page_count_label = "page" if len(unique_urls) == 1 else "pages"
        task = {
            "id": _task_id(check, "all"),
            "check": check,
            "priority": prio.get(severity, "P3"),
            "severity": severity,
            "effort": effort.get(severity, "medium"),
            "title": (
                f"{meta['message']} — {len(unique_urls)} {page_count_label}"
                if unique_urls
                else meta["message"]
            ),
            "fix_hint": meta.get("fix"),
            "source": group[0].get("source"),
            "affected_count": len(unique_urls) or len(group),
            "occurrences": occurrences,
            "urls": unique_urls[:cap],
            "urls_truncated": max(0, len(unique_urls) - cap),
        }
        if check in LINK_CHECKS:
            links, total, truncated = _link_evidence(group, loc_cap)
            task["broken_links"] = links
            task["broken_links_total"] = total
            task["broken_links_truncated"] = truncated
        tasks.append(task)
    return tasks


def _per_issue(issues, prio, effort, cap, loc_cap, min_occ) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for issue in issues:
        # Same threshold semantics as _group_by_check (#224): min_occurrences
        # was previously never enforced on this branch at all.
        if issue.get("occurrences_count", 1) < min_occ:
            continue
        severity = issue["severity"]
        meta = check_meta(issue["check"])
        task = {
            "id": "TASK-"
            + (
                issue.get("id", "").replace("ISSUE-", "")
                or _task_id(issue["check"], str(issue.get("target_url")))
            ),
            "check": issue["check"],
            "priority": prio.get(severity, "P3"),
            "severity": severity,
            "effort": effort.get(severity, "medium"),
            "title": f"{meta['message']}: {issue.get('target_url') or ''}".strip(),
            "fix_hint": issue.get("fix_hint") or meta.get("fix"),
            "source": issue.get("source"),
            "affected_count": 1,
            "occurrences": issue.get("occurrences_count", 1),
            "urls": [issue["target_url"]] if issue.get("target_url") else [],
            "urls_truncated": 0,
        }
        if issue["check"] in LINK_CHECKS:
            links, total, truncated = _link_evidence([issue], loc_cap)
            task["broken_links"] = links
            task["broken_links_total"] = total
            task["broken_links_truncated"] = truncated
        tasks.append(task)
    return tasks


def _link_evidence(group: list[dict], loc_cap: int) -> tuple[list[dict[str, Any]], int, int]:
    """Return (kept locations, total locations, omitted count).

    ``loc_cap`` caps how many source locations are kept per task — a cap
    named and reported separately from the target-URL cap (#309), so a
    caller can always tell whether any evidence was left out.
    """
    total = sum(len(issue.get("locations") or []) for issue in group)
    out: list[dict[str, Any]] = []
    for issue in group:
        for loc in issue.get("locations") or []:
            if len(out) >= loc_cap:
                break
            out.append(
                {
                    "target_url": issue.get("target_url"),
                    "status_code": issue.get("status_code"),
                    "source_url": loc.get("source_url"),
                    "anchor": loc.get("anchor"),
                    "link_position": loc.get("link_position"),
                    "link_path": loc.get("link_path"),
                }
            )
        if len(out) >= loc_cap:
            break
    return out, total, max(0, total - len(out))


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------
def _esc(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def render_tasks_md(backlog: dict[str, Any]) -> str:
    src = backlog["source"]
    lines = [f"# Audit Tasks — {src.get('project', 'site')}", ""]
    # A failed crawl says so before anything else: the tasks below describe the
    # failed run, and a reader must not take them for a picture of the site.
    if src.get("crawl_valid") is False:
        reason = src.get("crawl_invalid_reason") or "the crawl produced no usable data"
        lines.append(f"> **Crawl failed — no health score.** {reason}.")
        lines.append("")
    # A partial crawl still scores, so this warning is distinct from the
    # failed-crawl one above: the run produced a real backlog, but only for
    # the slice of the site it actually reached (#308). Both can appear
    # together — a run can fail validity for other reasons while also having
    # stopped early — since they report different facts.
    if src.get("crawl_partial"):
        stop = src.get("crawl_finish_reason")
        stop_text = f" Stopped early: `{stop}`." if stop else ""
        lines.append(f"> **Partial crawl — this is a sample, not the whole site.**{stop_text}")
        if src.get("health_score_scope"):
            lines.append(f"> {src['health_score_scope']}")
        lines.append("")
    # health_score_basis is set whenever checks were skipped or disabled,
    # independent of crawl_partial (#457) — a fully-crawled, export-based run
    # missing files gets this warning too, and it must reach the human-facing
    # backlog even when the crawl_partial block above never fires.
    if src.get("health_score_basis"):
        lines.append(f"> {src['health_score_basis']}")
        lines.append("")
    health = src.get("health_score")
    health_text = "n/a" if health is None else health
    lines.append(f"- Source: audit generated at {src.get('generated_at')} (health {health_text})")
    lines.append(
        f"- Tasks: **{backlog['summary']['tasks_total']}** "
        f"({', '.join(f'{k}: {v}' for k, v in sorted(backlog['summary']['by_priority'].items()))})"
    )
    lines.append("")

    by_prio: dict[str, list[dict]] = {}
    for t in backlog["tasks"]:
        by_prio.setdefault(t["priority"], []).append(t)

    for prio in sorted(by_prio, key=lambda p: _PRIORITY_ORDER.get(p, 9)):
        lines.append(f"## {prio} ({len(by_prio[prio])})")
        lines.append("")
        for t in by_prio[prio]:
            lines.append(
                f"- [ ] **{_esc(t['title'])}** "
                f"`{t['check']}` · {t['severity']} · effort: {t['effort']} · `{t['id']}`"
            )
            if t.get("fix_hint"):
                lines.append(f"    - _How to fix:_ {_esc(t['fix_hint'])}")
            if t.get("broken_links"):
                lines.append("    - Broken links (destination ← source · position · XPath):")
                shown = t["broken_links"][:15]
                for bl in shown:
                    lines.append(
                        f"        - {_esc(bl['target_url'])} ({bl.get('status_code')}) "
                        f"← {_esc(bl['source_url'])} · {_esc(bl.get('link_position'))} "
                        f"· `{_esc(bl.get('link_path'))}`"
                    )
                # Locations cut by the display slice above and locations cut
                # by the location cap (#309) are both real omissions — a
                # reader must not mistake either for the full evidence list.
                hidden = t.get("broken_links_truncated", 0) + max(
                    0, len(t["broken_links"]) - len(shown)
                )
                if hidden:
                    lines.append(f"        - … {hidden} more source location(s) omitted")
            elif t["urls"]:
                for url in t["urls"][:15]:
                    lines.append(f"        - {_esc(url)}")
                shown = min(15, len(t["urls"]))
                hidden = t["urls_truncated"] + max(0, len(t["urls"]) - shown)
                if hidden:
                    lines.append(f"        - … {hidden} more")
            lines.append("")
    return "\n".join(lines) + "\n"


def write_tasks(backlog: dict[str, Any], json_path: str, md_path: str) -> tuple[str, str]:
    import json

    os.makedirs(os.path.dirname(os.path.abspath(json_path)) or ".", exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(backlog, fh, ensure_ascii=False, indent=2)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(render_tasks_md(backlog))
    return json_path, md_path
