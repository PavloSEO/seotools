"""Write the human-readable ``audit.md`` projection."""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Any

from ..core.models import AuditResult, Issue
from ..core.registry import check_meta

SEVERITY_LABEL = {"critical": "🔴 Critical", "warning": "🟡 Warning", "notice": "⚪ Notice"}
LINK_CHECKS = {"BROKEN_INTERNAL_LINK", "LINK_TO_5XX", "INTERNAL_LINK_TO_REDIRECT"}

# Readability caps — keep tables/sections from exploding on big crawls.
TOP_CHECKS = 12
MAX_CELL_CHARS = 200  # truncate very long cell values
MAX_LINK_ROWS = 200  # rows in a broken-link table (per check)
MAX_GROUP_URLS = 50  # URLs listed per duplicate group
MAX_GENERIC_ROWS = 500  # rows in a generic per-URL table


def _esc(value: Any) -> str:
    if value is None:
        return ""
    # escape backslash first, then the pipe (table-cell delimiter)
    text = str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - 1] + "…"
    return text


def _code(value: Any) -> str:
    """Render a value as an inline code span, neutralizing backticks."""
    return _esc(value).replace("`", "ʼ")  # noqa: RUF001 -- prevents closing the code span


def _kb(n: Any) -> str:
    try:
        return f"{int(n) / 1024:.0f} KB"
    except (TypeError, ValueError):
        return "—"


def write_markdown(result: AuditResult, path: str) -> str:
    lines: list[str] = []
    w = lines.append
    run = result.run
    s = result.summary

    # 1. header
    w(f"# SEO audit — {run.get('project', 'unknown')}")
    w("")
    w(f"- **Generated:** {run.get('generated_at')}")
    w(f"- **Input mode:** {run.get('input_mode')}  ·  **Profile:** {run.get('profile')}")
    if run.get("sf_version_detected"):
        w(f"- **SF version:** {run['sf_version_detected']}")
    w(f"- **Source:** {_esc(run.get('source') or run.get('exports_dir'))}")
    w(f"- **Exports used:** {', '.join(run.get('exports_used', [])) or '—'}")
    w("")

    # 2. health summary
    w("## Health summary")
    w("")
    # An invalid crawl leads with the failure: a score printed next to a
    # critical NO_RESPONSE reads as a verdict on the site rather than on the run.
    if run.get("crawl_valid") is False:
        reason = s.get("health_score_reason") or run.get("crawl_invalid_reason") or "unknown"
        w(f"> **Crawl failed — no health score.** {_esc(reason)}.")
        w(">")
        w("> The findings below describe the failed run, not the state of the site.")
    else:
        w(f"**Health score: {s.get('health_score')} / 100**")
        if s.get("health_score_basis"):
            w("")
            w(f"_{_esc(s['health_score_basis'])}_")
        if s.get("health_score_scope"):
            w("")
            w(f"_{_esc(s['health_score_scope'])}_")
        coverage = s.get("check_coverage")
        if coverage:
            w("")
            w(
                f"- Checks: **{coverage.get('checks_fired', 0)} fired**, "
                f"{coverage.get('checks_skipped', 0)} skipped, "
                f"{coverage.get('checks_silent', 0)} silent, "
                f"{coverage.get('checks_disabled', 0)} disabled "
                f"(of {coverage.get('checks_total', 0)} total)"
            )
    w("")
    totals = s.get("totals", {})
    w(
        f"- URLs crawled: **{totals.get('urls_crawled')}** "
        f"(HTML: {totals.get('html_pages')}, indexable: {totals.get('html_indexable')})"
    )
    w(f"- Total issues: **{totals.get('issues_total')}**")
    w("")
    by_sev = s.get("by_severity", {})
    w("| Severity | Count |")
    w("|---|---:|")
    for sev in ("critical", "warning", "notice"):
        w(f"| {SEVERITY_LABEL[sev]} | {by_sev.get(sev, 0)} |")
    w("")
    top = list(s.get("by_check", {}).items())[:TOP_CHECKS]
    if top:
        # Use the actual severity carried by this run's issues (post
        # severity_overrides), not the registry default — the severity-count
        # table and section headers below are keyed off Issue.severity too,
        # and this table must not contradict them.
        check_severity: dict[str, str] = {}
        for issue in result.issues:
            check_severity.setdefault(issue.check, issue.severity)
        w("**Most frequent issues:**")
        w("")
        w("| Check | Count | Severity |")
        w("|---|---:|---|")
        for check, count in top:
            severity = check_severity.get(check, check_meta(check)["severity"])
            w(f"| `{check}` | {count} | {severity} |")
        w("")
    if "size_stats_bytes" in s:
        ss = s["size_stats_bytes"]
        w(
            f"**HTML size:** median {_kb(ss.get('median'))}, p90 {_kb(ss.get('p90'))}, "
            f"p95 {_kb(ss.get('p95'))}, max {_kb(ss.get('max'))}."
        )
        w("")

    # Printed above the findings, not in an appendix: this exists to be read
    # before the report is believed, and a reader who has already gone through
    # 400 findings has spent the effort it was meant to save (issue #98).
    implausible = s.get("implausible_checks") or []
    if implausible:
        w("## Look at these before trusting the rest")
        w("")
        w(
            "Each check below describes more than half the crawled pages. That can be true "
            "-- a site really may have no meta description anywhere -- but it is also what "
            "a broken check looks like, and it is worth one minute of checking against the "
            "live site before the rest of this report is acted on."
        )
        w("")
        w("| Check | Pages | Share of crawl |")
        w("|---|---:|---:|")
        for row in implausible:
            w(f"| `{row['check']}` | {row['pages']} | {row['share']:.0%} |")
        w("")

    # group issues by severity
    by_sev_issues: dict[str, list[Issue]] = defaultdict(list)
    for issue in result.issues:
        by_sev_issues[issue.severity].append(issue)

    # 3/4/5. sections by severity
    for sev in ("critical", "warning", "notice"):
        issues = by_sev_issues.get(sev, [])
        if not issues:
            continue
        w(f"## {SEVERITY_LABEL[sev]} ({len(issues)})")
        w("")
        _render_severity_section(w, issues)

    # 6. sitemap & robots
    if "sitemap" in s:
        _render_sitemap(w, s["sitemap"])

    # 7. appendix
    # Read from the authoritative dataclass lists on the result, not from
    # ``run`` — those keys only exist in the mapping that AuditResult.to_json()
    # builds for JSON serialization, and this renderer never sees that copy.
    skipped = result.skipped
    if skipped:
        w("## Appendix: skipped checks")
        w("")
        w("| Check | Reason |")
        w("|---|---|")
        for sk in skipped:
            w(f"| `{sk.id}` | {_esc(sk.reason)} |")
        w("")

    # A disabled check is an operator's own choice, not missing evidence, but
    # it must still be visible here rather than passing as a clean result
    # (issue #177).
    disabled = result.disabled
    if disabled:
        w("## Appendix: disabled checks")
        w("")
        w("| Check |")
        w("|---|")
        for d in disabled:
            w(f"| `{d.id}` |")
        w("")

    text = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _render_severity_section(w, issues: list[Issue]) -> None:
    by_check: dict[str, list[Issue]] = defaultdict(list)
    for issue in issues:
        by_check[issue.check].append(issue)

    for check, group in sorted(by_check.items()):
        meta = check_meta(check)
        w(f"### `{check}` — {meta['message']} ({len(group)})")
        w("")
        if check in LINK_CHECKS:
            _render_link_table(w, group)
        elif check == "LARGE_HTML":
            _render_large_html(w, group)
        elif check in ("TITLE_DUPLICATE", "DESC_DUPLICATE", "H1_DUPLICATE", "DUPLICATE_BY_HASH"):
            _render_duplicates(w, group)
        elif check == "H1_MULTIPLE":
            _render_h1_multiple(w, group)
        else:
            _render_generic(w, group)
        if meta.get("fix"):
            w(f"> _How to fix:_ {meta['fix']}")
            w("")


def _render_link_table(w, group: list[Issue]) -> None:
    w("| Destination | Status | Source page | Anchor | Position | XPath |")
    w("|---|---:|---|---|---|---|")
    rows = 0
    for issue in group:
        for loc in issue.locations:
            if rows >= MAX_LINK_ROWS:
                w("| … more rows omitted | | | | | |")
                w("")
                return
            rows += 1
            w(
                f"| {_esc(issue.target_url)} | {issue.status_code or ''} "
                f"| {_esc(loc.get('source_url'))} | {_esc(loc.get('anchor'))} "
                f"| {_esc(loc.get('link_position'))} | `{_code(loc.get('link_path'))}` |"
            )
    w("")


def _render_large_html(w, group: list[Issue]) -> None:
    w("| URL | Size | × median | Rank |")  # noqa: RUF001 -- mathematical multiplier
    w("|---|---:|---:|---:|")
    for issue in sorted(group, key=lambda i: i.details.get("ratio", 0), reverse=True):
        d = issue.details
        w(
            f"| {_esc(issue.target_url)} | {_kb(d.get('size_bytes'))} "
            f"| ×{d.get('ratio')} | {d.get('rank') or '—'} |"  # noqa: RUF001
        )
    w("")


def _render_duplicates(w, group: list[Issue]) -> None:
    seen_groups: set[str] = set()
    for issue in group:
        gid = issue.group_id
        if gid in seen_groups:
            continue
        seen_groups.add(gid)
        value = issue.details.get("value")
        urls = [i.target_url for i in group if i.group_id == gid]
        if value:
            w(f'- **"{_esc(value)}"** — {len(urls)} URLs:')
        else:
            w(f"- Group `{gid}` — {len(urls)} URLs:")
        for url in urls[:MAX_GROUP_URLS]:
            w(f"    - {_esc(url)}")
        if len(urls) > MAX_GROUP_URLS:
            w(f"    - … {len(urls) - MAX_GROUP_URLS} more")
    w("")


def _render_h1_multiple(w, group: list[Issue]) -> None:
    w("| URL | H1 text |")
    w("|---|---|")
    for issue in group:
        texts = " ⏐ ".join(_esc(t) for t in issue.details.get("h1_texts", []))
        w(f"| {_esc(issue.target_url)} | {texts} |")
    w("")


def _render_generic(w, group: list[Issue]) -> None:
    has_details = any(i.details for i in group)
    if has_details:
        w("| URL | Details |")
        w("|---|---|")
        for issue in group[:MAX_GENERIC_ROWS]:
            detail = ", ".join(
                f"{k}={_esc(v)}"
                for k, v in issue.details.items()
                if not isinstance(v, (list, dict))
            )
            w(f"| {_esc(issue.target_url)} | {detail} |")
    else:
        for issue in group[:MAX_GENERIC_ROWS]:
            w(f"- {_esc(issue.target_url)}")
    if len(group) > MAX_GENERIC_ROWS:
        w(f"- … {len(group) - MAX_GENERIC_ROWS} more")
    w("")


def _render_sitemap(w, sm: dict[str, Any]) -> None:
    w("## Sitemap & robots")
    w("")
    w(f"- Declared in robots.txt: **{sm.get('declared_in_robots')}**")
    if sm.get("sitemaps"):
        w(f"- Sitemaps: {', '.join(_esc(x) for x in sm['sitemaps'])}")
    w(
        f"- URLs in sitemap: **{sm.get('urls_in_sitemap')}**  ·  "
        f"indexable URLs in crawl: **{sm.get('urls_in_crawl_indexable')}**"
    )
    w(
        f"- In sitemap but not in crawl: **{sm.get('in_sitemap_not_in_crawl')}**  ·  "
        f"in crawl but not in sitemap: **{sm.get('in_crawl_not_in_sitemap')}**"
    )
    w(
        f"- Non-200 URLs in sitemap: **{sm.get('non_200_in_sitemap')}**  ·  "
        f"non-indexable URLs in sitemap: **{sm.get('non_indexable_in_sitemap')}**"
    )
    lm = sm.get("lastmod")
    if lm:
        w(
            f"- lastmod: {lm.get('oldest')} to {lm.get('newest')} "
            f"(median {lm.get('median')}); older than threshold: "
            f"{round(lm.get('share_older_than_threshold', 0) * 100)}%"
            + (" · all values are identical!" if lm.get("all_identical") else "")
            + (f" · invalid: {lm['invalid_count']}" if lm.get("invalid_count") else "")
        )
    w("")
