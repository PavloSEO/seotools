"""Additive, body-free evidence references for saved audit documents.

The SF audit JSON is deliberately still ``2.0``.  This module adds an
optional contract around it rather than changing the historical wire format:
new native scans can name the saved SQLite ``audit`` observation that supports
each finding, while export-only and older documents explicitly say that such a
reference was not retained.  No helper here opens a page body or contacts a
target.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from typing import Any

from .registry import CHECKS

CONTRACT_VERSION = "audit_evidence_contract.v1"
SAVED_CORPUS_VERSION = "saved_corpus_derivations.v1"
AUDIT_SCHEMA_VERSION = "2.0"
_ISSUE_ID = re.compile(r"ISSUE-[0-9]{6}")
_TABLE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def stable_evidence_id(*, scan_uuid: str, source_table: str, observation_id: str) -> str:
    """Return an opaque stable identifier for one retained typed observation.

    ``source_table`` and ``observation_id`` remain separate fields in the
    contract for a human or reader to resolve.  The compact hash avoids turning
    arbitrary source identifiers into a report-facing path or payload.
    """
    try:
        normalized_uuid = str(uuid.UUID(scan_uuid))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("evidence reference requires a UUID scan identity") from exc
    if not isinstance(source_table, str) or not _TABLE.fullmatch(source_table):
        raise ValueError("evidence reference has an invalid typed table")
    if not isinstance(observation_id, str) or not observation_id or len(observation_id) > 256:
        raise ValueError("evidence reference has an invalid observation identity")
    digest = hashlib.sha256(
        json.dumps(
            [CONTRACT_VERSION, normalized_uuid, source_table, observation_id],
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"evidence:v1:{normalized_uuid}:{source_table}:{digest}"


def _scan_uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _items(value: Any) -> list[dict[str, str]]:
    """Return only closed skip/disable records from an audit document."""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        check_id, reason = item.get("id"), item.get("reason")
        if isinstance(check_id, str) and check_id in CHECKS and isinstance(reason, str):
            result.append({"id": check_id, "reason": reason})
    return result


def _population(document: Mapping[str, Any]) -> dict[str, Any]:
    run = document.get("run") if isinstance(document.get("run"), Mapping) else {}
    summary = document.get("summary") if isinstance(document.get("summary"), Mapping) else {}
    totals = summary.get("totals") if isinstance(summary.get("totals"), Mapping) else {}
    urls = totals.get("urls_crawled")
    representations = totals.get("pages_by_representation")
    return {
        "urls_crawled": urls if type(urls) is int and urls >= 0 else None,
        "crawl_valid": run.get("crawl_valid") is not False,
        "crawl_partial": bool(run.get("crawl_partial")),
        "scope_reason": summary.get("health_score_scope")
        if isinstance(summary.get("health_score_scope"), str)
        else "",
        "representations": dict(representations) if isinstance(representations, Mapping) else {},
    }


def capability_rows(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Describe every registered check without treating missing data as clean.

    ``check_coverage`` and the run's named skipped/disabled declarations are
    the authoritative completion facts.  A check that has no finding and is
    not listed in the silent inventory is *unmeasured*, not a silent success.
    """
    run = document.get("run") if isinstance(document.get("run"), Mapping) else {}
    summary = document.get("summary") if isinstance(document.get("summary"), Mapping) else {}
    coverage = (
        summary.get("check_coverage")
        if isinstance(summary.get("check_coverage"), Mapping)
        else None
    )
    issues = document.get("issues") if isinstance(document.get("issues"), list) else []
    fired = {
        item.get("check")
        for item in issues
        if isinstance(item, Mapping)
        and isinstance(item.get("check"), str)
        and item["check"] in CHECKS
    }
    skipped = {item["id"]: item["reason"] for item in _items(run.get("checks_skipped"))}
    disabled = {item["id"]: item["reason"] for item in _items(run.get("checks_disabled"))}
    if isinstance(coverage, Mapping):
        coverage_disabled = coverage.get("checks_disabled_ids")
        if isinstance(coverage_disabled, list):
            for check_id in coverage_disabled:
                if isinstance(check_id, str) and check_id in CHECKS:
                    disabled.setdefault(check_id, "disabled by the saved audit configuration")
        silent_raw = coverage.get("checks_silent_ids")
        silent = {
            check_id for check_id in silent_raw if isinstance(check_id, str) and check_id in CHECKS
        } if isinstance(silent_raw, list) else set()
    else:
        silent = set()
    partial = bool(run.get("crawl_partial"))
    rows: list[dict[str, Any]] = []
    for check_id in sorted(CHECKS):
        meta = CHECKS[check_id]
        row: dict[str, Any] = {
            "check": check_id,
            "source": str(meta["source"]),
            "state": "unmeasured",
            "capability": "unaccounted",
            "reason": "the saved audit does not account for this check",
        }
        if check_id in fired:
            row.update(state="measured", capability="finding_recorded", reason="")
        elif check_id in disabled:
            row.update(state="unmeasured", capability="disabled", reason=disabled[check_id])
        elif check_id in skipped:
            row.update(state="unmeasured", capability="skipped", reason=skipped[check_id])
        elif check_id in silent:
            row.update(
                state="partial" if partial else "measured",
                capability="ran_without_finding",
                reason=(
                    "crawl was partial; this clean result covers only the saved scope"
                    if partial
                    else ""
                ),
            )
        rows.append(row)
    return rows


def _issue_reference(issue: Mapping[str, Any], scan_uuid: str | None) -> dict[str, Any]:
    """Build a saved-audit reference; never fabricate one for a legacy audit."""
    issue_id = issue.get("id")
    if scan_uuid is None:
        return {
            "state": "unavailable",
            "reason": "legacy or export-only audit has no retained scan UUID",
        }
    if not isinstance(issue_id, str) or not _ISSUE_ID.fullmatch(issue_id):
        return {
            "state": "unavailable",
            "reason": "saved audit finding has no stable ISSUE identifier",
        }
    observation_id = f"issue:{issue_id}"
    return {
        "state": "measured",
        "id": stable_evidence_id(
            scan_uuid=scan_uuid, source_table="audit", observation_id=observation_id
        ),
        "scan_uuid": scan_uuid,
        "source_table": "audit",
        "observation_id": observation_id,
    }


def attach_contract(
    document: Mapping[str, Any], *, scan_uuid: str | None = None
) -> dict[str, Any]:
    """Return a copied audit document with additive capability and evidence IDs.

    The function deliberately keeps ``schema_version`` at ``2.0``; callers can
    use it before reports/tasks are built without breaking existing audit JSON
    readers.  A UUID supplied by a scan header wins over any run field so an
    integration can bind an exported document to its retained scan explicitly.
    """
    projected = copy.deepcopy(dict(document))
    if projected.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise ValueError("evidence contract supports only audit.json schema_version 2.0")
    run = projected.get("run") if isinstance(projected.get("run"), dict) else {}
    if not isinstance(projected.get("run"), dict):
        projected["run"] = run
    summary = projected.get("summary") if isinstance(projected.get("summary"), dict) else {}
    if not isinstance(projected.get("summary"), dict):
        projected["summary"] = summary
    identity = _scan_uuid(scan_uuid) or _scan_uuid(run.get("scan_uuid"))
    summary["evidence_contract"] = {
        "schema_version": CONTRACT_VERSION,
        "audit_schema_version": projected.get("schema_version"),
        "scan_uuid": identity,
        "scan_identity_state": "measured" if identity else "unavailable",
        "scan_identity_reason": "" if identity else "no retained scan UUID in this audit document",
        "population": _population(projected),
        "capability_rows": capability_rows(projected),
    }
    issues = projected.get("issues")
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            saved_evidence = issue.get("evidence")
            evidence = dict(saved_evidence) if isinstance(saved_evidence, Mapping) else {}
            evidence["contract"] = _issue_reference(issue, identity)
            issue["evidence"] = evidence
    return projected


def attach_saved_corpus(
    document: Mapping[str, Any], con: Any, *, duplicate_threshold: float = 0.92
) -> dict[str, Any]:
    """Attach already-stored corpus derivations at the audit boundary.

    ``con`` is the caller's already-open scan connection.  The only consumer is
    :func:`seohead.sf.core.corpus_derivations.derive`, which reads typed context
    items and does not fetch, render, or retain bodies.  This is intentionally a
    separate pure hook so legacy and export-only callers stay explicit.
    """
    from .corpus_derivations import derive

    projected = copy.deepcopy(dict(document))
    if projected.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise ValueError("saved corpus attachment supports only audit.json schema_version 2.0")
    summary = projected.get("summary") if isinstance(projected.get("summary"), dict) else {}
    if not isinstance(projected.get("summary"), dict):
        projected["summary"] = summary
    derived = derive(con, duplicate_threshold=duplicate_threshold)
    if derived.get("schema_version") != SAVED_CORPUS_VERSION:
        raise ValueError("saved corpus derivation has an unsupported schema version")
    summary["saved_corpus_derivations"] = derived
    return projected


def comparison_compatibility(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Return named comparison bases without inferring equality from absence."""
    def side(document: Mapping[str, Any], key: str) -> Any:
        run = document.get("run") if isinstance(document.get("run"), Mapping) else {}
        summary = document.get("summary") if isinstance(document.get("summary"), Mapping) else {}
        totals = summary.get("totals") if isinstance(summary.get("totals"), Mapping) else {}
        contract = (
            summary.get("evidence_contract")
            if isinstance(summary.get("evidence_contract"), Mapping)
            else {}
        )
        corpus = summary.get("saved_corpus_derivations")
        if key == "scope":
            if "crawl_valid" not in run or "crawl_partial" not in run:
                return None
            return {
                "crawl_valid": run.get("crawl_valid"),
                "crawl_partial": run.get("crawl_partial"),
                "scope": summary.get("health_score_scope"),
            }
        if key == "configuration":
            if not isinstance(run.get("crawl_config"), Mapping):
                return None
            return {
                "crawl_config": run.get("crawl_config"),
                "profile": run.get("profile"),
                "config_fingerprint": run.get("config_fingerprint"),
            }
        if key == "representation":
            representations = totals.get("pages_by_representation")
            return dict(representations) if isinstance(representations, Mapping) else None
        if key == "corpus":
            if not isinstance(corpus, Mapping):
                return None
            return {
                "corpus_partial": run.get("corpus_partial"),
                "schema_version": corpus.get("schema_version"),
                "scan_identity": contract.get("scan_identity_state"),
            }
        if not isinstance(run.get("input_mode"), str):
            return None
        return {
            "input_mode": run.get("input_mode"),
            "collector": run.get("collector"),
            "provider": run.get("provider"),
            "source_kind": run.get("source_kind"),
        }

    result: list[dict[str, Any]] = []
    for key in ("scope", "configuration", "representation", "corpus", "provider"):
        before_value, after_value = side(before, key), side(after, key)
        unknown = before_value is None or after_value is None
        # A dictionary with only absent values is also an unknown basis.
        if isinstance(before_value, dict) and not any(
            value is not None for value in before_value.values()
        ):
            unknown = True
        if isinstance(after_value, dict) and not any(
            value is not None for value in after_value.values()
        ):
            unknown = True
        if unknown:
            state = "unknown"
        elif before_value == after_value:
            state = "compatible"
        else:
            state = "incompatible"
        result.append({"basis": key, "state": state, "before": before_value, "after": after_value})
    return result


def comparison_warnings(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Render comparison bases as concise warnings for a caller's preflight."""
    warnings = []
    for row in comparison_compatibility(before, after):
        if row["state"] == "compatible":
            continue
        if row["state"] == "unknown":
            warnings.append(
                f"comparison {row['basis']} basis is unavailable; comparability is unknown"
            )
        else:
            warnings.append(f"comparison {row['basis']} basis differs between saved audits")
    return warnings
