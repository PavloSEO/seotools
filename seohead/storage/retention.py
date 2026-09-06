"""Finite scan body policy; metadata capture remains separate from retention."""

from __future__ import annotations

from typing import Any

from . import ScanError

BYTE_FIELDS = ("max_body_bytes", "max_body_store_bytes", "min_free_bytes", "history_warning_bytes")
NO_BODY_RETENTION = {
    "policy_version": "scan_retention.v1",
    "body_mode": "off",
    "max_body_bytes": 0,
    "max_body_store_bytes": 0,
    "min_free_bytes": 0,
    "history_warning_bytes": 0,
    "automatic_delete": False,
}


def validate_policy(policy: Any) -> dict[str, Any]:
    if not isinstance(policy, dict) or set(policy) != set(NO_BODY_RETENTION):
        raise ScanError("invalid scan_retention.v1 policy fields")
    if (
        policy["policy_version"] != "scan_retention.v1"
        or policy["body_mode"] not in {"off", "captured_entity_bytes"}
        or policy["automatic_delete"] is not False
        or any(
            type(policy[key]) is not int or not 0 <= policy[key] <= 2**63 - 1 for key in BYTE_FIELDS
        )
        or (policy["body_mode"] != "off" and policy["max_body_bytes"] == 0)
    ):
        raise ScanError("invalid finite scan body-retention policy")
    return dict(policy)


def policy_for_config(config: dict[str, Any]) -> dict[str, Any]:
    # A missing storage section belongs to an earlier producer. Do not insert
    # current defaults into its recorded configuration or alter its fingerprint.
    if "storage" not in config:
        return dict(NO_BODY_RETENTION)
    return validate_policy(
        {"policy_version": "scan_retention.v1", **config["storage"], "automatic_delete": False}
    )
