"""Normalized data model shared by every stage of the pipeline.

The model is deliberately source-agnostic: the rule engine never knows whether
the data came from a live Screaming Frog run (mode A) or from CSV/XLSX exports
parsed off disk (mode B). It only sees :class:`Page`, :class:`Link` and the
DataFrames the loader hands it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SEVERITIES: tuple[str, ...] = ("critical", "warning", "notice")


@dataclass
class Link:
    """A single link instance, as found in a ``*:Inlinks`` bulk export.

    Carries the localization that answers "where is this link, where does it go,
    where in the DOM": source page, anchor, semantic position and XPath path.
    """

    source_url: str | None = None
    destination_url: str | None = None
    anchor: str | None = None
    alt_text: str | None = None
    status_code: int | None = None
    link_position: str | None = None  # Navigation | Content | Sidebar | Footer
    link_path: str | None = None  # XPath from Screaming Frog
    follow: bool | None = None
    rel: str | None = None
    target: str | None = None

    def as_location(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "anchor": self.anchor,
            "alt_text": self.alt_text,
            "link_position": self.link_position,
            "link_path": self.link_path,
            "follow": self.follow,
            "rel": self.rel,
            "target": self.target,
        }


@dataclass
class Page:
    """One crawled URL with its normalized metrics.

    ``metrics`` holds typed, JSON-ready values (numbers as numbers, ``None`` for
    blanks). ``issues``/``issue_ids`` are filled in by the aggregator so each
    page links back to the issues that reference it.
    """

    url: str
    status_code: int | None = None
    status: str | None = None
    content_type: str | None = None
    indexability: str | None = None
    indexability_status: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    issue_ids: list[str] = field(default_factory=list)
    suppressed_issue_ids: list[str] = field(default_factory=list)

    @property
    def is_html(self) -> bool:
        return "html" in (self.content_type or "").lower()

    @property
    def is_2xx(self) -> bool:
        """Named separately from ``is_html`` (issue #133): a 301 or 404 answers with an
        HTML ``Content-Type`` just as often as a real page does, and ``AuditContext.html_pages``
        is the one place that distinction has to be made — ``is_html`` alone stays a pure
        Content-Type read, matching its counterpart in seohead.crawl.collect.PageRecord.
        """
        return self.status_code is not None and 200 <= int(self.status_code) < 300

    @property
    def is_indexable(self) -> bool:
        return (self.indexability or "").strip().lower() == "indexable"

    def to_json(self) -> dict[str, Any]:
        out = {
            "url": self.url,
            "status_code": self.status_code,
            "indexability": self.indexability,
            "indexability_status": self.indexability_status,
            "content_type": self.content_type,
            "metrics": self.metrics,
            "issues": self.issues,
            "issue_ids": self.issue_ids,
        }
        if self.suppressed_issue_ids:
            out["suppressed_issue_ids"] = self.suppressed_issue_ids
        return out


@dataclass
class Issue:
    """A single detected problem, localized and traceable to its source export."""

    check: str
    severity: str
    source: str
    message: str
    id: str | None = None
    fingerprint: str | None = None  # stable hash for diffing runs
    target_url: str | None = None
    status_code: int | None = None
    occurrences_count: int = 1
    locations: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    group_id: str | None = None
    fix_hint: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "check": self.check,
            "severity": self.severity,
            "source": self.source,
            "message": self.message,
            "target_url": self.target_url,
            "status_code": self.status_code,
            "occurrences_count": self.occurrences_count,
        }
        if self.locations:
            out["locations"] = self.locations
        if self.details:
            out["details"] = self.details
        if self.group_id:
            out["group_id"] = self.group_id
        if self.fix_hint:
            out["fix_hint"] = self.fix_hint
        if self.evidence:
            out["evidence"] = self.evidence
        return out


@dataclass
class Group:
    """A cluster of URLs sharing a value (duplicate title/description/hash)."""

    group_id: str
    check: str
    value: str | None
    urls: list[str]
    count: int

    def to_json(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "check": self.check,
            "value": self.value,
            "urls": self.urls,
            "count": self.count,
        }


@dataclass
class SkippedCheck:
    id: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "reason": self.reason}


@dataclass
class AuditResult:
    """The full audit, the single contract both reporters consume."""

    run: dict[str, Any]
    summary: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    skipped: list[SkippedCheck] = field(default_factory=list)
    disabled: list[SkippedCheck] = field(default_factory=list)
    suppressed_issues: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        from .. import __version__

        run = dict(self.run)
        run["checks_skipped"] = [s.to_json() for s in self.skipped]
        run["checks_disabled"] = [d.to_json() for d in self.disabled]
        document = {
            "schema_version": "2.0",
            "tool": {
                "name": "SF Analyzer",
                "version": __version__,
                "generated_by": "SEOHead",
            },
            "run": run,
            "summary": self.summary,
            "issues": [i.to_json() for i in self.issues],
            "pages": [p.to_json() for p in self.pages],
            "groups": [g.to_json() for g in self.groups],
        }
        if self.suppressed_issues:
            document["suppressed_issues"] = self.suppressed_issues
        return document
