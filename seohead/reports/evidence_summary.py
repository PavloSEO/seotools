"""Format only saved capability/population metadata for human report writers."""

from __future__ import annotations

import json
from typing import Any


def rows(summary: dict[str, Any]) -> list[list[str]]:
    from .client_findings import check_title

    contract = summary.get("evidence_contract")
    if not isinstance(contract, dict):
        return []
    output = [
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
                json.dumps(population, ensure_ascii=False, sort_keys=True),
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
                        "; prerequisites: "
                        + json.dumps(row["prerequisites"], sort_keys=True, ensure_ascii=False)
                        if isinstance(row.get("prerequisites"), dict)
                        else ""
                    ),
                ]
            )
    return output
