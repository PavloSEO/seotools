"""Append-only render phase summaries, without document bodies or local paths."""

import json
from dataclasses import asdict

from . import ScanError

KIND = "render_phase_summary"
FIELDS = {
    "mode",
    "patterns_sampled",
    "patterns_escalated",
    "probe_requests",
    "render_requests",
    "render_budget_exhausted",
    "time_budget_exhausted",
    "render_counts",
    "patterns_partially_rendered",
    "patterns_unprobed",
    "patterns_unprobed_reasons",
}


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ScanError("invalid render phase summary")
    if payload["mode"] not in {"raw", "js", "legacy_fragment"}:
        raise ScanError("invalid render phase mode")
    for key in ("patterns_sampled", "probe_requests", "render_requests"):
        if type(payload[key]) is not int or payload[key] < 0:
            raise ScanError("invalid render phase count")
    for key in ("render_budget_exhausted", "time_budget_exhausted"):
        if type(payload[key]) is not bool:
            raise ScanError("invalid render phase budget state")
    for key in ("patterns_escalated", "patterns_partially_rendered", "patterns_unprobed"):
        if not isinstance(payload[key], list) or any(
            not isinstance(value, str) for value in payload[key]
        ):
            raise ScanError("invalid render phase patterns")
    if not isinstance(payload["render_counts"], dict) or any(
        type(value) is not int or value < 0 for value in payload["render_counts"].values()
    ):
        raise ScanError("invalid render phase pattern count")
    if not isinstance(payload["patterns_unprobed_reasons"], dict) or any(
        not isinstance(value, str) for value in payload["patterns_unprobed_reasons"].values()
    ):
        raise ScanError("invalid render phase reason")


def record(scan, outcome):
    payload = {key: value for key, value in asdict(outcome).items() if key in FIELDS}
    validate(payload)
    ordinal = scan.con.execute(
        "SELECT COUNT(*) FROM context_items WHERE kind=?", (KIND,)
    ).fetchone()[0]
    scan.write_context(
        [
            {
                "kind": KIND,
                "item_key": f"phase:{ordinal}",
                "payload_version": "scan_context.v1",
                "payload_json": json.dumps(payload, sort_keys=True),
                "completeness": "complete",
                "reason": "",
            }
        ]
    )


def aggregate(con, outcome):
    for row in con.execute(
        "SELECT payload_json FROM context_items WHERE kind=? ORDER BY item_key", (KIND,)
    ):
        payload = json.loads(row[0])
        validate(payload)
        outcome.patterns_sampled += payload["patterns_sampled"]
        outcome.probe_requests += payload["probe_requests"]
        outcome.render_requests += payload["render_requests"]
        outcome.render_budget_exhausted |= payload["render_budget_exhausted"]
        outcome.time_budget_exhausted |= payload["time_budget_exhausted"]
        for key in ("patterns_escalated", "patterns_partially_rendered", "patterns_unprobed"):
            setattr(outcome, key, sorted(set(getattr(outcome, key)) | set(payload[key])))
        outcome.patterns_unprobed_reasons.update(payload["patterns_unprobed_reasons"])
        for pattern, count in payload["render_counts"].items():
            outcome.render_counts[pattern] = outcome.render_counts.get(pattern, 0) + count
    return outcome
