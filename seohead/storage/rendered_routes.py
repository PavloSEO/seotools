"""Typed, store-only route observations from native static/rendered documents."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any
from urllib.parse import urldefrag, urlsplit

from . import ScanError

KIND = "rendered_route_ledger"
COVERAGE_KIND = "rendered_route_coverage"
RUN_KIND = "rendered_route_run_coverage"
OUTER_VERSION = "scan_context.v1"
ROUTE_VERSION = "rendered_route_ledger.v1"
COVERAGE_VERSION = "rendered_route_coverage.v1"
_REPRESENTATIONS = {"static", "rendered", "legacy_fragment"}


def observations(
    parsed: dict[str, Any] | None, batch: Any, representation: str
) -> tuple[list[dict], dict]:
    """Return only parser-emitted eligible anchors; never admit or fetch a route."""
    if (
        not isinstance(parsed, dict)
        or not isinstance(parsed.get("_raw_html"), str)
        or not isinstance(parsed.get("links"), list)
    ):
        return [], {
            "representation": representation,
            "observed": 0,
            "omitted": 0,
            "completeness": "unavailable",
            "reason": "eligible anchor extraction is unavailable because the document was not parsed as HTML",
        }
    links = parsed["links"]
    omitted = int((parsed.get("link_observation") or {}).get("omitted") or 0)
    reasons: dict[str, str] = {}
    for decision in getattr(batch, "decisions", ()):
        if isinstance(decision, dict) and isinstance(decision.get("url"), str):
            reasons.setdefault(decision["url"], str(decision.get("reason") or "excluded"))
    values = []
    for link in links:
        if not isinstance(link, dict) or not isinstance(link.get("href"), str):
            continue
        resolved = urldefrag(link["href"].strip())[0]
        target = urlsplit(resolved)
        if target.scheme not in {"http", "https"} or not target.netloc:
            continue
        reason = reasons.get(link["href"]) or reasons.get(resolved)
        values.append(
            {
                "ordinal": len(values),
                "raw_value": str(link.get("raw_href") or ""),
                "resolved_url": resolved,
                "outcome": {
                    "state": "excluded" if reason else "stored",
                    "reason": reason or "stored without frontier admission",
                },
            }
        )
    partial = omitted or "link_observations_omitted" in getattr(batch, "partial_reasons", ())
    completeness = "partial" if partial else "complete"
    return values, {
        "representation": representation,
        "observed": len(values),
        "omitted": omitted,
        "completeness": completeness,
        "reason": "link observations omitted by parser cap" if partial else "",
    }


def context_items(
    page_url_id: int, document_id: int | None, coverage: dict, values: list[dict]
) -> list[dict]:
    """Build immutable route and coverage contexts after a document is stored."""
    representation = coverage["representation"]
    if (
        representation not in _REPRESENTATIONS
        or type(page_url_id) is not int
        or type(coverage.get("observed")) is not int
        or coverage["observed"] < 0
        or coverage["observed"] != len(values)
        or [value.get("ordinal") for value in values] != list(range(len(values)))
    ):
        raise ScanError("rendered route ledger has invalid page identity or representation")
    items = []
    for value in values:
        ordinal = value["ordinal"]
        payload = {
            "schema_version": ROUTE_VERSION,
            "page_url_id": page_url_id,
            "source_document_id": document_id,
            "representation": representation,
            "ordinal": ordinal,
            "extraction_method": "a[href]",
            "raw_value": value["raw_value"],
            "resolved_url": value["resolved_url"],
            "outcome": value["outcome"],
        }
        items.append(
            {
                "kind": KIND,
                "item_key": f"page:{page_url_id}:representation:{representation}:ordinal:{ordinal}",
                "payload_version": OUTER_VERSION,
                "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
                "completeness": "complete",
                "reason": "",
            }
        )
    payload = {
        "schema_version": COVERAGE_VERSION,
        "page_url_id": page_url_id,
        "source_document_id": document_id,
        **coverage,
    }
    items.append(
        {
            "kind": COVERAGE_KIND,
            "item_key": f"page:{page_url_id}:representation:{representation}",
            "payload_version": OUTER_VERSION,
            "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "completeness": coverage["completeness"],
            "reason": coverage["reason"],
        }
    )
    return items


def validate_context(con: Any, item: dict[str, Any], payload: Any) -> None:
    if item["kind"] == RUN_KIND:
        if (
            not isinstance(payload, dict)
            or set(payload)
            != {
                "schema_version",
                "state",
                "reason",
                "store_enabled",
                "rendering_mode",
                "static_sources",
                "rendered_sources",
            }
            or payload["schema_version"] != "rendered_route_run_coverage.v1"
            or payload["state"] not in {"complete", "partial", "unavailable"}
            or not isinstance(payload["reason"], str)
            or type(payload["store_enabled"]) is not bool
            or payload["rendering_mode"] not in {"raw", "js", "legacy_fragment"}
            or any(
                type(payload[k]) is not int or payload[k] < 0
                for k in ("static_sources", "rendered_sources")
            )
            or item["completeness"] != payload["state"]
            or item["reason"] != payload["reason"]
            or item["item_key"] != "run"
        ):
            raise ScanError("native rendered route run coverage is invalid")
        return
    if item["kind"] == KIND:
        required = {
            "schema_version",
            "page_url_id",
            "source_document_id",
            "representation",
            "ordinal",
            "extraction_method",
            "raw_value",
            "resolved_url",
            "outcome",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or payload["schema_version"] != ROUTE_VERSION
            or type(payload["page_url_id"]) is not int
            or (
                payload["source_document_id"] is not None
                and type(payload["source_document_id"]) is not int
            )
            or payload["representation"] not in _REPRESENTATIONS
            or type(payload["ordinal"]) is not int
            or payload["ordinal"] < 0
            or payload["extraction_method"] != "a[href]"
            or not isinstance(payload["raw_value"], str)
            or not isinstance(payload["resolved_url"], str)
            or not payload["resolved_url"]
            or not isinstance(payload["outcome"], dict)
            or set(payload["outcome"]) != {"state", "reason"}
            or payload["outcome"]["state"] not in {"stored", "excluded", "unavailable"}
            or not isinstance(payload["outcome"]["reason"], str)
            or item["completeness"] != "complete"
            or item["reason"]
        ):
            raise ScanError("native rendered route ledger context is invalid")
        if (
            item["item_key"]
            != f"page:{payload['page_url_id']}:representation:{payload['representation']}:ordinal:{payload['ordinal']}"
        ):
            raise ScanError("native rendered route ledger key is invalid")
    elif item["kind"] == COVERAGE_KIND:
        required = {
            "schema_version",
            "page_url_id",
            "source_document_id",
            "representation",
            "observed",
            "omitted",
            "completeness",
            "reason",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or payload["schema_version"] != COVERAGE_VERSION
            or type(payload["page_url_id"]) is not int
            or (
                payload["source_document_id"] is not None
                and type(payload["source_document_id"]) is not int
            )
            or payload["representation"] not in _REPRESENTATIONS
            or any(
                type(payload[key]) is not int or payload[key] < 0 for key in ("observed", "omitted")
            )
            or payload["completeness"] not in {"complete", "partial", "unavailable"}
            or not isinstance(payload["reason"], str)
            or (
                payload["completeness"] == "complete"
                and (payload["omitted"] != 0 or payload["reason"])
            )
            or (
                payload["completeness"] == "partial"
                and not payload["reason"]
            )
            or (
                payload["completeness"] == "unavailable"
                and (payload["observed"] != 0 or payload["omitted"] != 0 or not payload["reason"])
            )
            or item["completeness"] != payload["completeness"]
            or item["reason"] != payload["reason"]
            or item["item_key"]
            != f"page:{payload['page_url_id']}:representation:{payload['representation']}"
        ):
            raise ScanError("native rendered route coverage context is invalid")
    else:
        raise ScanError("native rendered route context kind is invalid")
    if not con.execute("SELECT 1 FROM pages WHERE url_id=?", (payload["page_url_id"],)).fetchone():
        raise ScanError("native rendered route context references an unknown page")
    if payload["source_document_id"] is not None and not con.execute(
        "SELECT 1 FROM documents WHERE document_id=? AND url_id=? AND representation=?",
        (
            payload["source_document_id"],
            payload["page_url_id"],
            payload["representation"],
        ),
    ).fetchone():
        raise ScanError(
            "native rendered route context document does not match its page or representation"
        )
    if item["kind"] == COVERAGE_KIND:
        _validate_coverage_routes(con, payload)


def _validate_coverage_routes(con: Any, coverage: dict[str, Any]) -> None:
    """Check a stored coverage record against the occurrence rows it closes."""
    ordinals = []
    for row in con.execute(
        "SELECT payload_json FROM context_items WHERE kind=? AND item_key LIKE ?",
        (
            KIND,
            f"page:{coverage['page_url_id']}:representation:{coverage['representation']}:ordinal:%",
        ),
    ):
        try:
            route = json.loads(row[0])
        except (TypeError, ValueError) as exc:
            raise ScanError("native rendered route ledger context is invalid JSON") from exc
        if (
            route.get("page_url_id") != coverage["page_url_id"]
            or route.get("representation") != coverage["representation"]
            or type(route.get("ordinal")) is not int
        ):
            raise ScanError("native rendered route coverage does not match its occurrence rows")
        ordinals.append(route["ordinal"])
    if sorted(ordinals) != list(range(coverage["observed"])):
        raise ScanError(
            "native rendered route coverage has noncontiguous ordinals or wrong observed count"
        )


def validate_ledger(con: Any) -> None:
    """Validate every closed occurrence population after all contexts are available."""
    coverage = []
    routes_without_coverage = set()
    for row in con.execute(
        "SELECT kind,payload_json FROM context_items WHERE kind IN (?,?)", (KIND, COVERAGE_KIND)
    ):
        payload = json.loads(row["payload_json"])
        key = (payload["page_url_id"], payload["representation"])
        if row["kind"] == COVERAGE_KIND:
            coverage.append(payload)
        else:
            routes_without_coverage.add(key)
    for item in coverage:
        _validate_coverage_routes(con, item)
        routes_without_coverage.discard((item["page_url_id"], item["representation"]))
    if routes_without_coverage:
        raise ScanError("native rendered route occurrences lack coverage")


def read(con: Any) -> dict:
    """Read immutable observations and derive route relation only from complete sides."""
    routes, coverage = [], {}
    for row in con.execute(
        "SELECT * FROM context_items WHERE kind IN (?,?,?) ORDER BY kind,item_key",
        (KIND, COVERAGE_KIND, RUN_KIND),
    ):
        payload = json.loads(row["payload_json"])
        if row["kind"] == RUN_KIND:
            run = payload
        elif row["kind"] == COVERAGE_KIND:
            coverage[(payload["page_url_id"], payload["representation"])] = payload
        else:
            routes.append(payload)
    grouped = defaultdict(list)
    for route in routes:
        grouped[(route["page_url_id"], route["extraction_method"], route["resolved_url"])].append(
            route
        )
    output = []
    for key, values in sorted(grouped.items()):
        reps = {value["representation"] for value in values}
        static = coverage.get((key[0], "static"))
        counterparts = [
            value
            for (page_url_id, representation), value in coverage.items()
            if page_url_id == key[0] and representation != "static"
        ]
        complete = (
            static
            and static["completeness"] == "complete"
            and counterparts
            and all(value and value["completeness"] == "complete" for value in counterparts)
        )
        if not complete:
            relation = "unknown"
        elif "static" in reps and len(reps) > 1:
            relation = "shared"
        elif "static" in reps:
            relation = "raw_only"
        else:
            relation = "rendered_only"
        output.append(
            {
                "page_url_id": key[0],
                "extraction_method": key[1],
                "resolved_url": key[2],
                "relation": relation,
                "occurrences": values,
            }
        )
    return {
        "routes": output,
        "coverage": list(coverage.values())
        if coverage
        else [{"state": "unavailable", "reason": "route ledger is absent from this older scan"}],
        "run_coverage": run
        if "run" in locals()
        else {"state": "unavailable", "reason": "route ledger is absent from this older scan"},
    }


def run_context(
    state: str, reason: str, store_enabled: bool, mode: str, static: int, rendered: int
) -> dict:
    payload = {
        "schema_version": "rendered_route_run_coverage.v1",
        "state": state,
        "reason": reason,
        "store_enabled": store_enabled,
        "rendering_mode": mode,
        "static_sources": static,
        "rendered_sources": rendered,
    }
    return {
        "kind": RUN_KIND,
        "item_key": "run",
        "payload_version": OUTER_VERSION,
        "payload_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        "completeness": state,
        "reason": reason,
    }
