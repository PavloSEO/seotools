"""Reader-facing finding labels and reproductions for human report formats.

The audit documents remain the machine-evidence source.  This module only makes
an immutable, display-oriented projection for Markdown, CSV, XLSX, and DOCX;
it neither fetches a URL nor derives a new audit verdict.
"""

from __future__ import annotations

import re
from typing import Any

from seohead.sf.core.registry import CHECKS, check_meta

_CHECK_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+\b")
_ATTRIBUTION = re.compile(
    r"^(?:seohead|screaming frog)\s+(?:found|reported|detected)\s+(.+?)\.?$", re.IGNORECASE
)
_PROTECTED_EVIDENCE = re.compile(r"https?://\S+|`[^`]*`|\"[^\"]*\"|'[^']*'")
_PRODUCER_REASON = re.compile(r"\b(?:seohead|screaming frog)\b", re.IGNORECASE)
_MAX_EVIDENCE_ITEMS = 10
_FINDING_VIEW_LABELS = {
    "severity": "Severity",
    "check": "Check",
    "url": "URL",
    "text": "Observation",
    "status_code": "Status",
    "occurrences_count": "Occurrences",
    "fix_hint": "Fix hint",
    "details": "Evidence",
    "locations": "Locations",
    "segment": "Segment",
}


def finding_view_notice(summary: dict[str, Any]) -> str | None:
    """Explain that a saved view is a displayed subset while audit totals remain source-wide."""
    view = summary.get("finding_view")
    if not isinstance(view, dict):
        return None
    counts = view.get("counts") or {}
    page = view.get("pagination") or {}
    note = (
        f"Saved finding view {view.get('name')} (view revision {view.get('revision')}, "
        f"config revision {view.get('config_revision')}): showing {counts.get('returned', 0)} "
        f"rows at offset {page.get('offset', 0)} of {counts.get('matched', 0)} matches "
        f"from {counts.get('source', 0)} source findings; {counts.get('filtered', 0)} did not match."
    )
    if page.get("truncated"):
        note += " More matching rows remain; use the next offset to continue."
    if view.get("state") == "partial":
        missing = dict(counts.get("missing_filter_fields") or {})
        for field, count in (counts.get("missing_projection_fields") or {}).items():
            missing[field] = max(missing.get(field, 0), count)
        detail = ", ".join(f"{field}={count}" for field, count in sorted(missing.items()))
        note += f" Some filter or projection fields were unavailable ({detail})."
    note += " Audit totals, evidence coverage, and scores describe the full source audit."
    exclusions = summary.get("finding_exclusions")
    suppressed = exclusions.get("suppressed_total") if isinstance(exclusions, dict) else None
    if type(suppressed) is int and suppressed > 0:
        note += f" The source audit also records {suppressed} excluded findings."
        by_rule = exclusions.get("by_rule")
        if isinstance(by_rule, dict) and by_rule:
            rule_rows = sorted(by_rule.items(), key=lambda item: str(item[0]))
            details = ", ".join(
                f"{re.sub(r'[^A-Za-z0-9_.-]', '?', str(name))[:64]}="
                f"{count if type(count) is int and count >= 0 else 'unknown'}"
                for name, count in rule_rows[:20]
            )
            suffix = f" (+{len(rule_rows) - 20} more)" if len(rule_rows) > 20 else ""
            note += f" Exclusion counts by rule: {details}{suffix}."
    return note


def finding_view_columns(summary: dict[str, Any]) -> list[str] | None:
    view = summary.get("finding_view")
    columns = view.get("columns") if isinstance(view, dict) else None
    return columns if isinstance(columns, list) else None


def finding_view_label(column: str) -> str:
    return _FINDING_VIEW_LABELS.get(column, column)


def check_title(check: Any) -> str:
    """Return a reader-facing title without exposing a registry identifier."""
    check_id = str(check or "")
    if check_id in CHECKS:
        return str(check_meta(check_id).get("message") or "Audit finding")
    if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", check_id):
        return check_id.replace("_", " ").capitalize()
    return "Audit finding"


def finding_exclusion_report(
    summary: dict[str, Any], suppressed_issues: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Join saved exclusion rules to their recorded counts without recomputing them."""
    exclusion = summary.get("finding_exclusions")
    exclusion = exclusion if isinstance(exclusion, dict) else {}
    policy = summary.get("finding_exclusion_policy")
    policy = policy if isinstance(policy, list) else []
    suppressed = suppressed_issues if isinstance(suppressed_issues, list) else []
    if not exclusion and not policy and not suppressed:
        return None

    counts_by_rule: dict[str, dict[str, Any]] = {}
    raw_counts = exclusion.get("by_rule")
    if isinstance(raw_counts, list):
        for row in raw_counts:
            if isinstance(row, dict) and row.get("id") is not None:
                counts_by_rule[str(row["id"])] = row
    elif isinstance(raw_counts, dict):
        for rule_id, count in raw_counts.items():
            counts_by_rule[str(rule_id)] = (
                count if isinstance(count, dict) else {"suppressed_findings": count}
            )

    policy_by_id = {
        str(row["id"]): row for row in policy if isinstance(row, dict) and row.get("id") is not None
    }
    for issue in suppressed:
        marker = issue.get("suppression") if isinstance(issue, dict) else None
        if isinstance(marker, dict) and marker.get("rule_id") is not None:
            rule_id = str(marker["rule_id"])
            policy_by_id.setdefault(
                rule_id,
                {
                    "id": rule_id,
                    "pattern": marker.get("pattern", ""),
                    "reason": marker.get("reason", ""),
                    "checks": [],
                },
            )

    rules = []
    for rule_id, rule in policy_by_id.items():
        counts = counts_by_rule.get(rule_id, {})
        rules.append(
            {
                "id": rule_id,
                "pattern": rule.get("pattern", ""),
                "checks": rule.get("checks") or [],
                "reason": rule.get("reason") or counts.get("reason", ""),
                "suppressed_findings": counts.get("suppressed_findings", 0),
                "suppressed_occurrences": counts.get("suppressed_occurrences", 0),
            }
        )
    for rule_id, counts in counts_by_rule.items():
        if rule_id not in policy_by_id:
            rules.append(
                {
                    "id": rule_id,
                    "pattern": "",
                    "checks": [],
                    "reason": counts.get("reason", ""),
                    "suppressed_findings": counts.get("suppressed_findings", 0),
                    "suppressed_occurrences": counts.get("suppressed_occurrences", 0),
                }
            )

    total = exclusion.get("suppressed_total")
    if type(total) is not int or total < 0:
        total = len(suppressed)
    rules_configured = exclusion.get("rules_configured")
    if type(rules_configured) is not int or rules_configured < 0:
        rules_configured = len(policy_by_id)
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    occurrences = totals.get("suppressed_occurrences")
    if type(occurrences) is not int or occurrences < 0:
        occurrences = sum(
            value["suppressed_occurrences"]
            for value in rules
            if type(value["suppressed_occurrences"]) is int and value["suppressed_occurrences"] >= 0
        )
    return {
        "suppressed_total": total,
        "suppressed_occurrences": occurrences,
        "rules_configured": rules_configured,
        "rules": rules,
        "issues": suppressed,
    }


def _detail_rows(details: Any) -> list[str]:
    """Keep primitive recorded details, with readable labels and bounded shape."""
    if not isinstance(details, dict):
        return []

    def record(value: dict[str, Any]) -> str:
        bits = [
            f"{str(name).replace('_', ' ').capitalize()}: {item}"
            for name, item in sorted(value.items())
            if isinstance(item, (str, int, float, bool)) and item not in ("", None)
        ]
        return "; ".join(bits) or "Structured record retained in the saved audit"

    rows: list[str] = []
    for key, value in sorted(details.items()):
        label = str(key).replace("_", " ").capitalize()
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            rows.append(f"{label}: {value}")
        elif isinstance(value, list):
            values = [str(item) for item in value if isinstance(item, (str, int, float, bool))]
            records = [record(item) for item in value if isinstance(item, dict)]
            unsupported = len(value) - len(values) - len(records)
            parts: list[str] = []
            remaining = _MAX_EVIDENCE_ITEMS
            if values:
                shown = values[:remaining]
                remaining -= len(shown)
                suffix = (
                    f"; {len(values) - len(shown)} more values omitted"
                    if len(values) > len(shown)
                    else ""
                )
                parts.append(", ".join(shown) + suffix)
            if records:
                shown = records[:_MAX_EVIDENCE_ITEMS]
                shown = shown[:remaining]
                suffix = (
                    f"; {len(records) - len(shown)} more structured records omitted"
                    if len(records) > len(shown)
                    else ""
                )
                parts.append(" | ".join(shown) + suffix)
            if unsupported:
                parts.append(f"{unsupported} unsupported values omitted")
            if parts:
                rows.append(f"{label}: {'; '.join(parts)}")
        elif isinstance(value, dict):
            rows.append(f"{label}: {record(value)}")
    return rows


def _observation(value: Any) -> str:
    """Translate only known internal wrappers while preserving recorded content."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = _ATTRIBUTION.fullmatch(text)
    if match:
        text = match.group(1)
    if _CHECK_IDENTIFIER.fullmatch(text) and text in CHECKS:
        return ""

    def translate(part: str) -> str:
        return _CHECK_IDENTIFIER.sub(
            lambda matched: (
                check_title(matched.group(0)) if matched.group(0) in CHECKS else matched.group(0)
            ),
            part,
        )

    return _translate_unprotected(text, translate)


def _translate_unprotected(text: str, translate) -> str:
    """Apply a display translation without corrupting copied URLs, code, or quotes."""
    pieces: list[str] = []
    cursor = 0
    for protected in _PROTECTED_EVIDENCE.finditer(text):
        pieces.append(translate(text[cursor : protected.start()]))
        pieces.append(protected.group(0))
        cursor = protected.end()
    pieces.append(translate(text[cursor:]))
    return "".join(pieces)


def client_reason(value: Any) -> str:
    """Translate known collector wrappers in a failure reason, preserving its cause."""
    text = _observation(value)
    return _translate_unprotected(text, lambda part: _PRODUCER_REASON.sub("The audit", part))


def _location_rows(locations: Any) -> list[str]:
    """Render saved source/anchor/XPath facts without turning them into new findings."""
    if not isinstance(locations, list):
        return []
    rows: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        bits = [
            f"Source: {location['source_url']}"
            if isinstance(location.get("source_url"), str) and location["source_url"]
            else "",
            f"Anchor: {location['anchor']}"
            if isinstance(location.get("anchor"), str) and location["anchor"]
            else "",
            f"Position: {location['link_position']}"
            if isinstance(location.get("link_position"), str) and location["link_position"]
            else "",
            f"XPath: {location['link_path']}"
            if isinstance(location.get("link_path"), str) and location["link_path"]
            else "",
        ]
        if any(bits):
            rows.append("; ".join(bit for bit in bits if bit))
    shown = rows[:_MAX_EVIDENCE_ITEMS]
    if len(rows) > len(shown):
        shown.append(f"{len(rows) - len(shown)} more locations omitted")
    return shown


def _evidence_reference(value: Any) -> dict[str, str]:
    """Project the closed audit-evidence reference without exposing raw inputs.

    Export file names, paths and collector-specific payloads stay in the
    machine audit.  Human reports receive only a stable saved-observation ID,
    or the explicit reason that an older/export-only audit cannot provide one.
    """
    contract = value.get("contract") if isinstance(value, dict) else None
    if not isinstance(contract, dict):
        return {
            "state": "unavailable",
            "reason": "no stable saved-evidence reference is present in this audit",
        }
    observations = contract.get("observations")
    if isinstance(observations, list):
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            if observation.get("state") in {"measured", "imported_projection"}:
                contract = observation
                break
    state = contract.get("state")
    if state not in {"measured", "imported_projection"}:
        return {
            "state": "unavailable",
            "reason": str(contract.get("reason") or "saved evidence is unavailable"),
        }
    identifier = contract.get("id")
    source_table = contract.get("source_table")
    observation_id = contract.get("observation_id")
    if not all(
        isinstance(item, str) and item for item in (identifier, source_table, observation_id)
    ):
        return {
            "state": "unavailable",
            "reason": "saved evidence reference is incomplete",
        }
    return {
        "state": state,
        "id": identifier,
        "source_table": source_table,
        "observation_id": observation_id,
        "role": str(contract.get("role") or "observation"),
    }


def reproduction(finding: dict[str, Any], observation: str = "") -> str:
    """State only the primitive observation the saved audit can support."""
    url = finding.get("url")
    status = finding.get("status_code")
    observation = observation or _observation(finding.get("text"))
    if isinstance(url, str) and url:
        details = [
            row
            for row in _detail_rows(finding.get("details"))
            if "Structured record retained" not in row and "unsupported values omitted" not in row
        ]
        if type(status) is int:
            result = f"{url} returned HTTP {status}."
            observed = details[0] if details else observation
            return f"{result} Recorded observation: {observed}" if observed else result
        if details:
            return f"At {url}: {details[0]}."
        if observation:
            return f"At {url}: {observation}"
    locations = finding.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict) and isinstance(location.get("source_url"), str):
                target = f" to {url}" if isinstance(url, str) and url else ""
                path = (
                    f" at {location['link_path']}"
                    if isinstance(location.get("link_path"), str) and location["link_path"]
                    else ""
                )
                return f"Link from {location['source_url']}{path}{target}."
    target = f" Target URL: {url}." if isinstance(url, str) and url else ""
    return "Reproduction unavailable from the saved audit." + target


def project_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Copy one finding with client-only display fields.

    ``text`` remains the audit's recorded observation.  Check IDs, producer
    names, and tool names are intentionally absent from the display fields;
    those identifiers remain in the original machine audit and JSON output.
    """
    projected = dict(finding)
    observation = _observation(finding.get("text"))
    projected["client_title"] = check_title(finding.get("check"))
    projected["client_observation"] = observation
    projected["client_reproduction"] = reproduction(finding, observation)
    projected["client_details"] = _detail_rows(finding.get("details"))
    projected["client_locations"] = _location_rows(finding.get("locations"))
    projected["client_evidence"] = _evidence_reference(finding.get("evidence"))
    if finding.get("evidence"):
        reference = projected["client_evidence"]
        if reference.get("id"):
            projected["client_details"].append("Saved observation: " + reference["id"])
        else:
            projected["client_details"].append(
                "Saved observation unavailable: " + reference.get("reason", "not captured")
            )
    return projected


def project_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow document copy whose findings have display fields."""
    projected = dict(document)
    summary = dict(document.get("summary") or {})
    summary["checks_disabled"] = [
        {**item, "reason": client_reason(item.get("reason"))}
        for item in summary.get("checks_disabled") or []
        if isinstance(item, dict)
    ]
    summary["tools_failed"] = [
        {**item, "error": client_reason(item.get("error"))}
        for item in summary.get("tools_failed") or []
        if isinstance(item, dict)
    ]
    projected["summary"] = summary
    projected["findings"] = [
        project_finding(finding)
        for finding in document.get("findings") or []
        if isinstance(finding, dict)
    ]
    columns = finding_view_columns(summary)
    if columns is not None:
        for finding in projected["findings"]:
            raw = finding
            client_details = "; ".join(raw.get("client_details") or [])
            client_locations = "; ".join(raw.get("client_locations") or [])
            finding["view_fields"] = {
                "severity": raw.get("severity"),
                "check": raw.get("check"),
                "url": raw.get("url"),
                "text": raw.get("client_observation"),
                "status_code": raw.get("status_code"),
                "occurrences_count": raw.get("occurrences_count"),
                "fix_hint": raw.get("fix_hint"),
                "details": client_details if client_details else None,
                "locations": client_locations if client_locations else None,
                "segment": raw.get("__view_segment"),
            }
    return projected
