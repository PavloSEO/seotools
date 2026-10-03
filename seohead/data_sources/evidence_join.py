"""Versioned normalized join and compatibility decision for imported evidence.

``seohead.tools.external_join`` already joins a crawl against a URL-keyed CSV
under ``external_join.v1``: strict keys by default, every relaxation an
explicit opt-in. That contract stays untouched for its callers. This module is
the normalized-evidence counterpart for issue #781:

- :func:`join_evidence` joins crawl pages to a
  ``seohead.normalized-evidence.v1`` document. Evidence rows keep their
  declared grain — matched entries group the evidence rows at a key instead of
  forming a Cartesian product — and normalization collisions on both sides
  are reported rather than hidden. Matched, crawl-only, external-only and
  unkeyable populations are all retained with their provenance.
- :func:`evidence_compatibility` is a pure decision over two normalized
  documents: ``compatible``, ``incompatible`` or ``unknown`` with
  machine-readable reasons across period, timezone boundary policy, reporting
  identity, attribution, engine/search scope, grain and collection coverage.
  A declared cross-source policy may juxtapose distinct metrics (GSC clicks
  beside GA4 sessions) on labeled axes — never summed, never interchangeable.
  Quadrant eligibility for #835 additionally requires a documented boundary
  policy and a measured numeric value on both sources at the same URL key;
  zeros count, nulls, missing rows and suppressed values do not.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from seohead.data_sources.evidence_import import (
    NORMALIZED_FORMAT,
    EvidenceImportError,
)
from seohead.tools.external_join import normalize_join_key

JOIN_FORMAT = "seohead.evidence-join.v1"
COMPATIBILITY_FORMAT = "seohead.evidence-compatibility.v1"

_URL_POLICY_KEYS = {"ignore_query", "ignore_scheme", "casefold_path"}
BOUNDARY_POLICIES = ("strict", "local_calendar")
# Coverage that still permits comparison but must stay labeled.
_DEGRADED_STATES = {"partial"}
_UNUSABLE_STATES = {"failed", "skipped", "not_configured"}
_POLICY_KEYS = {"boundary_policy", "cross_source", "quadrant"}
# Provenance aspects whose values identify the client property or site; a
# public response replaces them with a redaction marker.
_PRIVATE_AXES = ("reporting_identity", "site_origin")


def _key_fn(policy: dict[str, bool]):
    def key(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        return normalize_join_key(
            value,
            ignore_query=policy["ignore_query"],
            ignore_scheme=policy["ignore_scheme"],
            casefold_path=policy["casefold_path"],
        )

    return key


def _row_join_key(row: dict[str, Any], key_fn) -> str | None:
    url = row.get("url") or {}
    if url.get("state") == "unkeyable":
        return None
    candidate = url.get("resolved") or url.get("raw")
    return key_fn(candidate)


def _evidence_header(document: dict[str, Any]) -> dict[str, Any]:
    """The dataset-level provenance a joined population carries with it."""
    mapping = document.get("mapping") or {}
    source = mapping.get("source") or {}
    provenance = document.get("provenance") or {}
    fields = provenance.get("fields") or {}
    return {
        "provider": source.get("provider"),
        "operation": source.get("operation"),
        "reporting_identity": source.get("reporting_identity"),
        "privacy": source.get("privacy"),
        "search_engine": source.get("search_engine"),
        "search_type": source.get("search_type"),
        "attribution": source.get("attribution"),
        "site_origin": source.get("site_origin"),
        "period": mapping.get("period"),
        "timezone": source.get("timezone"),
        "dimensions": mapping.get("dimensions") or [],
        "metrics": mapping.get("metrics") or [],
        "collection": mapping.get("collection") or {},
        "file": provenance.get("file"),
        "artifact_reference": provenance.get("artifact_reference"),
        "field_origins": {name: entry.get("origin") for name, entry in fields.items()},
    }


def join_evidence(
    pages: list[dict[str, Any]],
    document: dict[str, Any],
    *,
    url_policy: dict[str, bool] | None = None,
    crawl: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Join crawl pages to normalized evidence, keeping every population.

    ``pages`` are crawl-side records carrying ``url``; ``document`` is a
    ``seohead.normalized-evidence.v1`` document. URL relaxation flags are
    explicit and off by default; the policy that derived every key is recorded
    under ``url_policy``. Each matched entry lists the evidence rows at the
    key, so a page+query row keeps its grain and no metrics are multiplied.
    """
    if not isinstance(document, dict) or document.get("format") != NORMALIZED_FORMAT:
        raise EvidenceImportError(f"expected a {NORMALIZED_FORMAT} document")
    policy = {"ignore_query": False, "ignore_scheme": False, "casefold_path": False}
    unknown = set(url_policy or {}) - _URL_POLICY_KEYS
    if unknown:
        raise EvidenceImportError(f"url_policy has unsupported keys: {sorted(unknown)}")
    for name, value in (url_policy or {}).items():
        if not isinstance(value, bool):
            raise EvidenceImportError(f"url_policy {name} must be a boolean")
        policy[name] = value
    key_fn = _key_fn(policy)
    pages = list(pages or [])
    rows = list(document.get("rows") or [])

    page_by_key: dict[str, list[dict[str, Any]]] = {}
    page_raw_by_key: dict[str, set[str]] = {}
    unkeyable_pages: list[dict[str, Any]] = []
    for position, page in enumerate(pages):
        raw = page.get("url")
        key = key_fn(raw)
        if key is None:
            unkeyable_pages.append({"position": position, "url": raw, "reason": "unkeyable_url"})
            continue
        page_by_key.setdefault(key, []).append(page)
        page_raw_by_key.setdefault(key, set()).add(raw)

    row_by_key: dict[str, list[dict[str, Any]]] = {}
    row_raw_by_key: dict[str, set[str]] = {}
    unkeyable_rows: list[dict[str, Any]] = []
    for row in rows:
        key = _row_join_key(row, key_fn)
        if key is None:
            unkeyable_rows.append(row)
            continue
        row_by_key.setdefault(key, []).append(row)
        url = row.get("url") or {}
        row_raw_by_key.setdefault(key, set()).add(url.get("resolved") or url.get("raw"))

    shared_keys = sorted(set(page_by_key) & set(row_by_key))
    matched = [
        {
            "key": key,
            "url": page.get("url"),
            "page": page,
            "rows": row_by_key[key],
            "candidate_multiplicity": len(page_by_key[key]) * len(row_by_key[key]),
        }
        for key in shared_keys
        for page in page_by_key[key]
    ]
    crawl_only = [
        {"key": key, "url": page.get("url"), "page": page}
        for key in sorted(set(page_by_key) - set(row_by_key))
        for page in page_by_key[key]
    ]
    external_only = [
        {"key": key, "row": row}
        for key in sorted(set(row_by_key) - set(page_by_key))
        for row in row_by_key[key]
    ]
    collisions = {
        "pages": [
            {"key": key, "raw_values": sorted(raw)}
            for key, raw in sorted(page_raw_by_key.items())
            if len(raw) > 1
        ],
        "rows": [
            {"key": key, "raw_values": sorted(raw)}
            for key, raw in sorted(row_raw_by_key.items())
            if len(raw) > 1
        ],
        "multiplicity": [
            {"key": key, "pages": len(page_by_key[key]), "rows": len(row_by_key[key])}
            for key in shared_keys
            if len(page_by_key[key]) > 1 or len(row_by_key[key]) > 1
        ],
    }
    summary = {
        "pages": len(pages),
        "rows": len(rows),
        "matched": len(matched),
        "matched_pages": sum(len(page_by_key[key]) for key in shared_keys),
        "matched_rows": sum(len(row_by_key[key]) for key in shared_keys),
        "candidate_pairs": sum(len(page_by_key[key]) * len(row_by_key[key]) for key in shared_keys),
        "crawl_only": len(crawl_only),
        "external_only": len(external_only),
        "unkeyable_pages": len(unkeyable_pages),
        "unkeyable_rows": len(unkeyable_rows),
        "page_key_collisions": len(collisions["pages"]),
        "row_key_collisions": len(collisions["rows"]),
        "multi_match_keys": len(collisions["multiplicity"]),
        "ambiguous_rows": sum(1 for row in rows if row.get("ambiguous")),
    }
    return {
        "format": JOIN_FORMAT,
        "url_policy": {"version": "external_join.v1", **policy},
        "crawl": dict(crawl or {}),
        "evidence": _evidence_header(document),
        "matched": matched,
        "crawl_only": crawl_only,
        "external_only": external_only,
        "unkeyable_pages": unkeyable_pages,
        "unkeyable_rows": unkeyable_rows,
        "collisions": collisions,
        "summary": summary,
    }


# --- compatibility decision -----------------------------------------------------


def _field(document: dict[str, Any], name: str) -> Any:
    fields = (document.get("provenance") or {}).get("fields") or {}
    entry = fields.get(name) or {}
    return entry.get("value")


def _window(value: Any) -> tuple[date, date] | None:
    if not isinstance(value, dict):
        return None
    try:
        start = date.fromisoformat(value.get("start_date") or "")
        end = date.fromisoformat(value.get("end_date") or "")
    except ValueError:
        return None
    return (start, end) if start <= end else None


def _validate_policy(policy: Any) -> dict[str, Any]:
    if policy is None:
        return {}
    if not isinstance(policy, dict) or set(policy) - _POLICY_KEYS:
        raise EvidenceImportError(f"comparison policy may name only {sorted(_POLICY_KEYS)}")
    boundary = policy.get("boundary_policy")
    if boundary is not None and boundary not in BOUNDARY_POLICIES:
        raise EvidenceImportError(f"boundary_policy must be one of {BOUNDARY_POLICIES}")
    cross = policy.get("cross_source")
    if cross is not None and cross != "juxtapose":
        raise EvidenceImportError('cross_source must be "juxtapose" when declared')
    quadrant = policy.get("quadrant")
    if quadrant is not None:
        if not isinstance(quadrant, dict) or set(quadrant) - {"left_metric", "right_metric"}:
            raise EvidenceImportError("policy quadrant may name only left_metric and right_metric")
        for name in ("left_metric", "right_metric"):
            if not isinstance(quadrant.get(name), str) or not quadrant[name]:
                raise EvidenceImportError(f"policy quadrant requires a {name} string")
    return policy


def _aspect(reasons: list[dict[str, Any]], verdict: str, reason: str, aspect: str, **extra) -> None:
    reasons.append({"aspect": aspect, "verdict": verdict, "reason": reason, **extra})


def _period_aspect(left: dict, right: dict, reasons: list) -> bool:
    lw = _window(_field(left, "period"))
    rw = _window(_field(right, "period"))
    if lw is None or rw is None:
        _aspect(reasons, "unknown", "period_unknown", "period", unverified=True)
        return False
    if lw == rw:
        _aspect(reasons, "compatible", "period_equal", "period")
        return True
    latest_start, earliest_end = max(lw[0], rw[0]), min(lw[1], rw[1])
    if latest_start <= earliest_end:
        _aspect(reasons, "incompatible", "period_overlap_unequal", "period")
    elif lw[1] + timedelta(days=1) == rw[0] or rw[1] + timedelta(days=1) == lw[0]:
        _aspect(reasons, "incompatible", "period_adjacent", "period")
    else:
        _aspect(reasons, "incompatible", "period_disjoint", "period")
    return False


def _measured_keys(document: dict[str, Any], metric: str) -> set[str]:
    """URL keys where at least one row carries a measured numeric value."""
    keys = set()
    for row in document.get("rows") or []:
        url = row.get("url") or {}
        if url.get("state") != "keyed":
            continue
        entry = (row.get("metrics") or {}).get(metric) or {}
        if entry.get("state") == "measured":
            keys.add(url["normalized"])
    return keys


def _keyed_urls(document: dict[str, Any]) -> set[str]:
    return {
        (row.get("url") or {})["normalized"]
        for row in document.get("rows") or []
        if (row.get("url") or {}).get("state") == "keyed"
    }


def _zero_measured_keys(document: dict[str, Any], metric: str) -> set[str]:
    keys = set()
    for row in document.get("rows") or []:
        url = row.get("url") or {}
        if url.get("state") != "keyed":
            continue
        entry = (row.get("metrics") or {}).get(metric) or {}
        if entry.get("state") == "measured" and entry.get("value") == 0:
            keys.add(url["normalized"])
    return keys


def evidence_compatibility(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether two normalized evidence documents may be compared.

    The verdict is ``compatible``, ``incompatible`` or ``unknown``; it is
    ``incompatible`` when any aspect must never be blended (different windows,
    mismatched engines or identities without a declared policy) and ``unknown``
    when a needed fact cannot be established. ``policy.quadrant`` additionally
    reports measured-zero-aware eligibility for the #835 cohort layer.
    """
    for document in (left, right):
        if not isinstance(document, dict) or document.get("format") != NORMALIZED_FORMAT:
            raise EvidenceImportError(f"expected {NORMALIZED_FORMAT} documents")
    policy = _validate_policy(policy)
    boundary = policy.get("boundary_policy") or "strict"
    juxtapose = policy.get("cross_source") == "juxtapose"
    reasons: list[dict[str, Any]] = []
    juxtaposed = False

    _period_aspect(left, right, reasons)

    # Timezone boundary policy: strict requires the same known calendar;
    # a declared local_calendar policy accepts each source's own labeled dates.
    lt, rt = _field(left, "timezone"), _field(right, "timezone")
    if lt is not None and rt is not None and lt == rt:
        _aspect(reasons, "compatible", "timezone_equal", "timezone")
    elif lt is None or rt is None:
        if boundary == "local_calendar":
            _aspect(reasons, "compatible", "timezone_unverified_local_calendar", "timezone")
        else:
            _aspect(reasons, "unknown", "timezone_unknown", "timezone", unverified=True)
    elif boundary == "local_calendar":
        _aspect(reasons, "compatible", "timezone_differs_local_calendar", "timezone")
    else:
        _aspect(reasons, "incompatible", "timezone_mismatch", "timezone")

    # Reporting identity: same provider and same identity join; anything else
    # needs the declared cross-source juxtaposition, which labels both axes.
    lp, rp = _field(left, "provider"), _field(right, "provider")
    li, ri = _field(left, "reporting_identity"), _field(right, "reporting_identity")
    if lp == rp and lp is not None and li == ri and li is not None:
        _aspect(reasons, "compatible", "same_reporting_source", "reporting_identity")
    elif lp == rp and lp is not None and (li is None or ri is None):
        if juxtapose:
            _aspect(
                reasons,
                "compatible",
                "identity_unverified_juxtaposed",
                "reporting_identity",
                unverified=True,
            )
            juxtaposed = True
        else:
            _aspect(
                reasons,
                "unknown",
                "reporting_identity_unknown",
                "reporting_identity",
                unverified=True,
            )
    elif lp != rp or li != ri:
        if juxtapose:
            _aspect(reasons, "compatible", "juxtaposed_sources", "reporting_identity")
            juxtaposed = True
        else:
            _aspect(reasons, "incompatible", "reporting_source_mismatch", "reporting_identity")

    la, ra = _field(left, "attribution"), _field(right, "attribution")
    if la is not None and ra is not None:
        if la == ra:
            _aspect(reasons, "compatible", "attribution_equal", "attribution")
        elif juxtapose:
            _aspect(reasons, "compatible", "attribution_labeled", "attribution")
            juxtaposed = True
        else:
            _aspect(reasons, "incompatible", "attribution_mismatch", "attribution")
    else:
        if juxtapose:
            _aspect(
                reasons,
                "compatible",
                "attribution_unverified_juxtaposed",
                "attribution",
                unverified=True,
            )
            juxtaposed = True
        else:
            _aspect(reasons, "unknown", "attribution_unknown", "attribution", unverified=True)

    for aspect, field in (("search_engine", "search_engine"), ("search_type", "search_type")):
        lv, rv = _field(left, field), _field(right, field)
        if lv is not None and rv is not None:
            if lv == rv:
                _aspect(reasons, "compatible", f"{field}_equal", aspect)
            elif juxtapose:
                _aspect(reasons, "compatible", f"{field}_labeled", aspect)
                juxtaposed = True
            else:
                _aspect(reasons, "incompatible", f"{field}_mismatch", aspect)
        elif lv is None and rv is None:
            _aspect(reasons, "compatible", f"{field}_not_declared", aspect)
        elif juxtapose:
            # A declared cross-source juxtaposition may pair a search-scoped
            # source with one that carries no search scope (GSC clicks beside
            # GA4 all-traffic sessions). The asymmetry is labeled on each axis,
            # so it is documented rather than unknown.
            _aspect(reasons, "compatible", f"{field}_differs_labeled", aspect)
            juxtaposed = True
        else:
            _aspect(reasons, "unknown", f"{field}_unknown", aspect, unverified=True)

    ldims = set((left.get("mapping") or {}).get("dimensions") or [])
    rdims = set((right.get("mapping") or {}).get("dimensions") or [])
    if ldims == rdims:
        _aspect(reasons, "compatible", "grain_equal", "grain")
    elif juxtapose:
        _aspect(reasons, "compatible", "grain_differs_labeled", "grain")
        juxtaposed = True
    else:
        _aspect(reasons, "incompatible", "grain_mismatch", "grain")

    _aspect(
        reasons,
        "compatible",
        "metrics_remain_distinct_per_source",
        "metrics",
    )

    degraded: list[str] = []
    for side, document in (("left", left), ("right", right)):
        collection = (document.get("mapping") or {}).get("collection") or {}
        state = collection.get("state")
        flags = [name for name in ("sampled", "thresholded", "truncated") if collection.get(name)]
        if state in _UNUSABLE_STATES:
            _aspect(
                reasons,
                "unknown",
                f"collection_{state}",
                "collection",
                side=side,
                unverified=True,
            )
        elif state in _DEGRADED_STATES or flags:
            parts = ([state] if state in _DEGRADED_STATES else []) + flags
            degraded.append(f"{side}:{','.join(parts)}")
    if degraded:
        _aspect(
            reasons,
            "compatible",
            "collection_coverage_degraded",
            "collection",
            degraded=degraded,
        )

    if any(reason["verdict"] == "incompatible" for reason in reasons):
        verdict = "incompatible"
    elif any(reason["verdict"] == "unknown" for reason in reasons):
        verdict = "unknown"
    else:
        verdict = "compatible"

    def _axis(document: dict[str, Any]) -> dict[str, Any]:
        mapping = document.get("mapping") or {}
        source = mapping.get("source") or {}
        return {
            "provider": source.get("provider"),
            "operation": source.get("operation"),
            "reporting_identity": source.get("reporting_identity"),
            "metrics": [metric["name"] for metric in mapping.get("metrics") or []],
            "dimensions": mapping.get("dimensions") or [],
            "period": mapping.get("period"),
            "timezone": source.get("timezone"),
            "attribution": source.get("attribution"),
            "search_engine": source.get("search_engine"),
            "search_type": source.get("search_type"),
        }

    result: dict[str, Any] = {
        "format": COMPATIBILITY_FORMAT,
        "verdict": verdict,
        "juxtaposed": juxtaposed,
        "boundary_policy": boundary if policy else None,
        "reasons": reasons,
        "axes": {"left": _axis(left), "right": _axis(right)},
    }

    quadrant_req = policy.get("quadrant")
    quadrant: dict[str, Any] = {"requested": bool(quadrant_req), "eligible": False, "reasons": []}
    if quadrant_req:
        blockers: list[str] = []
        if not policy.get("boundary_policy"):
            blockers.append("boundary_policy_undeclared")
        if verdict != "compatible":
            blockers.append(f"sources_{verdict}")
        if any(reason.get("unverified") for reason in reasons):
            blockers.append("unverified_scope")
        left_metrics = {m["name"] for m in (left.get("mapping") or {}).get("metrics") or []}
        right_metrics = {m["name"] for m in (right.get("mapping") or {}).get("metrics") or []}
        lm, rm = quadrant_req["left_metric"], quadrant_req["right_metric"]
        if lm not in left_metrics:
            blockers.append("left_metric_undeclared")
        if rm not in right_metrics:
            blockers.append("right_metric_undeclared")
        left_keys = _keyed_urls(left)
        right_keys = _keyed_urls(right)
        left_measured = _measured_keys(left, lm) if lm in left_metrics else set()
        right_measured = _measured_keys(right, rm) if rm in right_metrics else set()
        shared = left_keys & right_keys
        eligible_keys = left_measured & right_measured
        quadrant.update(
            {
                "eligible": not blockers,
                "reasons": blockers,
                "eligible_key_count": len(eligible_keys) if not blockers else 0,
                "shared_key_count": len(shared),
                "left_only_key_count": len(left_keys - right_keys),
                "right_only_key_count": len(right_keys - left_keys),
                "excluded": {
                    "left_unmeasured": len(shared - left_measured),
                    "right_unmeasured": len(shared - right_measured),
                },
                "both_measured_zero_keys": len(
                    eligible_keys & _zero_measured_keys(left, lm) & _zero_measured_keys(right, rm)
                )
                if not blockers
                else 0,
                "degraded": sorted(degraded) or [],
            }
        )
    result["quadrant"] = quadrant
    return result


def public_compatibility(result: dict[str, Any]) -> dict[str, Any]:
    """A compatibility result with client-identifying axis values redacted."""
    axes = {}
    for side, axis in (result.get("axes") or {}).items():
        axes[side] = {
            name: ("redacted" if name in _PRIVATE_AXES and value is not None else value)
            for name, value in axis.items()
        }
    return {**result, "axes": axes}
