"""Build Excel, Word, CSV, Markdown, or JSON reports from one audit document.

This package is the formatting boundary. The four human-facing writers
(``xlsx``, ``docx``, ``csv``, ``md``) only ever read one shape: flat
``findings``/``pages`` records, the ``seohead.site-audit/1`` contract. A
Screaming Frog audit (``sf run``'s ``audit.json`` — findings under ``issues``,
page facts nested under ``pages[].metrics``) is a second, equally real input;
:func:`_normalize_sf_audit` reshapes it into the same flat contract once, here,
so no writer has to know two schemas. ``json`` is a *reshaping* exception: a
recognized document's ``json`` output passes through untouched, on either
contract, because it is not reinterpreting the data, only relaying it. It is
not a *recognition* exception: an unrecognized or wrong-version document is
refused for ``json`` exactly as for the other four formats (#338) -- relaying
a document this package has not reviewed would still hand the caller a
plausible-looking file for a contract these writers do not understand.

A document that matches neither contract, or declares a schema/schema_version
marker this package does not support, is refused rather than rendered: see
:func:`_detect_kind`. Report generators do not calculate metrics and do not
make network requests: if a value was not captured in the audit JSON, it must
remain absent rather than being invented during rendering.

Formats are separated by operational purpose, not personal preference:

``xlsx``  tables, filters, and a live chart for sorting, triage, and assigning
          individual findings to developers
``docx``  a narrative document with headings for client review and approval
``csv``   flat records for importing into a task tracker or another database
``md``    a portable report for editors, repositories, and correspondence
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from seohead.audit.site import SCHEMA as _SITE_AUDIT_SCHEMA

FORMATS = ("xlsx", "docx", "csv", "md", "json")

# The SF Analyzer audit.json contract's own version marker (seohead/sf/core/models.py
# AuditResult.to_json). Only this exact value is accepted: a document declaring any
# other schema_version has not been reviewed against these writers and must be
# refused rather than rendered as if it matched (#338).
_SF_AUDIT_SCHEMA_VERSION = "2.0"

SEVERITY_TITLES = {
    "critical": "Critical",
    "warning": "Warning",
    "notice": "Notice",
}

# Characters that Excel, Google Sheets, and most spreadsheet importers treat as
# the start of a formula. Tab and CR are included because both `csv.writer`
# and openpyxl pass them through as literal leading bytes, and some importers
# apply the same formula rule to them as to `=`/`+`/`-`/`@`.
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")


def neutralize_formula(value: Any) -> Any:
    """Defuse CSV/XLSX formula injection (CWE-1236) in one cell value.

    A finding's text or a page's title/H1/canonical is copied verbatim from a
    crawled site (see :func:`seohead.tools.parser.document_title`) into the
    XLSX and CSV reports, so the audited site — not this tool's operator —
    controls the string. Excel and Google Sheets evaluate any cell starting
    with `=`, `+`, `-`, or `@` as a formula, which turns a page title into a
    live `HYPERLINK(...)` exfiltration or a legacy DDE payload the moment the
    report is opened. Prefixing a leading apostrophe is the standard
    neutralizer: the string no longer starts with a formula-leading
    character, so both `csv.writer` and openpyxl's own type autodetection
    write it as plain text.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_LEADS):
        return "'" + value
    return value


def format_locations(locations: Any) -> str:
    """Flatten a finding's ``locations`` list into one reviewable cell.

    xlsx and csv are the tabular "working output" the broken-pages scenario
    hands to a developer (docs/scenarios/broken-pages.md), so each source
    page, its anchor, its position on the page and its XPath must survive
    into one column rather than requiring the reader to open the raw
    audit.json (#220).
    """
    if not isinstance(locations, list):
        return ""
    rows = []
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        bits = [
            str(loc[key])
            for key in ("source_url", "anchor", "link_position", "link_path")
            if loc.get(key)
        ]
        if bits:
            rows.append(" · ".join(bits))
    return "; ".join(rows)


def checks_completed_display(summary: dict[str, Any]) -> int | str:
    """Return the "checks completed" value for a normalized report summary.

    ``tools_run`` is the one place either accepted contract names the checks
    that actually executed. A source that cannot supply that inventory
    (``None``) must say so rather than showing a count of zero, which would
    read as "no checks ran" instead of the true "not measured" (#337).
    """
    tools_run = summary.get("tools_run")
    if tools_run is None:
        return "not reported"
    return len(tools_run)


def _load(data: Any) -> dict[str, Any]:
    """Load an audit document from a mapping or a JSON file path."""
    if isinstance(data, dict):
        return data
    path = pathlib.Path(str(data))
    if not path.exists():
        raise FileNotFoundError(f"audit file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _detect_kind(document: dict[str, Any]) -> tuple[str | None, str | None]:
    """Identify which of the two audit contracts ``document`` matches.

    ``findings`` (even empty) is the shape of ``seohead.site-audit/1``, written
    by :mod:`seohead.audit.site` and by every fixture in this package's own
    tests. ``issues`` + ``pages`` with no ``findings`` is the shape of an SF
    Analyzer ``audit.json`` (:mod:`seohead.sf.core.models`). Matching the shape
    is not enough on its own (#338): each contract also declares an explicit
    version marker (``schema`` / ``schema_version``), and only the exact value
    this package's writers were reviewed against is accepted -- a document
    from a future, incompatible revision of either producer must be refused,
    not rendered as a plausible-looking but incomplete report. This gate
    applies uniformly to every output format, ``json`` included: a document
    this package cannot recognize is refused rather than relayed, even though
    a recognized document's ``json`` output is otherwise an untouched copy.

    Returns ``(kind, error)``. ``kind`` is ``None`` when the document must be
    refused; ``error`` then names the offending or missing marker so the
    caller's message is specific rather than "not recognized". Neither shape
    matching means this is not a document either side of this package ever
    produces, and ``error`` is ``None`` in that case -- the generic message in
    :func:`build_report` already names the unrecognized top-level keys (#151).
    """
    if "findings" in document or "schema" in document:
        marker = document.get("schema")
        if marker != _SITE_AUDIT_SCHEMA:
            return None, (
                f"unsupported or missing 'schema' marker {marker!r}; "
                f"site-audit reports require exactly {_SITE_AUDIT_SCHEMA!r}"
            )
        invalid = [
            name
            for name, expected in (("findings", list), ("pages", list), ("summary", dict))
            if not isinstance(document.get(name), expected)
        ]
        if invalid:
            return None, f"site-audit has invalid or missing container(s): {', '.join(invalid)}"
        return "site-audit", None
    if "issues" in document or "schema_version" in document:
        marker = document.get("schema_version")
        if marker != _SF_AUDIT_SCHEMA_VERSION:
            return None, (
                f"unsupported or missing 'schema_version' marker {marker!r}; "
                f"SF Analyzer audits require exactly {_SF_AUDIT_SCHEMA_VERSION!r}"
            )
        invalid = [
            name
            for name, expected in (
                ("run", dict),
                ("summary", dict),
                ("issues", list),
                ("pages", list),
                ("groups", list),
            )
            if not isinstance(document.get(name), expected)
        ]
        if invalid:
            return (
                None,
                f"SF Analyzer audit has invalid or missing container(s): {', '.join(invalid)}",
            )
        return "sf-audit", None
    return None, None


def _normalize_sf_audit(document: dict[str, Any]) -> dict[str, Any]:
    """Reshape an SF Analyzer ``audit.json`` into the flat site-audit contract.

    Every field copied here is real evidence already present in ``document``;
    none is computed or guessed, matching the module contract above.
    """
    run = document.get("run") or {}
    summary = document.get("summary") or {}
    totals = summary.get("totals") or {}
    by_severity = summary.get("by_severity") or {}

    # The four human-facing writers key their narrow ("severity", "source",
    # "url", "text") table columns off these exact names, but a
    # BROKEN_INTERNAL_LINK finding is not actionable without status_code,
    # occurrences_count, locations and fix_hint too: docs/scenarios/broken-pages.md
    # promises the developer handoff carries all of it, so every field the SF
    # issue actually has is preserved here even where a given writer does not
    # yet render it (#220).
    findings = [
        {
            "severity": issue.get("severity"),
            "source": issue.get("source", ""),
            "url": issue.get("target_url") or "",
            "text": issue.get("message", ""),
            "check": issue.get("check", ""),
            "status_code": issue.get("status_code"),
            "occurrences_count": issue.get("occurrences_count"),
            "fix_hint": issue.get("fix_hint") or "",
            "locations": issue.get("locations") or [],
            "details": issue.get("details") or {},
        }
        for issue in document.get("issues") or []
    ]

    pages = []
    for page in document.get("pages") or []:
        metrics = page.get("metrics") or {}
        h1 = metrics.get("h1")
        pages.append(
            {
                "url": page.get("url", ""),
                "status": page.get("status_code"),
                "title": metrics.get("title") or "",
                "title_length": metrics.get("title_length") or "",
                "description_length": metrics.get("desc_length") or "",
                "h1": (h1[0] if isinstance(h1, list) and h1 else h1) or "",
                "canonical": metrics.get("canonical") or "",
                "words": metrics.get("word_count") or 0,
                # Schema.org and social-tag evidence live in the issue stream
                # for this contract, not in a per-page metric column; left
                # absent here rather than invented.
                "schema_types": "",
                "schema_errors": "",
                "social_missing": "",
            }
        )

    tools_failed = [
        {"tool": item.get("id"), "error": item.get("reason")}
        for item in run.get("checks_skipped") or []
    ]
    # A deliberately disabled check is an operator choice, not missing evidence,
    # but it must stay visible and distinct from `tools_failed` (checks the
    # source tried and could not run) -- collapsing the two would let a
    # disabled BROKEN_PAGE_4XX read as a check that ran clean (#361).
    checks_disabled = [
        {"id": item.get("id"), "reason": item.get("reason")}
        for item in run.get("checks_disabled") or []
    ]

    # `by_check` names only the checks that found something; a check that ran
    # and found nothing is invisible there, so its key count is "checks with
    # findings", never "checks completed" (#337). `check_coverage`, when the
    # source supplies it, also names the checks that ran silently
    # (`checks_silent_ids`); fired-with-findings plus silent-but-run is the
    # actual completed-check inventory. `tools_run` already means exactly that
    # in the site-audit contract (the handler names that ran) -- giving it the
    # same one meaning here, instead of overloading it with a findings count,
    # is what "one documented meaning per accepted contract" requires. Without
    # `check_coverage` there is no source evidence for what completed, so
    # `tools_run` stays `None` (unavailable) rather than understating to zero.
    check_coverage = summary.get("check_coverage") or None
    tools_run = (
        sorted(
            set((summary.get("by_check") or {}).keys())
            | set(check_coverage.get("checks_silent_ids") or [])
        )
        if check_coverage
        else None
    )

    # A basis note about reduced check coverage is still useful context, but
    # the crawl-invalid and sitemap-scope reasons now have their own explicit
    # fields below and must not also flow through this generic trailing note,
    # which would show the same fact twice.
    severity_note = summary.get("health_score_basis")

    crawl_valid = run.get("crawl_valid")
    crawl_valid = True if crawl_valid is None else bool(crawl_valid)

    return {
        "domain": run.get("project") or "",
        "url": run.get("source") or (pages[0]["url"] if pages else ""),
        "generated_at": run.get("generated_at", ""),
        "findings": findings,
        "pages": pages,
        "summary": {
            "pages_checked": totals.get("urls_crawled", len(pages)),
            "findings_total": totals.get("issues_total", len(findings)),
            "findings_by_severity": {
                "critical": by_severity.get("critical", 0),
                "warning": by_severity.get("warning", 0),
                "notice": by_severity.get("notice", 0),
            },
            "tools_run": tools_run,
            "tools_failed": tools_failed,
            "checks_disabled": checks_disabled,
            # Whether the crawl produced usable data at all, and why not --
            # a recipient must not mistake a failed run for a site-wide audit
            # that simply found nothing (#361).
            "crawl_valid": crawl_valid,
            "crawl_invalid_reason": (
                None
                if crawl_valid
                else (run.get("crawl_invalid_reason") or summary.get("health_score_reason"))
            ),
            # Whether the crawl covered only a subset, and of what -- a
            # recipient must not mistake a sampled crawl for the whole site.
            "crawl_partial": bool(run.get("crawl_partial")),
            "crawl_finish_reason": run.get("crawl_finish_reason")
            or run.get("crawl_stopped_reason"),
            "crawl_scope_note": summary.get("health_score_scope"),
            "severity_note": severity_note,
        },
    }


def build_report(data: Any, fmt: str = "xlsx", path: str | None = None) -> dict[str, Any]:
    """Render an audit document in the requested report format.

    ``data`` is either the audit mapping itself or the path to its JSON file.
    ``path`` selects the output location; when omitted, the name is derived from
    the audited domain and the format.
    """
    fmt = (fmt or "xlsx").lower().lstrip(".")
    if fmt not in FORMATS:
        return {
            "ok": False,
            "error": f"report format {fmt!r} is not supported; "
            f"available formats: {', '.join(FORMATS)}",
        }
    try:
        document = _load(data)
    except (FileNotFoundError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}
    if not isinstance(document, dict):
        return {
            "ok": False,
            "error": f"audit document must be a JSON object, got {type(document).__name__}",
        }

    kind, marker_error = _detect_kind(document)
    if kind is None:
        return {
            "ok": False,
            "error": marker_error
            or (
                "audit document schema not recognized: expected 'findings' "
                "(seohead.site-audit/1) or 'issues'+'pages' (SF Analyzer audit.json); "
                f"got top-level keys {sorted(document.keys())!r}"
            ),
        }
    # The four human-facing writers only ever read the flat site-audit shape;
    # ``json`` relays the original document, on either contract, untouched.
    rendered = document if kind == "site-audit" else _normalize_sf_audit(document)

    target = pathlib.Path(path or f"audit-{rendered.get('domain', 'site')}.{fmt}")
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        if fmt == "json":
            target.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        elif fmt == "xlsx":
            from seohead.reports import xlsx

            xlsx.write(rendered, target)
        elif fmt == "docx":
            from seohead.reports import docx

            docx.write(rendered, target)
        elif fmt == "csv":
            from seohead.reports import csvfile

            csvfile.write(rendered, target)
        else:
            from seohead.reports import md

            md.write(rendered, target)
    except ImportError as exc:
        return {
            "ok": False,
            "error": f"dependency required for {fmt} is missing: {exc}",
            "install": "pip install 'seohead[reports]'",
        }
    # File output is an external boundary. Return the failure as structured data
    # so CLI and MCP callers receive the same non-crashing contract.
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "format": fmt,
        "path": str(target),
        "bytes": target.stat().st_size,
        "findings": len(rendered.get("findings") or []),
        "pages": len(rendered.get("pages") or []),
    }
