"""Write flat CSV records for a task tracker or downstream database.

One file represents one entity, so the renderer writes three adjacent files:
``<name>.csv`` contains findings, ``<name>.pages.csv`` contains page facts, and
``<name>.scope.csv`` contains run-evidence caveats. Mixing
different entities into a single table produces an ambiguous file that is difficult
or impossible to import reliably.

Named ``csvfile`` rather than ``csv``: a module named ``csv.py`` next to code that does
``import csv`` for the standard library would shadow it. This is deliberate, not an
inconsistency to align with the other format modules in this package (see docs/NAMING.md).
"""

from __future__ import annotations

import csv
import pathlib
from typing import Any


def _scope_rows(summary: dict[str, Any]) -> list[list[Any]]:
    """Return run evidence separately from task-tracker finding rows (#574)."""
    from seohead.reports.client_findings import check_title

    rows: list[list[Any]] = []
    if summary.get("crawl_valid") is False:
        rows.append(
            [
                "crawl",
                "validity",
                "failed",
                summary.get("crawl_invalid_reason") or "the crawl produced no usable data",
            ]
        )
    if summary.get("crawl_partial"):
        bits = []
        if finish := summary.get("crawl_finish_reason"):
            bits.append(f"stopped: {finish}")
        if scope := summary.get("crawl_scope_note"):
            bits.append(scope)
        rows.append(["crawl", "scope", "partial", "; ".join(bits)])
    for item in summary.get("checks_disabled") or []:
        rows.append(["check", check_title(item.get("id")), "disabled", item.get("reason", "")])
    for item in summary.get("tools_failed") or []:
        rows.append(["check", check_title(item.get("tool")), "unavailable", item.get("error", "")])
    return rows


def _write_project_coverage(summary: dict[str, Any], path: pathlib.Path) -> None:
    coverage = summary.get("project_coverage")
    if not isinstance(coverage, dict):
        return
    from seohead.reports import neutralize_formula
    from seohead.reports.project_coverage import value_text

    project = coverage.get("project") or {}
    status = coverage.get("status") or {}
    destination = path.with_suffix(".coverage.csv")
    with destination.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(
            [
                "Project UUID",
                "Project site",
                "Checklist revision",
                "Checklist state",
                "Counts",
                "Item ID",
                "Item",
                "Kind",
                "Execution",
                "State",
                "Attempt",
                "Enabled",
                "Stale",
                "Scope",
                "Measurement",
                "Reason",
            ]
        )
        items = status.get("items") or [None]
        for item in items:
            item = item if isinstance(item, dict) else {}
            writer.writerow(
                [
                    neutralize_formula(project.get("uuid", "")),
                    neutralize_formula(project.get("site", "")),
                    status.get("revision", ""),
                    neutralize_formula(status.get("state", "")),
                    neutralize_formula(value_text(status.get("counts"))),
                    neutralize_formula(item.get("id", "")),
                    neutralize_formula(item.get("title", "")),
                    neutralize_formula(item.get("kind", "")),
                    neutralize_formula(item.get("execution_kind", "")),
                    neutralize_formula(item.get("state", "")),
                    neutralize_formula(item.get("attempt_status", "")),
                    item.get("enabled", ""),
                    item.get("stale", ""),
                    neutralize_formula(value_text(item.get("scope"))),
                    neutralize_formula(value_text(item.get("measurement"))),
                    neutralize_formula(item.get("reason", status.get("reason", ""))),
                ]
            )


def write(document: dict[str, Any], path: pathlib.Path) -> None:
    from seohead.reports import SEVERITY_TITLES, neutralize_formula

    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        # ``utf-8-sig`` includes a BOM so Excel detects UTF-8 instead of corrupting
        # multilingual URLs, titles, and finding evidence when the file is opened.
        writer = csv.writer(fh, delimiter=";")
        # A task tracker importing this file needs the same evidence the
        # documented developer handoff promises (docs/scenarios/broken-pages.md):
        # which check fired, the status code, how many occurrences, every
        # linking location, and the fix hint (#220).
        writer.writerow(
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
        for finding in document.get("findings") or []:
            writer.writerow(
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

    scope_rows = _scope_rows(document.get("summary") or {})
    scope_path = path.with_suffix(".scope.csv")
    with scope_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["Evidence type", "Identifier", "Status", "Reason"])
        for row in scope_rows:
            writer.writerow([neutralize_formula(value) for value in row])

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
    pages_path = path.with_suffix(".pages.csv")
    with pages_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(columns)
        for page in document.get("pages") or []:
            writer.writerow([neutralize_formula(page.get(c, "")) for c in columns])

    _write_project_coverage(document.get("summary") or {}, path)
