"""Excel report with Summary, Findings, Pages, and Technologies worksheets.

The chart is built with Excel primitives through openpyxl rather than embedded as
a static matplotlib image. It therefore remains editable and tied to worksheet
data, while the generated workbook avoids an unnecessary plotting dependency.
"""

from __future__ import annotations

import pathlib
from typing import Any

_HEAD = {"critical": "C00000", "warning": "BF8F00", "notice": "808080"}
_MAX_SUPPRESSED_FINDINGS = 10000


def _style_header(ws, row: int = 1) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    fill = PatternFill("solid", fgColor="1F3864")
    for cell in ws[row]:
        if cell.value is None:
            continue
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    # Use a string coordinate rather than ``ws.cell()``. Accessing a cell here
    # materializes a row, so the next ``append`` would skip one and leave a blank
    # row immediately below the header.
    ws.freeze_panes = f"A{row + 1}"


def _autofit(ws, limits: dict[int, int] | None = None) -> None:
    """Fit columns to content while capping widths for readable long-URL tables."""
    from openpyxl.utils import get_column_letter

    limits = limits or {}
    for idx, column in enumerate(ws.columns, start=1):
        longest = max((len(str(c.value)) for c in column if c.value is not None), default=0)
        ws.column_dimensions[get_column_letter(idx)].width = min(
            max(longest + 2, 10), limits.get(idx, 60)
        )


def write(document: dict[str, Any], path: pathlib.Path) -> None:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    from seohead.reports import checks_completed_display, neutralize_formula
    from seohead.reports.client_findings import (
        check_title,
        finding_exclusion_report,
        finding_view_columns,
        finding_view_label,
        finding_view_notice,
    )

    wb = Workbook()
    summary = document.get("summary") or {}
    by_sev = summary.get("findings_by_severity") or {}

    # -- Summary -------------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = f"SEO Audit: {document.get('domain', '')}"
    ws["A1"].font = Font(bold=True, size=16)
    ws["A2"] = document.get("url", "")
    ws["A3"] = f"Generated: {document.get('generated_at', '')}"

    # Failed/partial crawl scope and disabled-check evidence must be visible
    # before the metrics and severity counts below, not appended as a
    # trailing note: a recipient who never scrolls past the numbers must
    # still be unable to mistake a failed or sampled crawl for a clean,
    # site-wide audit, and a deliberately disabled check for one that ran
    # clean (#361).
    scope_rows: list[str] = []
    if summary.get("crawl_valid") is False:
        reason = summary.get("crawl_invalid_reason") or "the crawl produced no usable data"
        scope_rows.append(f"Crawl failed -- no health score. {reason}")
    if summary.get("crawl_partial"):
        finish = summary.get("crawl_finish_reason")
        scope = summary.get("crawl_scope_note")
        bits = [b for b in (f"stopped: {finish}" if finish else None, scope) if b]
        scope_rows.append(
            "Partial crawl -- scope is limited." + (f" {'; '.join(bits)}" if bits else "")
        )
    for item in summary.get("checks_disabled") or []:
        scope_rows.append(f"Disabled check {check_title(item.get('id'))} -- {item.get('reason')}")
    if notice := finding_view_notice(summary):
        scope_rows.append(notice)
    exclusions = finding_exclusion_report(summary, document.get("suppressed_issues"))
    if exclusions is not None:
        count = exclusions["suppressed_total"]
        finding_label = "finding" if count == 1 else "findings"
        occurrences = exclusions["suppressed_occurrences"]
        occurrence_label = "occurrence" if occurrences == 1 else "occurrences"
        rule_count = exclusions["rules_configured"]
        rule_label = "rule" if rule_count == 1 else "rules"
        scope_rows.append(
            f"Finding exclusions: {count} {finding_label} and {occurrences} {occurrence_label} "
            f"suppressed by {rule_count} configured URL {rule_label}; see Finding Exclusions."
        )

    row = 4
    for text in scope_rows:
        cell = ws.cell(row=row, column=1, value=text)
        cell.font = Font(bold=True, color="C00000")
        row += 1
    offset = row - 4

    rows = [
        ("Pages checked", summary.get("pages_checked", 0)),
        ("Total findings", summary.get("findings_total", 0)),
        ("Critical findings", by_sev.get("critical", 0)),
        ("Warnings", by_sev.get("warning", 0)),
        ("Notices", by_sev.get("notice", 0)),
        ("Checks completed", checks_completed_display(summary)),
        ("Checks unavailable", len(summary.get("tools_failed") or [])),
    ]
    header_row = 5 + offset
    ws.cell(row=header_row, column=1, value="Metric")
    ws.cell(row=header_row, column=2, value="Value")
    _style_header(ws, header_row)
    for i, (name, value) in enumerate(rows, start=header_row + 1):
        ws.cell(row=i, column=1, value=name)
        ws.cell(row=i, column=2, value=value)

    # The three severity bars expose the issue distribution at a glance.
    sev_start = header_row + 3  # Critical findings is the 3rd metric row
    chart = BarChart()
    chart.title = "Findings by Severity"
    chart.y_axis.title = "Count"
    chart.add_data(
        Reference(ws, min_col=2, min_row=sev_start, max_row=sev_start + 2), titles_from_data=False
    )
    chart.set_categories(Reference(ws, min_col=1, min_row=sev_start, max_row=sev_start + 2))
    chart.legend = None
    chart.height, chart.width = 7, 12
    ws.add_chart(chart, f"D{header_row}")

    failed = summary.get("tools_failed") or []
    if failed:
        start = header_row + 1 + len(rows) + 1
        ws.cell(row=start, column=1, value="Unavailable checks -- evidence is absent from report")
        ws.cell(row=start, column=1).font = Font(bold=True, color="C00000")
        for i, item in enumerate(failed, start=start + 1):
            ws.cell(row=i, column=1, value=check_title(item.get("tool")))
            ws.cell(row=i, column=2, value=item.get("error"))
    note = summary.get("severity_note")
    if note:
        ws.cell(row=header_row + 1 + len(rows) + len(failed) + 3, column=1, value=note).font = Font(
            italic=True, size=9, color="808080"
        )
    _autofit(ws, {2: 40})

    if exclusions is not None:
        ws = wb.create_sheet("Finding Exclusions")
        ws.append(["Rule", "Pattern", "Checks", "Suppressed findings", "Occurrences", "Reason"])
        _style_header(ws)
        for rule in exclusions["rules"]:
            ws.append(
                [
                    neutralize_formula(rule["id"]),
                    neutralize_formula(rule["pattern"]),
                    neutralize_formula(", ".join(rule["checks"]) or "all checks"),
                    rule["suppressed_findings"],
                    rule["suppressed_occurrences"],
                    neutralize_formula(rule["reason"]),
                ]
            )
        _autofit(ws)

        suppressed = exclusions["issues"]
        if suppressed:
            ws = wb.create_sheet("Suppressed Findings")
            ws.append(["Issue ID", "Check", "Severity", "URL", "Occurrences", "Rule", "Reason"])
            _style_header(ws)
            for issue in suppressed[:_MAX_SUPPRESSED_FINDINGS]:
                issue = issue if isinstance(issue, dict) else {}
                marker = issue.get("suppression")
                marker = marker if isinstance(marker, dict) else {}
                ws.append(
                    [
                        neutralize_formula(issue.get("id", "")),
                        neutralize_formula(check_title(issue.get("check"))),
                        neutralize_formula(issue.get("severity", "")),
                        neutralize_formula(issue.get("target_url", "")),
                        issue.get("occurrences_count", ""),
                        neutralize_formula(marker.get("rule_id", "")),
                        neutralize_formula(marker.get("reason", "")),
                    ]
                )
            if len(suppressed) > _MAX_SUPPRESSED_FINDINGS:
                ws.append(
                    [
                        f"Showing {_MAX_SUPPRESSED_FINDINGS} of {len(suppressed)}; see source audit JSON for all records"
                    ]
                )
            _autofit(ws)

    # -- Findings ------------------------------------------------------------
    # This sheet is the documented developer handoff for a Screaming Frog
    # audit (docs/scenarios/broken-pages.md): Check/Status/Occurrences/
    # Locations/Fix Hint are the evidence a BROKEN_INTERNAL_LINK finding
    # carries beyond its message, and dropping them here forced the reader
    # back to raw audit.json (#220).
    ws = wb.create_sheet("Findings")
    view_columns = finding_view_columns(summary)
    if view_columns is not None:
        ws.append([finding_view_label(column) for column in view_columns])
    else:
        ws.append(
            [
                "Severity",
                "URL",
                "Finding",
                "Observation",
                "Reproduction",
                "Status",
                "Occurrences",
                "Evidence",
                "Locations",
                "Fix Hint",
            ]
        )
    _style_header(ws)
    from seohead.reports import SEVERITY_TITLES

    for finding in document.get("findings") or []:
        if view_columns is not None:
            values = finding.get("view_fields") or {}
            ws.append([neutralize_formula(values.get(column, "")) for column in view_columns])
            severity_column = (
                view_columns.index("severity") + 1 if "severity" in view_columns else None
            )
        else:
            ws.append(
                [
                    SEVERITY_TITLES.get(finding.get("severity"), finding.get("severity")),
                    neutralize_formula(finding.get("url", "")),
                    neutralize_formula(finding.get("client_title", "Audit finding")),
                    neutralize_formula(finding.get("client_observation", "")),
                    neutralize_formula(finding.get("client_reproduction", "")),
                    finding.get("status_code", ""),
                    finding.get("occurrences_count", ""),
                    neutralize_formula("; ".join(finding.get("client_details") or [])),
                    neutralize_formula("; ".join(finding.get("client_locations") or [])),
                    neutralize_formula(finding.get("fix_hint", "")),
                ]
            )
            severity_column = 1
        colour = _HEAD.get(finding.get("severity"))
        if colour and severity_column:
            ws.cell(row=ws.max_row, column=severity_column).font = Font(bold=True, color=colour)
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(view_columns) if view_columns is not None else 10)}{ws.max_row}"
    _autofit(ws, {4: 100, 8: 100, 9: 60} if view_columns is None else {})

    # -- Pages ---------------------------------------------------------------
    pages = document.get("pages") or []
    ws = wb.create_sheet("Pages")
    # description_length is part of the site-audit page contract (audit.site
    # emits it, csvfile.py already writes it) -- this sheet was the one place
    # it was silently dropped, making the XLSX working file unusable for the
    # meta-description-length scenario it is supposed to cover (#225).
    columns = [
        "url",
        "status",
        "title",
        "title_length",
        "description_length",
        "h1",
        "canonical",
        "words",
        "schema_types",
        "schema_errors",
        "social_missing",
    ]
    titles = [
        "URL",
        "Status",
        "Title",
        "Title Length",
        "Description Length",
        "H1",
        "Canonical",
        "Words",
        "Schema Types",
        "Schema Errors",
        "Missing Social Tags",
    ]
    ws.append(titles)
    _style_header(ws)
    for page in pages:
        ws.append([neutralize_formula(page.get(c, "")) for c in columns])
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{ws.max_row}"
    _autofit(ws, {1: 70, 3: 60, 6: 40})  # URL, Title, H1 -- H1 shifted by the new column

    # -- Technologies and infrastructure ------------------------------------
    ws = wb.create_sheet("Technologies")
    ws.append(["Category", "Detected Technology", "Evidence"])
    _style_header(ws)
    tech = (document.get("site") or {}).get("tech_detect") or {}
    for item in tech.get("technologies") or []:
        ws.append(
            [
                neutralize_formula(item.get("category", "")),
                neutralize_formula(item.get("name", "")),
                neutralize_formula(item.get("evidence", "")),
            ]
        )
    registration = ((document.get("site") or {}).get("domain_profile") or {}).get(
        "registration"
    ) or {}
    if registration:
        ws.append([])
        ws.append(["domain", "registrar", neutralize_formula(registration.get("registrar", ""))])
        ws.append(["domain", "created", neutralize_formula(registration.get("created", ""))])
        ws.append(["domain", "expires", neutralize_formula(registration.get("expires", ""))])
        ws.append(["domain", "age in years", registration.get("age_years", "")])
    _autofit(ws, {3: 70})

    # -- Project coverage ----------------------------------------------------
    coverage = summary.get("project_coverage")
    if isinstance(coverage, dict):
        from seohead.reports.project_coverage import value_text

        project = coverage.get("project") or {}
        checklist = coverage.get("status") or {}
        ws = wb.create_sheet("Project Coverage")
        ws.append(["Project", neutralize_formula(project.get("site", ""))])
        ws.append(["Project UUID", neutralize_formula(project.get("uuid", ""))])
        ws.append(["Checklist state", neutralize_formula(checklist.get("state", ""))])
        ws.append(["Revision", checklist.get("revision", "")])
        ws.append(["Counts", neutralize_formula(value_text(checklist.get("counts")))])
        ws.append([])
        ws.append(
            [
                "Item ID",
                "Item",
                "Kind",
                "Execution",
                "Priority",
                "Priority origin",
                "Priority reason",
                "State",
                "Attempt",
                "Complete",
                "Blocked by",
                "Enabled",
                "Stale",
                "Scope",
                "Measurement",
                "Reason",
            ]
        )
        _style_header(ws, 7)
        for item in checklist.get("items") or []:
            if not isinstance(item, dict):
                continue
            ws.append(
                [
                    neutralize_formula(item.get("id", "")),
                    neutralize_formula(item.get("title", "")),
                    neutralize_formula(item.get("kind", "")),
                    neutralize_formula(item.get("execution_kind", "")),
                    neutralize_formula(item.get("priority", "")),
                    neutralize_formula(item.get("priority_origin", "")),
                    neutralize_formula(item.get("priority_reason", "")),
                    neutralize_formula(item.get("state", "")),
                    neutralize_formula(item.get("attempt_status", "")),
                    item.get("complete", ""),
                    neutralize_formula(value_text(item.get("blocked_by"))),
                    item.get("enabled", ""),
                    item.get("stale", ""),
                    neutralize_formula(value_text(item.get("scope"))),
                    neutralize_formula(value_text(item.get("measurement"))),
                    neutralize_formula(item.get("reason", checklist.get("reason", ""))),
                ]
            )
        if ws.max_row > 7:
            ws.auto_filter.ref = f"A7:P{ws.max_row}"
        _autofit(ws, {2: 45, 7: 60, 14: 70, 15: 70, 16: 70})

    from seohead.reports.evidence_summary import rows as evidence_rows

    evidence = evidence_rows(summary)
    if evidence:
        evidence_sheet = wb.create_sheet("Evidence coverage")
        evidence_sheet.append(["Kind", "Measurement", "State", "Scope or reason"])
        for row in evidence:
            evidence_sheet.append([neutralize_formula(value) for value in row])
        evidence_sheet.freeze_panes = "A2"
        evidence_sheet.auto_filter.ref = evidence_sheet.dimensions

    wb.save(path)
