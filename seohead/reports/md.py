"""Write a portable Markdown report for editors and version control."""

from __future__ import annotations

import pathlib
from typing import Any


def write(document: dict[str, Any], path: pathlib.Path) -> None:
    from seohead.reports import SEVERITY_TITLES

    summary = document.get("summary") or {}
    by_sev = summary.get("findings_by_severity") or {}
    out: list[str] = [
        f"# SEO Audit: {document.get('domain', '')}",
        "",
        f"{document.get('url', '')} · Generated {document.get('generated_at', '')}",
        "",
    ]

    # Failed/partial crawl scope must be read before the metrics table below,
    # not discovered afterward: a recipient must not mistake a failed or
    # sampled crawl for a clean, site-wide audit (#361).
    if summary.get("crawl_valid") is False:
        reason = summary.get("crawl_invalid_reason") or "the crawl produced no usable data"
        out += [f"> **Crawl failed — no health score.** {reason}", ""]
    if summary.get("crawl_partial"):
        finish = summary.get("crawl_finish_reason")
        scope = summary.get("crawl_scope_note")
        bits = [b for b in (f"stopped: {finish}" if finish else None, scope) if b]
        detail = f" {'; '.join(bits)}" if bits else ""
        out += [f"> **Partial crawl — scope is limited.**{detail}", ""]

    out += [
        "| Metric | Value |",
        "|---|---|",
        f"| Pages checked | {summary.get('pages_checked', 0)} |",
        f"| Critical findings | {by_sev.get('critical', 0)} |",
        f"| Warnings | {by_sev.get('warning', 0)} |",
        f"| Notices | {by_sev.get('notice', 0)} |",
        "",
    ]

    disabled = summary.get("checks_disabled") or []
    if disabled:
        out += [
            "## Disabled checks",
            "",
            "These checks were deliberately turned off and did not run --"
            " do not read their silence as a clean result:",
            "",
        ]
        out += [f"- **{d.get('id')}** — {d.get('reason')}" for d in disabled] + [""]

    failed = summary.get("tools_failed") or []
    if failed:
        out += [
            "## Unavailable checks",
            "",
            "These checks did not complete. Their silence does not mean no issues were found:",
            "",
        ]
        out += [f"- **{f.get('tool')}** — {f.get('error')}" for f in failed] + [""]

    findings = document.get("findings") or []
    for level in ("critical", "warning", "notice"):
        chunk = [f for f in findings if f.get("severity") == level]
        if not chunk:
            continue
        out += [f"## {SEVERITY_TITLES.get(level, level)} — {len(chunk)}", ""]
        for finding in chunk:
            where = finding.get("url") or finding.get("source", "")
            out.append(f"- {finding.get('text', '')}" + (f" — `{where}`" if where else ""))
        out.append("")

    pages = document.get("pages") or []
    if pages:
        out += [
            "## Pages",
            "",
            "| URL | Status | Title | Words | Canonical |",
            "|---|---|---|---|---|",
        ]
        for page in pages:
            out.append(
                "| {} | {} | {} | {} | {} |".format(
                    page.get("url", ""),
                    page.get("status", ""),
                    str(page.get("title", "")).replace("|", "\\|")[:80],
                    page.get("words", ""),
                    str(page.get("canonical", "")).replace("|", "\\|")[:60],
                )
            )
        out.append("")

    note = summary.get("severity_note")
    if note:
        out += ["---", "", f"_{note}_", ""]

    path.write_text("\n".join(out), encoding="utf-8")
