"""Format only saved capability/population metadata for human report writers."""

from __future__ import annotations

from typing import Any


def rows(summary: dict[str, Any]) -> list[list[str]]:
    from .client_findings import check_title

    contract = summary.get("evidence_contract")
    output: list[list[str]] = []
    field_cwv = summary.get("field_cwv")
    if isinstance(field_cwv, dict):
        assessments = field_cwv.get("assessments") or []
        sampling = field_cwv.get("sampling")
        if isinstance(sampling, dict):
            output.append(
                [
                    "CrUX field CWV",
                    "Bounded URL sample",
                    str(field_cwv.get("state", "unavailable")),
                    "; ".join(
                        f"{key}: {sampling.get(key)}"
                        for key in (
                            "requested",
                            "sampled",
                            "omitted",
                            "requests",
                            "cache_hits",
                            "duplicate_record_targets",
                            "cache_max_age_hours",
                        )
                    ),
                ]
            )
        if not assessments:
            output.append(
                [
                    "CrUX field CWV",
                    "LCP / INP / CLS",
                    str(field_cwv.get("state", "unavailable")),
                    "no supplied field record",
                ]
            )
        for assessment in assessments:
            period = assessment.get("collection_period") or {}
            scope = (
                f"{assessment.get('target_kind')}: {assessment.get('target')}; "
                f"{assessment.get('form_factor')}; "
                f"{period.get('first_date')}..{period.get('last_date')}; "
                f"retrieved {assessment.get('retrieved_at')}; "
                f"{assessment.get('policy')}; "
                f"{assessment.get('provider_access')}, {assessment.get('cost_mode')}, "
                f"{assessment.get('quota_mode')}"
            )
            for name, metric in (assessment.get("metrics") or {}).items():
                measurement = str(metric.get("label") or name) + (
                    f" p75 {metric['p75']} {metric['unit']}"
                    if metric.get("p75") is not None
                    else ""
                )
                output.append(
                    [
                        "CrUX field CWV",
                        measurement,
                        str(metric.get("state", "unavailable")),
                        scope + (f"; {metric['reason']}" if metric.get("reason") else ""),
                    ]
                )
    if not isinstance(contract, dict):
        return output
    output += [
        [
            "Run",
            "Saved scan identity",
            str(contract.get("scan_identity_state", "unavailable")),
            str(
                contract.get("scan_uuid") or contract.get("scan_identity_reason") or "not retained"
            ),
        ]
    ]
    population = contract.get("population")
    if isinstance(population, dict):
        output.append(
            [
                "Run",
                "Measured population",
                "partial" if population.get("crawl_partial") else "recorded",
                "; ".join(
                    (
                        "URLs: " + str(population.get("urls_crawled", "unknown")),
                        "Scope: " + str(population.get("scope_reason") or "saved crawl population"),
                    )
                ),
            ]
        )
    for row in contract.get("capability_rows") or []:
        if isinstance(row, dict):
            output.append(
                [
                    "Check",
                    check_title(row.get("check")),
                    str(row.get("state", "unmeasured")),
                    str(row.get("reason") or row.get("capability") or "not recorded")
                    + (
                        "; prerequisite metadata: "
                        + str(
                            row["prerequisites"]
                            .get("required_evidence", {})
                            .get("state", "unknown")
                        )
                        + "; population: "
                        + str(
                            row["prerequisites"].get("population", {}).get("value")
                            or "not declared"
                        )
                        if isinstance(row.get("prerequisites"), dict)
                        else ""
                    ),
                ]
            )
    return output
