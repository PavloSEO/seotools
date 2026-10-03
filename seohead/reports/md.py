"""Write a portable Markdown report for editors and version control."""

from __future__ import annotations

import pathlib
from typing import Any


def _field(value: Any, limit: int | None = None) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|")[:limit] if limit else text


def _coverage_field(value: Any) -> str:
    """Keep project-controlled newlines from changing the Markdown table shape."""
    return _field(value).replace("\r", " ").replace("\n", " ")


def write(document: dict[str, Any], path: pathlib.Path) -> None:
    from seohead.reports import SEVERITY_TITLES
    from seohead.reports.client_findings import check_title

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

    from seohead.reports.evidence_summary import rows as evidence_rows

    evidence = evidence_rows(summary)
    if evidence:
        out += [
            "## Saved evidence coverage",
            "",
            "| Kind | Measurement | State | Scope or reason |",
            "|---|---|---|---|",
        ]
        out.extend(
            "| " + " | ".join(_coverage_field(value) for value in row) + " |" for row in evidence
        )
        out.append("")

    coverage = summary.get("project_coverage")
    if isinstance(coverage, dict):
        from seohead.reports.project_coverage import priority_text, value_text

        project = coverage.get("project") or {}
        status = coverage.get("status") or {}
        out += [
            "## Project checklist coverage",
            "",
            f"Project: {project.get('site', '')} · UUID: {project.get('uuid', '')}",
            f"Checklist state: {status.get('state', '')} · Revision: {status.get('revision', '')}",
            "",
        ]
        counts = status.get("counts")
        if isinstance(counts, dict):
            out += [
                "| Total | Complete | Remaining | Run | Not applicable | Not run | Stale | Disabled |",
                "|---|---|---|---|---|---|---|---|",
                "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                    _field(counts.get("total")),
                    _field(counts.get("complete")),
                    _field(counts.get("remaining")),
                    _field(counts.get("run")),
                    _field(counts.get("not_applicable")),
                    _field(counts.get("not_run")),
                    _field(counts.get("stale")),
                    _field(counts.get("disabled")),
                ),
                "",
            ]
        elif status.get("reason"):
            out += [f"Reason: {_field(status['reason'])}", ""]
        items = status.get("items") or []
        if items:
            out += [
                "| Item | Kind | Execution | Priority | State | Attempt | Complete | Blocked by | Enabled | Scope | Measurement | Reason |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|",
            ]
            for item in items:
                out.append(
                    "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                        _coverage_field(item.get("title") or item.get("id")),
                        _coverage_field(item.get("kind")),
                        _coverage_field(item.get("execution_kind")),
                        _coverage_field(priority_text(item)),
                        _coverage_field(item.get("state")),
                        _coverage_field(item.get("attempt_status")),
                        _coverage_field(item.get("complete")),
                        _coverage_field(value_text(item.get("blocked_by"))),
                        _coverage_field(item.get("enabled")),
                        _coverage_field(value_text(item.get("scope"))),
                        _coverage_field(value_text(item.get("measurement"))),
                        _coverage_field(item.get("reason")),
                    )
                )
            out.append("")

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
        out += [f"- **{check_title(d.get('id'))}** — {d.get('reason')}" for d in disabled] + [""]

    failed = summary.get("tools_failed") or []
    if failed:
        out += [
            "## Unavailable checks",
            "",
            "These checks did not complete. Their silence does not mean no issues were found:",
            "",
        ]
        out += [f"- **{check_title(f.get('tool'))}** — {f.get('error')}" for f in failed] + [""]

    findings = document.get("findings") or []
    for level in ("critical", "warning", "notice"):
        chunk = [f for f in findings if f.get("severity") == level]
        if not chunk:
            continue
        out += [f"## {SEVERITY_TITLES.get(level, level)} — {len(chunk)}", ""]
        for finding in chunk:
            out.append(f"- **{finding.get('client_title', 'Audit finding')}**")
            observation = finding.get("client_observation")
            if observation:
                out.append(f"  - Observation: {observation}")
            out.append(f"  - Reproduction: {finding.get('client_reproduction', '')}")
            for detail in finding.get("client_details") or []:
                out.append(f"  - Evidence: {detail}")
            for location in finding.get("client_locations") or []:
                out.append(f"  - Location: {location}")
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
                    _field(page.get("url")),
                    _field(page.get("status")),
                    _field(page.get("title"), 80),
                    _field(page.get("words")),
                    _field(page.get("canonical"), 60),
                )
            )
        out.append("")

    note = summary.get("severity_note")
    if note:
        out += ["---", "", f"_{note}_", ""]

    path.write_text("\n".join(out), encoding="utf-8")


def write_stream(document: Any, path: pathlib.Path) -> None:
    """Stream findings and pages from audit.v2 without collecting rendered rows."""
    from seohead.reports import SEVERITY_TITLES
    from seohead.reports.client_findings import check_title
    from seohead.reports.evidence_summary import rows as evidence_rows

    summary = document.get("summary") or {}
    by_severity = summary.get("findings_by_severity") or {}
    with path.open("w", encoding="utf-8", newline="") as stream:

        def lines(*values: str) -> None:
            stream.write("\n".join(values) + "\n")

        lines(
            f"# SEO Audit: {document.get('domain', '')}",
            "",
            f"{document.get('url', '')} · Generated {document.get('generated_at', '')}",
            "",
        )
        if summary.get("crawl_valid") is False:
            lines(
                f"> **Crawl failed — no health score.** {summary.get('crawl_invalid_reason') or 'the crawl produced no usable data'}",
                "",
            )
        if summary.get("crawl_partial"):
            finish = summary.get("crawl_finish_reason")
            scope = summary.get("crawl_scope_note")
            bits = [part for part in (f"stopped: {finish}" if finish else None, scope) if part]
            lines(f"> **Partial crawl — scope is limited.** {'; '.join(bits)}", "")
        evidence = evidence_rows(summary)
        if evidence:
            lines(
                "## Saved evidence coverage",
                "",
                "| Kind | Measurement | State | Scope or reason |",
                "|---|---|---|---|",
            )
            for row in evidence:
                lines("| " + " | ".join(_coverage_field(value) for value in row) + " |")
            lines("")
        coverage = summary.get("project_coverage")
        if isinstance(coverage, dict):
            from seohead.reports.project_coverage import priority_text, value_text

            project = coverage.get("project") or {}
            status = coverage.get("status") or {}
            lines(
                "## Project checklist coverage",
                "",
                f"Project: {project.get('site', '')} · UUID: {project.get('uuid', '')}",
                f"Checklist state: {status.get('state', '')} · Revision: {status.get('revision', '')}",
                "",
            )
            counts = status.get("counts")
            if isinstance(counts, dict):
                lines(
                    "| Total | Complete | Remaining | Run | Not applicable | Not run | Stale | Disabled |",
                    "|---|---|---|---|---|---|---|---|",
                    "| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                        *(
                            _field(counts.get(key))
                            for key in (
                                "total",
                                "complete",
                                "remaining",
                                "run",
                                "not_applicable",
                                "not_run",
                                "stale",
                                "disabled",
                            )
                        )
                    ),
                    "",
                )
            for item in status.get("items") or []:
                lines(
                    f"- **{_coverage_field(item.get('title') or item.get('id'))}** — "
                    f"{_coverage_field(item.get('kind'))}; {_coverage_field(item.get('execution_kind'))}; "
                    f"{_coverage_field(priority_text(item))}; {_coverage_field(item.get('state'))}; "
                    f"{_coverage_field(item.get('attempt_status'))}; complete={_coverage_field(item.get('complete'))}; "
                    f"blocked by {_coverage_field(value_text(item.get('blocked_by')))}; "
                    f"{_coverage_field(item.get('reason'))}"
                )
            lines("")
        lines(
            "| Metric | Value |",
            "|---|---|",
            f"| Pages checked | {summary.get('pages_checked', 0)} |",
            f"| Critical findings | {by_severity.get('critical', 0)} |",
            f"| Warnings | {by_severity.get('warning', 0)} |",
            f"| Notices | {by_severity.get('notice', 0)} |",
            "",
        )
        disabled = summary.get("checks_disabled") or []
        if disabled:
            lines("## Disabled checks", "", "These checks were deliberately turned off:", "")
            for item in disabled:
                lines(f"- **{check_title(item.get('id'))}** — {item.get('reason')}")
            lines("")
        failed = summary.get("tools_failed") or []
        if failed:
            lines("## Unavailable checks", "", "These checks did not complete:", "")
            for item in failed:
                lines(f"- **{check_title(item.get('tool'))}** — {item.get('error')}")
            lines("")
        for level in ("critical", "warning", "notice"):
            count = by_severity.get(level, 0)
            if not count:
                continue
            lines(f"## {SEVERITY_TITLES.get(level, level)} — {count}", "")
            for finding in document.iter_findings(level):
                lines(f"- **{finding.get('client_title', 'Audit finding')}**")
                if observation := finding.get("client_observation"):
                    lines(f"  - Observation: {observation}")
                lines(f"  - Reproduction: {finding.get('client_reproduction', '')}")
                for detail in finding.get("client_details") or []:
                    lines(f"  - Evidence: {detail}")
                for location in finding.get("client_locations") or []:
                    lines(f"  - Location: {location}")
            lines("")
        if document.page_count:
            lines(
                "## Pages",
                "",
                "| URL | Status | Title | Words | Canonical |",
                "|---|---|---|---|---|",
            )
            for page in document.get("pages"):
                lines(
                    "| {} | {} | {} | {} | {} |".format(
                        _field(page.get("url")),
                        _field(page.get("status")),
                        _field(page.get("title"), 80),
                        _field(page.get("words")),
                        _field(page.get("canonical"), 60),
                    )
                )
