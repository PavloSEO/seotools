"""Write flat CSV records for a task tracker or downstream database.

One file represents one entity, so the renderer writes adjacent files:
``<name>.csv`` contains findings, ``<name>.pages.csv`` contains page facts when
they exist, and ``<name>.scope.csv`` contains run-evidence caveats. Mixing
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
        rows.append(["check", item.get("id", ""), "disabled", item.get("reason", "")])
    for item in summary.get("tools_failed") or []:
        rows.append(["check", item.get("tool", ""), "unavailable", item.get("error", "")])
    return rows


def write(document: dict[str, Any], path: pathlib.Path) -> None:
    from seohead.reports import SEVERITY_TITLES, format_locations, neutralize_formula

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
                "Source",
                "URL",
                "Finding",
                "Check",
                "Status",
                "Occurrences",
                "Locations",
                "Fix Hint",
            ]
        )
        for finding in document.get("findings") or []:
            writer.writerow(
                [
                    SEVERITY_TITLES.get(finding.get("severity"), finding.get("severity")),
                    neutralize_formula(finding.get("source", "")),
                    neutralize_formula(finding.get("url", "")),
                    neutralize_formula(finding.get("text", "")),
                    finding.get("check", ""),
                    finding.get("status_code", ""),
                    finding.get("occurrences_count", ""),
                    neutralize_formula(format_locations(finding.get("locations"))),
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

    pages = document.get("pages") or []
    if pages:
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
            for page in pages:
                writer.writerow([neutralize_formula(page.get(c, "")) for c in columns])
