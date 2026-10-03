"""Lossless semantic projection for technical-audit PDF reports.

The model contains evidence and stable source references only. Layout, locale,
branding and rendering belong to later report layers.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

SCHEMA = "seohead.technical-audit-pdf/1"


def _source_ref(collection: str, index: int | None = None, record: dict | None = None) -> dict:
    pointer = "#" + "".join(
        "/" + part.replace("~", "~0").replace("/", "~1") for part in collection.split(".")
    )
    if index is not None:
        pointer += f"/{index}"
    result = {"collection": collection, "index": index, "pointer": pointer}
    if isinstance(record, dict):
        for key in ("id", "finding_id", "observation_id"):
            value = record.get(key)
            if value is not None:
                result["id"] = value
                break
    return result


def _declared(value: Any, present: bool) -> dict[str, Any]:
    return {"state": "reported" if present else "not_reported", "value": value if present else None}


def _collection(value: Any, name: str, *, record_kind: str) -> dict[str, Any]:
    """Preserve absent collections separately from explicitly empty ones."""
    if value is None:
        return {"state": "unavailable", "source_count": None, "projected_count": 0, "records": []}
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list when present")
    records = []
    for index, item in enumerate(value):
        if record_kind == "mapping" and not isinstance(item, dict):
            raise ValueError(f"{name}[{index}] must be an object")
        if record_kind == "string" and not isinstance(item, str):
            raise ValueError(f"{name}[{index}] must be a string")
        records.append(deepcopy(item))
    return {
        "state": "reported",
        "source_count": len(value),
        "projected_count": len(records),
        "records": records,
    }


def _count_block(records: list, declared_value: Any, declared_present: bool) -> dict[str, Any]:
    return {
        "source_count": len(records),
        "declared_total": _declared(declared_value, declared_present),
        "projected_count": len(records),
    }


def _coverage_record(
    collection: str,
    index: int,
    record: Any,
    state: str,
    *,
    identifier: Any = None,
    reason: Any = None,
) -> dict[str, Any]:
    return {
        "source_ref": _source_ref(collection, index, record if isinstance(record, dict) else None),
        "id": identifier,
        "state": state,
        "reason": reason,
        "record": deepcopy(record),
    }


def _coverage(source: dict[str, Any], summary: dict[str, Any], kind: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    groups: dict[str, dict[str, Any]] = {}

    def add_group(
        name: str,
        collection: str,
        raw: Any,
        state: str,
        id_field: str | None = None,
        reason_field: str | None = None,
        record_kind: str = "mapping",
    ) -> None:
        group = _collection(raw, collection, record_kind=record_kind)
        groups[name] = group
        for index, record in enumerate(group["records"]):
            identifier = (
                record.get(id_field)
                if id_field and isinstance(record, dict)
                else record
                if isinstance(record, str)
                else None
            )
            reason = record.get(reason_field) if reason_field and isinstance(record, dict) else None
            record_state = state
            if name == "capabilities" and isinstance(record, dict):
                row_state = record.get("state")
                record_state = (
                    row_state if isinstance(row_state, str) and row_state else "unreported"
                )
            coverage = _coverage_record(
                collection, index, record, record_state, identifier=identifier, reason=reason
            )
            if name == "fired" and isinstance(record, dict):
                key = str(record.get("id") or "")
                if key:
                    escaped = key.replace("~", "~0").replace("/", "~1")
                    coverage["source_ref"]["pointer"] = f"#/summary/by_check/{escaped}"
                    coverage["source_ref"]["key"] = key
                    coverage["source_ref"]["index"] = None
            checks.append(coverage)

    if kind == "site-audit":
        add_group("ran", "summary.tools_run", summary.get("tools_run"), "ran", record_kind="string")
        add_group(
            "failed", "summary.tools_failed", summary.get("tools_failed"), "failed", "tool", "error"
        )
        add_group(
            "page_tools_failed",
            "summary.page_tools_failed",
            summary.get("page_tools_failed"),
            "failed",
            "tool",
            "reason",
        )
        add_group(
            "disabled",
            "summary.checks_disabled",
            summary.get("checks_disabled"),
            "disabled",
            "id",
            "reason",
        )
        evidence = summary.get("evidence_contract")
        if evidence is not None and not isinstance(evidence, dict):
            raise ValueError("summary.evidence_contract must be an object when present")
        capabilities = evidence.get("capability_rows") if isinstance(evidence, dict) else None
        add_group(
            "capabilities",
            "summary.evidence_contract.capability_rows",
            capabilities,
            "unreported",
            "check",
            "reason",
        )
        check_coverage = summary.get("check_coverage")
    else:
        by_check = summary.get("by_check")
        if by_check is not None and not isinstance(by_check, dict):
            raise ValueError("summary.by_check must be an object when present")
        fired = [{"id": name, "finding_count": count} for name, count in (by_check or {}).items()]
        add_group(
            "fired",
            "summary.by_check",
            fired if by_check is not None else None,
            "has_findings",
            "id",
        )
        check_coverage = summary.get("check_coverage")
        if check_coverage is not None and not isinstance(check_coverage, dict):
            raise ValueError("summary.check_coverage must be an object when present")
        silent_ids = (
            check_coverage.get("checks_silent_ids") if isinstance(check_coverage, dict) else None
        )
        add_group(
            "silent",
            "summary.check_coverage.checks_silent_ids",
            silent_ids,
            "ran_no_findings",
            record_kind="string",
        )
        run = source.get("run") or {}
        if not isinstance(run, dict):
            raise ValueError("run must be an object")
        add_group(
            "skipped", "run.checks_skipped", run.get("checks_skipped"), "skipped", "id", "reason"
        )
        add_group(
            "disabled",
            "run.checks_disabled",
            run.get("checks_disabled"),
            "disabled",
            "id",
            "reason",
        )

    if check_coverage is not None and not isinstance(check_coverage, dict):
        raise ValueError("summary.check_coverage must be an object when present")
    evidence_state = (
        "reported" if isinstance(summary.get("evidence_contract"), dict) else "unavailable"
    )
    check_state = "reported" if isinstance(check_coverage, dict) else "unavailable"
    has_records = (
        any(group["state"] == "reported" for group in groups.values())
        or evidence_state == "reported"
        or check_state == "reported"
    )
    return {
        "state": "reported" if has_records else "unavailable",
        "source_evidence": {
            "state": evidence_state,
            "record": deepcopy(summary.get("evidence_contract"))
            if evidence_state == "reported"
            else None,
        },
        "source_check_coverage": {
            "state": check_state,
            "record": deepcopy(check_coverage) if check_state == "reported" else None,
        },
        "groups": groups,
        "checks": checks,
    }


def _backlog(document: dict[str, Any], project: str | None, kind: str) -> dict[str, Any]:
    snapshot = None
    snapshot_ref = None
    if project is not None:
        from seohead.reports.project_coverage import load_snapshot

        _, snapshot = load_snapshot(project, document, kind)
        snapshot_ref = "project_snapshot"
    else:
        candidate = (document.get("summary") or {}).get("project_coverage")
        if candidate is not None:
            if not isinstance(candidate, dict):
                raise ValueError("summary.project_coverage must be an object when present")
            snapshot = candidate
            snapshot_ref = "summary.project_coverage"

    if snapshot is None:
        return {
            "state": "not_requested",
            "source_count": None,
            "declared_count": {"state": "not_requested", "value": None},
            "projected_count": 0,
            "project": None,
            "status": None,
            "items": [],
        }

    status = snapshot.get("status")
    if not isinstance(status, dict):
        return {
            "state": "unavailable",
            "source_count": None,
            "declared_count": {"state": "not_reported", "value": None},
            "projected_count": 0,
            "project": deepcopy(snapshot.get("project")),
            "status": deepcopy(status),
            "items": [],
        }
    raw_items = status.get("items")
    if raw_items is None:
        state, source_count, items = "unavailable", None, []
    elif not isinstance(raw_items, list):
        raise ValueError("project checklist status.items must be a list when present")
    else:
        if any(not isinstance(item, dict) for item in raw_items):
            raise ValueError("project checklist status.items must contain objects")
        state, source_count = "recorded", len(raw_items)
        items = [
            {
                "source_ref": _source_ref(f"{snapshot_ref}.status.items", index, item),
                "record": deepcopy(item),
            }
            for index, item in enumerate(raw_items)
        ]
    counts = status.get("counts")
    declared_present = isinstance(counts, dict) and "total" in counts
    return {
        "state": state,
        "source_count": source_count,
        "declared_count": _declared(
            counts.get("total") if isinstance(counts, dict) else None, declared_present
        ),
        "projected_count": len(items),
        "project": deepcopy(snapshot.get("project")),
        "status": deepcopy(status),
        "items": items,
    }


def _validate_optional_containers(
    document: dict[str, Any], summary: dict[str, Any], kind: str
) -> None:
    """Reject malformed optional evidence containers instead of hiding them."""

    def validate_count(value: Any, name: str, *, positive: bool = False) -> None:
        if type(value) is not int or value < (1 if positive else 0):
            qualifier = "positive" if positive else "non-negative"
            raise ValueError(f"{name} must be a {qualifier} integer")

    def string_ids(raw: Any, name: str) -> set[str] | None:
        if raw is None:
            return None
        if not isinstance(raw, list) or any(
            not isinstance(value, str) or not value for value in raw
        ):
            raise ValueError(f"{name} must be a list of non-empty strings")
        ids = set(raw)
        if len(ids) != len(raw):
            raise ValueError(f"{name} must not contain duplicate IDs")
        return ids

    if kind == "site-audit":
        mapping_fields = (
            "findings_by_severity",
            "evidence_contract",
            "check_coverage",
            "project_coverage",
        )
        list_fields = ("tools_run", "tools_failed", "page_tools_failed", "checks_disabled")
        container = summary
        label = "summary"
    else:
        for name in ("totals", "by_severity", "by_check", "check_coverage", "project_coverage"):
            if (
                name in summary
                and summary[name] is not None
                and not isinstance(summary[name], dict)
            ):
                raise ValueError(f"summary.{name} must be an object when present")
        run = document.get("run") or {}
        for name in ("checks_skipped", "checks_disabled"):
            if name in run and run[name] is not None and not isinstance(run[name], list):
                raise ValueError(f"run.{name} must be a list when present")
        container = {}
        mapping_fields = ()
        list_fields = ()
        label = "summary"

    for name in mapping_fields:
        if (
            name in container
            and container[name] is not None
            and not isinstance(container[name], dict)
        ):
            raise ValueError(f"{label}.{name} must be an object when present")
    for name in list_fields:
        if (
            name in container
            and container[name] is not None
            and not isinstance(container[name], list)
        ):
            raise ValueError(f"{label}.{name} must be a list when present")
    if kind == "site-audit":
        page_failures = summary.get("page_tools_failed")
        if page_failures is not None and any(not isinstance(row, dict) for row in page_failures):
            raise ValueError("summary.page_tools_failed must contain objects")
        for index, row in enumerate(page_failures or []):
            for name in ("failed_pages", "pages_checked"):
                if name in row:
                    validate_count(row[name], f"summary.page_tools_failed[{index}].{name}")
            if (
                row.get("failed_pages") is not None
                and row.get("pages_checked") is not None
                and row["failed_pages"] > row["pages_checked"]
            ):
                raise ValueError(
                    f"summary.page_tools_failed[{index}].failed_pages exceeds pages_checked"
                )
        severity = summary.get("findings_by_severity")
        severity_prefix = "summary.findings_by_severity"
    else:
        by_check = summary.get("by_check")
        if isinstance(by_check, dict):
            for check, count in by_check.items():
                if not isinstance(check, str) or not check:
                    raise ValueError("summary.by_check keys must be non-empty strings")
                if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
                    raise ValueError(f"summary.by_check[{check!r}] must be a positive integer")
        totals = summary.get("totals") or {}
        for name, value in totals.items():
            if name == "pages_by_representation":
                if not isinstance(value, dict):
                    raise ValueError("summary.totals.pages_by_representation must be an object")
                for representation, amount in value.items():
                    if not isinstance(representation, str) or not representation:
                        raise ValueError(
                            "summary.totals.pages_by_representation keys must be non-empty strings"
                        )
                    validate_count(
                        amount, f"summary.totals.pages_by_representation[{representation!r}]"
                    )
            else:
                validate_count(value, f"summary.totals.{name}")
        severity = summary.get("by_severity")
        severity_prefix = "summary.by_severity"

    if isinstance(severity, dict):
        for name, value in severity.items():
            if not isinstance(name, str) or not name:
                raise ValueError(f"{severity_prefix} keys must be non-empty strings")
            validate_count(value, f"{severity_prefix}.{name}")

    declared_total_fields = (
        (("findings_total", summary), ("pages_checked", summary)) if kind == "site-audit" else ()
    )
    for name, container in declared_total_fields:
        if name in container and container[name] is not None:
            validate_count(container[name], f"summary.{name}")

    evidence = summary.get("evidence_contract")
    if isinstance(evidence, dict) and "capability_rows" in evidence:
        rows = evidence["capability_rows"]
        if rows is not None and (
            not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
        ):
            raise ValueError("summary.evidence_contract.capability_rows must contain objects")

    if kind != "sf-audit":
        return

    totals = summary.get("totals") or {}
    check_coverage = summary.get("check_coverage")
    run = document.get("run") or {}
    skipped_rows = run.get("checks_skipped") or []
    disabled_rows = run.get("checks_disabled") or []
    for collection_name, rows in (
        ("run.checks_skipped", skipped_rows),
        ("run.checks_disabled", disabled_rows),
    ):
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"{collection_name} must contain objects")
        ids = [row.get("id") for row in rows]
        string_ids(ids, f"{collection_name} IDs")
    skipped_ids = {row["id"] for row in skipped_rows}
    run_disabled_ids = {row["id"] for row in disabled_rows}

    if not isinstance(check_coverage, dict):
        return
    for name, value in check_coverage.items():
        if not isinstance(name, str):
            raise ValueError("summary.check_coverage keys must be strings")
        if name.startswith("checks_") and not name.endswith("_ids"):
            validate_count(value, f"summary.check_coverage.{name}")

    silent_ids = string_ids(
        check_coverage.get("checks_silent_ids"), "summary.check_coverage.checks_silent_ids"
    )
    disabled_ids = string_ids(
        check_coverage.get("checks_disabled_ids"), "summary.check_coverage.checks_disabled_ids"
    )
    fired_ids = (
        set(summary.get("by_check") or {}) if isinstance(summary.get("by_check"), dict) else None
    )
    effective_disabled_ids = run_disabled_ids | (disabled_ids or set())

    if silent_ids is not None and fired_ids is not None and silent_ids & fired_ids:
        raise ValueError("summary.by_check and checks_silent_ids contain the same check")
    if fired_ids is not None and fired_ids & effective_disabled_ids:
        raise ValueError("summary.by_check contains a disabled check")
    if silent_ids is not None and silent_ids & effective_disabled_ids:
        raise ValueError("checks_silent_ids contains a disabled check")
    if silent_ids is not None and silent_ids & skipped_ids:
        raise ValueError("checks_silent_ids contains a skipped check")
    if disabled_ids is not None and "checks_disabled" in run and disabled_ids != run_disabled_ids:
        raise ValueError("checks_disabled_ids disagrees with run.checks_disabled")

    count_fields = check_coverage
    checks_fired = count_fields.get("checks_fired")
    checks_silent = count_fields.get("checks_silent")
    checks_disabled = count_fields.get("checks_disabled")
    checks_skipped = count_fields.get("checks_skipped")
    checks_total = count_fields.get("checks_total")
    if checks_fired is not None and fired_ids is not None and checks_fired != len(fired_ids):
        raise ValueError("checks_fired disagrees with summary.by_check")
    if checks_silent is not None and silent_ids is not None and checks_silent != len(silent_ids):
        raise ValueError("checks_silent disagrees with checks_silent_ids")
    if checks_disabled is not None:
        declared_disabled = effective_disabled_ids
        if (disabled_ids is not None or disabled_rows) and checks_disabled != len(
            declared_disabled
        ):
            raise ValueError("checks_disabled disagrees with disabled check IDs")
    if checks_skipped is not None and (
        skipped_rows or fired_ids is not None or effective_disabled_ids
    ):
        effective_skipped = skipped_ids - (fired_ids or set()) - effective_disabled_ids
        if checks_skipped != len(effective_skipped):
            raise ValueError("checks_skipped disagrees with run.checks_skipped")
    if (
        checks_total is not None
        and checks_fired is not None
        and checks_skipped is not None
        and checks_disabled is not None
        and checks_silent is not None
        and checks_total != checks_fired + checks_skipped + checks_disabled + checks_silent
    ):
        raise ValueError("check coverage counts do not sum to checks_total")


def build_pdf_model(data: Any, *, project: str | None = None) -> dict[str, Any]:
    """Build a lossless semantic model from one recognized saved audit.

    The function is offline and does not write files. It preserves source rows,
    exact source summary values and their counts separately from projected
    counts. It deliberately leaves layout, locale, branding and pagination to
    the PDF renderer.
    """
    from seohead.reports import _detect_kind, _load, _normalize_sf_audit
    from seohead.reports.client_findings import project_finding

    diagnostics: list[dict[str, str]] = []
    document = _load(data, diagnostics)
    if not isinstance(document, dict):
        raise ValueError(f"audit document must be a JSON object, got {type(document).__name__}")
    kind, error = _detect_kind(document)
    if kind is None:
        raise ValueError(error or f"audit document schema not recognized: {sorted(document)}")

    summary = document.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("audit summary must be an object")
    _validate_optional_containers(document, summary, kind)
    findings_collection = "findings" if kind == "site-audit" else "issues"
    source_findings = document.get(findings_collection)
    source_pages = document.get("pages")
    if not isinstance(source_findings, list) or any(
        not isinstance(item, dict) for item in source_findings
    ):
        raise ValueError(f"{findings_collection} must contain only finding objects")
    if not isinstance(source_pages, list) or any(
        not isinstance(item, dict) for item in source_pages
    ):
        raise ValueError("pages must contain only page objects")

    normalized = document if kind == "site-audit" else _normalize_sf_audit(document)
    display_document = {
        "findings": [project_finding(item) for item in normalized.get("findings") or []]
    }
    normalized_findings = normalized.get("findings") or []
    if len(normalized_findings) != len(source_findings) or len(display_document["findings"]) != len(
        source_findings
    ):
        raise ValueError("finding projection did not preserve the source row count")
    normalized_pages = normalized.get("pages") or []
    if len(normalized_pages) != len(source_pages):
        raise ValueError("page projection did not preserve the source row count")

    findings = []
    for index, (raw, projected, display) in enumerate(
        zip(source_findings, normalized_findings, display_document["findings"], strict=True)
    ):
        findings.append(
            {
                "source_ref": _source_ref(findings_collection, index, raw),
                "record": deepcopy(raw),
                "display": {
                    "check_key": projected.get("check"),
                    "title": display.get("client_title"),
                    "observation": display.get("client_observation"),
                    "reproduction": display.get("client_reproduction"),
                    "details": deepcopy(display.get("client_details") or []),
                    "locations": deepcopy(display.get("client_locations") or []),
                    "evidence_reference": deepcopy(display.get("client_evidence")),
                },
            }
        )

    pages = [
        {
            "source_ref": _source_ref("pages", index, raw),
            "record": deepcopy(raw),
            "display": deepcopy(projected),
        }
        for index, (raw, projected) in enumerate(zip(source_pages, normalized_pages, strict=True))
    ]

    raw_run = document.get("run") if kind == "sf-audit" else {}
    raw_run = raw_run if isinstance(raw_run, dict) else {}
    crawl_valid_present = "crawl_valid" in raw_run or "crawl_valid" in summary
    crawl_valid = raw_run.get("crawl_valid", summary.get("crawl_valid"))
    crawl_partial_present = "crawl_partial" in raw_run or "crawl_partial" in summary
    crawl_partial = raw_run.get("crawl_partial", summary.get("crawl_partial"))
    if crawl_valid is False:
        run_state = "failed"
    elif crawl_partial is True:
        run_state = "partial"
    elif crawl_valid is True and crawl_partial is False:
        run_state = "complete"
    elif document.get("ok") is False:
        run_state = "failed"
    elif document.get("ok") is True:
        run_state = "complete"
    else:
        run_state = "unknown"
    run_reasons = []
    for name, value in (
        (
            "crawl_invalid_reason",
            raw_run.get("crawl_invalid_reason") or summary.get("crawl_invalid_reason"),
        ),
        ("crawl_finish_reason", raw_run.get("crawl_finish_reason")),
        ("crawl_stopped_reason", raw_run.get("crawl_stopped_reason")),
    ):
        if value is not None:
            run_reasons.append({"source_field": name, "value": deepcopy(value)})
    scope = {
        name: deepcopy(raw_run[name])
        for name in (
            "source",
            "project",
            "crawl_valid",
            "crawl_partial",
            "crawl_finish_reason",
            "crawl_stopped_reason",
            "crawl_scope_note",
        )
        if name in raw_run
    }
    if "crawl_scope_note" not in scope and "crawl_scope_note" in summary:
        scope["crawl_scope_note"] = deepcopy(summary["crawl_scope_note"])
    if "health_score_scope" in summary:
        scope["health_score_scope"] = deepcopy(summary["health_score_scope"])
    if kind == "site-audit":
        scope["operation"] = "bounded_site_audit"

    coverage = _coverage(document, summary, kind)
    backlog = _backlog(document, project, kind)

    if kind == "site-audit":
        declared_findings_key = "findings_total"
        declared_findings = summary.get(declared_findings_key)
        findings_declared = declared_findings_key in summary
        declared_pages_key = "pages_checked"
        declared_pages = summary.get(declared_pages_key)
        pages_declared = declared_pages_key in summary
        severity_key = "findings_by_severity"
    else:
        totals = summary.get("totals") or {}
        declared_findings_key = "totals.issues_total"
        declared_findings = totals.get("issues_total")
        findings_declared = "issues_total" in totals
        declared_pages_key = "totals.urls_crawled"
        declared_pages = totals.get("urls_crawled")
        pages_declared = "urls_crawled" in totals
        severity_key = "by_severity"
    declared_severity = summary.get(severity_key)
    severity_reported = isinstance(declared_severity, dict)

    def group_count(name: str) -> dict[str, Any]:
        group = coverage["groups"].get(name) or {
            "state": "unavailable",
            "source_count": None,
            "projected_count": 0,
        }
        return {
            "state": group["state"],
            "source_count": group["source_count"],
            "projected_count": group["projected_count"],
        }

    check_coverage = summary.get("check_coverage")
    check_total_present = isinstance(check_coverage, dict) and "checks_total" in check_coverage
    check_total = check_coverage.get("checks_total") if isinstance(check_coverage, dict) else None
    run_id = raw_run.get("scan_uuid") or raw_run.get("run_id")
    evidence_contract = summary.get("evidence_contract")
    if run_id is None and isinstance(evidence_contract, dict):
        run_id = evidence_contract.get("scan_uuid")

    source_schema = (
        document.get("schema") if kind == "site-audit" else document.get("schema_version")
    )
    model = {
        "schema": SCHEMA,
        "source": {
            "kind": kind,
            "schema": source_schema,
            "domain": document.get("domain") or raw_run.get("project"),
            "url": document.get("url") or raw_run.get("source"),
            "generated_at": document.get("generated_at") or raw_run.get("generated_at"),
            "run_id": run_id,
            "input_diagnostics": deepcopy(diagnostics),
        },
        "run": {
            "state": run_state,
            "validity": _declared(crawl_valid, crawl_valid_present),
            "partial": _declared(crawl_partial, crawl_partial_present),
            "scope": scope,
            "reasons": run_reasons,
        },
        "summary": {
            "source": deepcopy(summary),
            "counts": {
                "findings": {
                    **_count_block(source_findings, declared_findings, findings_declared),
                    "declared_total_source_field": declared_findings_key,
                    "declared_by_severity": _declared(declared_severity, severity_reported),
                },
                "pages": {
                    **_count_block(source_pages, declared_pages, pages_declared),
                    "declared_total_source_field": declared_pages_key,
                },
                "checks": {
                    "declared_total": _declared(check_total, check_total_present),
                    "ran": group_count("ran") if kind == "site-audit" else group_count("fired"),
                    "skipped": group_count("skipped"),
                    "failed": group_count("failed"),
                    "page_tools_failed": group_count("page_tools_failed"),
                    "disabled": group_count("disabled"),
                    "capabilities": group_count("capabilities"),
                    "silent": group_count("silent"),
                },
                "backlog": {
                    "state": backlog["state"],
                    "source_count": backlog["source_count"],
                    "declared_count": deepcopy(backlog["declared_count"]),
                    "projected_count": backlog["projected_count"],
                },
            },
        },
        "coverage": coverage,
        "findings": findings,
        "pages": pages,
        "backlog": backlog,
        "omissions": [],
    }
    return model
