"""Offline normalization and join contracts for imported evidence (#781).

Covers the ``seohead.evidence-mapping.v1`` manifest, the
``seohead.normalized-evidence.v1`` document, the normalized join, the pure
compatibility decision, measured-zero quadrant eligibility, and the privacy
boundary on restricted provider evidence. Every fixture is synthetic and the
offline test forbids any socket or DNS work.
"""

from __future__ import annotations

import csv
import json
import socket

import pytest

from seohead.data_sources import evidence_import, evidence_join
from seohead.servers import handlers

MAPPING = evidence_import.MAPPING_FORMAT


def _manifest(**source) -> dict:
    manifest = {
        "format": MAPPING,
        "period": {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        "metrics": [{"name": "clicks", "type": "number", "unit": "count"}],
        "source": {"reporting_identity": "fixture-property", "attribution": "data-driven"},
    }
    if source:
        manifest["source"].update(source)
    return manifest


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def _write_json(path, rows):
    path.write_text(json.dumps({"rows": rows}), encoding="utf-8")
    return str(path)


def _write_xlsx(path, rows, sheet="Data"):
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(list(rows[0]))
    for row in rows:
        worksheet.append(list(row.values()))
    workbook.save(path)
    return str(path)


def _write_envelope(path, evidence, result):
    path.write_text(json.dumps({"evidence": evidence, "result": result}), encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _gsc_envelope(path, rows, **overrides):
    evidence = {
        "format": "seohead.provider-evidence.v1",
        "provider": "gsc",
        "operation": "search_analytics",
        "status": "complete",
        "retrieved_at": "2026-02-01T00:00:00Z",
        "period": "2026-01-01..2026-01-31",
        "dimensions": ["page"],
        "reporting_identity": "fixture-property",
        "sampling": "unknown",
        "pagination": {"truncated": False},
        "artifact_reference": "local-artifact:fixture",
    }
    evidence.update(overrides.pop("evidence", {}))
    result = {
        "rows": rows,
        "dimensions": overrides.pop("dimensions", ["page"]),
        "period": {"start_date": "2026-01-01", "end_date": "2026-01-31"},
    }
    result.update(overrides.pop("result", {}))
    return _write_envelope(path, evidence, result)


def _normalized_csv(path):
    return evidence_import.normalize_file(
        _write_csv(
            path,
            [
                {"url": "https://a.test/one", "clicks": "0"},
                {"url": "https://a.test/two", "clicks": "7"},
            ],
        ),
        manifest=_manifest(),
    )


# --- loaders and equivalent normalization ---------------------------------------


def test_same_rows_normalize_equivalently_across_csv_xlsx_json(tmp_path):
    logical = [
        {"url": "https://a.test/one", "clicks": "0"},
        {"url": "https://a.test/two", "clicks": "7"},
    ]
    manifest = _manifest()
    docs = {
        "csv": evidence_import.normalize_file(
            _write_csv(tmp_path / "rows.csv", logical), manifest=manifest
        ),
        "xlsx": evidence_import.normalize_file(
            _write_xlsx(
                tmp_path / "rows.xlsx",
                [
                    {"url": "https://a.test/one", "clicks": 0},
                    {"url": "https://a.test/two", "clicks": 7},
                ],
            ),
            manifest=manifest,
        ),
        "json": evidence_import.normalize_file(
            _write_json(
                tmp_path / "rows.json",
                [
                    {"url": "https://a.test/one", "clicks": 0},
                    {"url": "https://a.test/two", "clicks": 7},
                ],
            ),
            manifest=manifest,
        ),
    }
    for kind, document in docs.items():
        assert document["format"] == evidence_import.NORMALIZED_FORMAT
        assert document["provenance"]["file"]["kind"] == kind
        assert document["provenance"]["file"]["sha256"]
        assert document["provenance"]["fields"]["period"]["origin"] == "declared"
    keyed = [
        (row["url"]["normalized"], row["metrics"]["clicks"]["state"]) for row in docs["csv"]["rows"]
    ]
    for other in ("xlsx", "json"):
        assert [
            (row["url"]["normalized"], row["metrics"]["clicks"]["state"])
            for row in docs[other]["rows"]
        ] == keyed
    # A numeric zero survives as measured, whatever the source format.
    for document in docs.values():
        assert document["rows"][0]["metrics"]["clicks"]["value"] == 0


def test_zero_stays_measured_while_absence_stays_unavailable(tmp_path):
    rows = [
        {"url": "https://a.test/zero", "clicks": "0"},
        {"url": "https://a.test/blank", "clicks": ""},
        {"url": "https://a.test/suppressed", "clicks": "<5"},
        {"url": "https://a.test/bad", "clicks": "abc"},
        {"url": "https://a.test/null", "clicks": None},
    ]
    doc = evidence_import.normalize_file(
        _write_json(tmp_path / "rows.json", [*rows, {"url": "https://a.test/absent"}]),
        manifest=_manifest(),
    )
    states = {row["url"]["raw"]: row["metrics"]["clicks"] for row in doc["rows"]}
    assert states["https://a.test/zero"]["state"] == "measured"
    assert states["https://a.test/zero"]["value"] == 0
    assert states["https://a.test/blank"]["reason"] == "empty"
    assert states["https://a.test/suppressed"]["reason"] == "suppressed"
    assert states["https://a.test/bad"]["reason"] == "invalid"
    assert states["https://a.test/null"]["reason"] == "null"
    assert states["https://a.test/absent"]["reason"] == "absent"
    assert all(states[raw]["value"] != 0 or raw.endswith("zero") for raw in states)


def test_xlsx_formula_cells_are_not_evaluated(tmp_path):
    path = _write_xlsx(
        tmp_path / "formula.xlsx",
        [{"url": "https://a.test/one", "clicks": "=SUM(1,2)"}],
    )
    doc = evidence_import.normalize_file(path, manifest=_manifest())
    metric = doc["rows"][0]["metrics"]["clicks"]
    assert metric["state"] == "unavailable"
    assert metric["reason"] == "formula"


def test_xlsx_unknown_sheet_is_a_declared_error(tmp_path):
    path = _write_xlsx(tmp_path / "sheets.xlsx", [{"url": "https://a.test/", "clicks": 1}])
    with pytest.raises(evidence_import.EvidenceImportError, match="sheet"):
        evidence_import.normalize_file(path, manifest=_manifest(), sheet="Missing")


def test_manifest_rejects_malformed_and_unknown_fields():
    with pytest.raises(evidence_import.EvidenceImportError, match="format"):
        evidence_import.validate_manifest({"format": "other"})
    with pytest.raises(evidence_import.EvidenceImportError, match="unsupported keys"):
        evidence_import.validate_manifest({"format": MAPPING, "surprise": True})
    with pytest.raises(evidence_import.EvidenceImportError, match="timezone"):
        evidence_import.validate_manifest({"format": MAPPING, "source": {"timezone": "Pacific"}})
    with pytest.raises(evidence_import.EvidenceImportError, match="both"):
        evidence_import.validate_manifest(
            {"format": MAPPING, "period": {"start_date": "2026-01-01"}}
        )
    with pytest.raises(evidence_import.EvidenceImportError, match="unsupported type"):
        evidence_import.validate_manifest(
            {"format": MAPPING, "metrics": [{"name": "label", "type": "string"}]}
        )


# --- provider envelopes ---------------------------------------------------------


def test_gsc_envelope_normalizes_page_and_extra_dimensions(tmp_path):
    path = _gsc_envelope(
        tmp_path / "gsc.json",
        [
            {
                "keys": ["seo", "https://a.test/one", "MOBILE"],
                "clicks": 0,
                "impressions": 3,
            }
        ],
        dimensions=["query", "page", "device"],
        result={"dimensions": ["query", "page", "device"]},
    )
    doc = evidence_import.normalize_file(path)
    assert doc["mapping"]["source"]["timezone"] == "America/Los_Angeles"
    assert doc["provenance"]["fields"]["timezone"] == {
        "value": "America/Los_Angeles",
        "origin": "declared",
        "declared_by": "provider_contract",
    }
    assert doc["mapping"]["source"]["privacy"] == "restricted"
    row = doc["rows"][0]
    assert row["url"]["normalized"] == "https://a.test/one"
    assert row["dimensions"] == {
        "query": "seo",
        "page": "https://a.test/one",
        "device": "MOBILE",
    }
    # Higher grain stays on the row: the natural key keeps query and device.
    assert row["natural_key"] == ["https://a.test/one", "seo", "MOBILE"]
    assert row["metrics"]["clicks"]["value"] == 0
    assert row["metrics"]["impressions"]["value"] == 3


def test_ga4_relative_landing_requires_an_explicit_origin(tmp_path):
    rows = [
        {
            "dimensionValues": [{"value": "/landing?utm=x"}],
            "metricValues": [{"value": "4"}],
        }
    ]
    evidence = {
        "format": "seohead.provider-evidence.v1",
        "provider": "ga4",
        "operation": "landing_pages",
        "status": "complete",
        "retrieved_at": "2026-02-01T00:00:00Z",
        "period": "2026-01-01..2026-01-31",
        "reporting_identity": "fixture-property",
        "pagination": {"truncated": False},
    }
    result = {
        "rows": rows,
        "dimensions": ["landingPagePlusQueryString"],
        "metrics": ["sessions"],
        "period": {"start_date": "2026-01-01", "end_date": "2026-01-31"},
    }
    path = _write_envelope(tmp_path / "ga4.json", evidence, result)
    unbound = evidence_import.normalize_file(path)
    assert unbound["mapping"]["source"]["timezone"] is None
    row = unbound["rows"][0]
    assert row["url"]["state"] == "unkeyable"
    assert row["url"]["reason"] == "missing_site_origin"
    assert row["metrics"]["sessions"]["value"] == 4.0

    bound = evidence_import.normalize_file(path, site_origin="https://a.test")
    bound_row = bound["rows"][0]
    assert bound_row["url"]["state"] == "keyed"
    assert bound_row["url"]["resolved"] == "https://a.test/landing?utm=x"
    assert bound_row["url"]["normalized"] == "https://a.test/landing?utm=x"
    assert bound["provenance"]["fields"]["site_origin"]["declared_by"] == "manifest"


def test_metrika_flat_rows_keep_attribution_and_engine(tmp_path):
    manifest = _manifest(
        kind="csv",
        provider="metrika",
        operation="aggregate",
        search_engine="yandex",
        attribution="last_click",
        timezone="Europe/Moscow",
        reporting_identity="counter-1",
    )
    manifest["dimensions"] = ["date"]
    manifest["metrics"] = [
        {"name": "visits", "type": "number", "unit": "count"},
        {"name": "pageviews", "type": "number", "unit": "count"},
    ]
    path = _write_csv(
        tmp_path / "metrika.csv",
        [{"url": "https://a.test/", "date": "2026-01-05", "visits": "3", "pageviews": "9"}],
    )
    doc = evidence_import.normalize_file(path, manifest=manifest)
    source = doc["mapping"]["source"]
    assert source["attribution"] == "last_click"
    assert source["search_engine"] == "yandex"
    assert source["timezone"] == "Europe/Moscow"
    assert doc["rows"][0]["metrics"]["visits"]["value"] == 3.0
    assert doc["rows"][0]["dimensions"] == {"date": "2026-01-05"}


def test_provider_envelope_requires_private_file_mode(tmp_path):
    path = tmp_path / "gsc.json"
    path.write_text(
        json.dumps(
            {
                "evidence": {"format": "seohead.provider-evidence.v1", "provider": "gsc"},
                "result": {"rows": [], "dimensions": []},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o644)
    with pytest.raises(evidence_import.EvidenceImportError, match="private"):
        evidence_import.normalize_file(str(path))


def test_unkeyable_url_states_stay_explicit(tmp_path):
    rows = [
        {"url": "https://a.test/good", "clicks": "1"},
        {"url": "not a url", "clicks": "1"},
        {"url": "(not set)", "clicks": "1"},
        {"url": "", "clicks": "1"},
        {"url": None, "clicks": "1"},
    ]
    doc = evidence_import.normalize_file(
        _write_json(tmp_path / "rows.json", rows), manifest=_manifest()
    )
    states = {row["row_index"]: row["url"] for row in doc["rows"]}
    assert states[0]["state"] == "keyed"
    assert states[1]["reason"] == "url_invalid"
    assert states[2]["reason"] == "non_url_value"
    assert states[3]["reason"] == "url_empty"
    assert states[4]["reason"] == "url_absent"
    assert doc["summary"]["unkeyable"] == 4
    assert doc["summary"]["keyed"] == 1


def test_duplicate_natural_keys_are_marked_not_summed(tmp_path):
    rows = [
        {"url": "https://a.test/one", "clicks": "1"},
        {"url": "https://a.test/one", "clicks": "2"},
    ]
    doc = evidence_import.normalize_file(
        _write_json(tmp_path / "rows.json", rows), manifest=_manifest()
    )
    assert doc["summary"]["ambiguous"] == 2
    assert all(row["ambiguous"] for row in doc["rows"])
    reject = _manifest()
    reject["duplicate_policy"] = "reject"
    with pytest.raises(evidence_import.EvidenceImportError, match="duplicate"):
        evidence_import.normalize_file(str(tmp_path / "rows.json"), manifest=reject)


# --- normalized join --------------------------------------------------------------


def _join_doc(tmp_path, rows=None):
    rows = rows or [
        {"url": "https://a.test/matched", "clicks": "0"},
        {"url": "https://a.test/matched?utm=ad", "clicks": "1"},
        {"url": "https://a.test/external-only", "clicks": "2"},
        {"url": "(not set)", "clicks": "3"},
    ]
    return evidence_import.normalize_file(
        _write_json(tmp_path / "rows.json", rows), manifest=_manifest()
    )


def test_join_keeps_every_population_with_provenance(tmp_path):
    doc = _join_doc(tmp_path)
    pages = [
        {"url": "https://a.test/matched", "status_code": 200},
        {"url": "https://a.test/crawl-only", "status_code": 404},
        {"url": "not a url"},
    ]
    join = evidence_join.join_evidence(pages, doc, crawl={"source": "audit", "partial": False})
    assert join["format"] == evidence_join.JOIN_FORMAT
    assert join["url_policy"] == {
        "version": "external_join.v1",
        "ignore_query": False,
        "ignore_scheme": False,
        "casefold_path": False,
    }
    assert join["crawl"] == {"source": "audit", "partial": False}
    assert join["summary"] == {
        "pages": 3,
        "rows": 4,
        "matched": 1,
        "matched_pages": 1,
        "matched_rows": 1,
        "candidate_pairs": 1,
        "crawl_only": 1,
        "external_only": 2,
        "unkeyable_pages": 1,
        "unkeyable_rows": 1,
        "page_key_collisions": 0,
        "row_key_collisions": 0,
        "multi_match_keys": 0,
        "ambiguous_rows": 0,
    }
    matched = join["matched"][0]
    assert matched["key"] == "https://a.test/matched"
    assert matched["rows"][0]["metrics"]["clicks"]["value"] == 0
    # The two raw evidence URLs remain separate rows at their own keys under
    # the strict default policy.
    assert {entry["row"]["url"]["raw"] for entry in join["external_only"]} == {
        "https://a.test/matched?utm=ad",
        "https://a.test/external-only",
    }
    # Dataset provenance rides with every population through the evidence header.
    assert join["evidence"]["period"] == {
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
    }
    assert join["evidence"]["collection"]["state"] == "complete"


def test_join_relaxed_policy_is_explicit_and_reports_collisions(tmp_path):
    doc = _join_doc(tmp_path)
    pages = [{"url": "https://a.test/matched?utm=ad&x=1"}, {"url": "https://a.test/matched?y=2"}]
    join = evidence_join.join_evidence(pages, doc, url_policy={"ignore_query": True})
    assert join["url_policy"]["ignore_query"] is True
    assert join["summary"]["matched"] == 2
    assert join["summary"]["matched_pages"] == 2
    assert join["summary"]["matched_rows"] == 2  # the two evidence rows are listed, not multiplied
    assert join["summary"]["candidate_pairs"] == 4
    assert join["summary"]["page_key_collisions"] == 1
    assert join["summary"]["multi_match_keys"] == 1
    multiplicity = join["collisions"]["multiplicity"][0]
    assert multiplicity == {"key": "https://a.test/matched", "pages": 2, "rows": 2}
    row_collision = join["collisions"]["rows"][0]
    assert row_collision["raw_values"] == [
        "https://a.test/matched",
        "https://a.test/matched?utm=ad",
    ]
    # Grain is never collapsed: each matched page lists both evidence rows.
    assert all(len(entry["rows"]) == 2 for entry in join["matched"])


def test_join_is_deterministic(tmp_path):
    doc = _join_doc(tmp_path)
    pages = [
        {"url": "https://a.test/crawl-only"},
        {"url": "https://a.test/matched"},
    ]
    first = evidence_join.join_evidence(pages, doc)
    second = evidence_join.join_evidence(pages, doc)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- compatibility ----------------------------------------------------------------


def _doc(period, _drop=(), _manifest_extra=None, **source):
    manifest = _manifest()
    manifest["period"] = period
    manifest["source"].update(source)
    manifest.update(_manifest_extra or {})
    for name in _drop:
        manifest["source"].pop(name, None)
    rows = [{"url": "https://a.test/one", "clicks": "1"}]
    return evidence_import.normalize_inline(rows, manifest=manifest)


def test_compatibility_identical_windows_and_scope_are_compatible():
    period = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
    left = _doc(period, timezone="America/Los_Angeles", search_engine="google")
    right = _doc(period, timezone="America/Los_Angeles", search_engine="google")
    result = evidence_join.evidence_compatibility(left, right)
    assert result["verdict"] == "compatible"
    assert result["format"] == evidence_join.COMPATIBILITY_FORMAT


def test_compatibility_windows_adjacent_overlap_and_disjoint_are_incompatible():
    jan = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
    feb = {"start_date": "2026-02-01", "end_date": "2026-02-28"}
    overlap = {"start_date": "2026-01-15", "end_date": "2026-02-15"}
    apart = {"start_date": "2026-03-01", "end_date": "2026-03-31"}
    tz = {"timezone": "America/Los_Angeles"}
    for other, reason in (
        (feb, "period_adjacent"),
        (overlap, "period_overlap_unequal"),
        (apart, "period_disjoint"),
    ):
        result = evidence_join.evidence_compatibility(_doc(jan, **tz), _doc(other, **tz))
        assert result["verdict"] == "incompatible"
        assert any(entry["reason"] == reason for entry in result["reasons"]), reason


def test_compatibility_same_dates_with_unknown_timezone_is_unknown():
    period = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
    left = _doc(period, timezone="America/Los_Angeles")
    right = _doc(period)  # timezone never established
    result = evidence_join.evidence_compatibility(left, right)
    assert result["verdict"] == "unknown"
    assert any(entry["reason"] == "timezone_unknown" for entry in result["reasons"])


def test_compatibility_timezone_mismatch_needs_a_declared_boundary_policy():
    period = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
    left = _doc(period, timezone="America/Los_Angeles")
    right = _doc(period, timezone="Europe/Moscow")
    strict = evidence_join.evidence_compatibility(left, right)
    assert strict["verdict"] == "incompatible"
    relaxed = evidence_join.evidence_compatibility(
        left, right, policy={"boundary_policy": "local_calendar"}
    )
    assert relaxed["verdict"] == "compatible"
    assert any(entry["reason"] == "timezone_differs_local_calendar" for entry in relaxed["reasons"])


def test_compatibility_attribution_and_engine_scope_are_checked():
    period = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
    base = {"timezone": "Europe/Moscow"}
    left = _doc(period, attribution="last_click", search_engine="yandex", **base)
    right = _doc(period, attribution="first_click", search_engine="google", **base)
    result = evidence_join.evidence_compatibility(left, right)
    assert result["verdict"] == "incompatible"
    reasons = {entry["reason"] for entry in result["reasons"]}
    assert "attribution_mismatch" in reasons
    assert "search_engine_mismatch" in reasons

    unknown_attribution = _doc(period, _drop=("attribution",), search_engine="yandex", **base)
    result = evidence_join.evidence_compatibility(left, unknown_attribution)
    assert result["verdict"] == "unknown"


def test_compatibility_distinct_sources_need_the_declared_juxtaposition():
    period = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
    gsc = _doc(
        period,
        _manifest_extra={"row_shape": "flat"},
        provider="gsc",
        search_engine="google",
        timezone="America/Los_Angeles",
        reporting_identity="prop-a",
    )
    ga4 = _doc(
        period,
        _manifest_extra={"row_shape": "flat"},
        provider="ga4",
        timezone="America/Los_Angeles",
        reporting_identity="prop-a",
    )
    refused = evidence_join.evidence_compatibility(gsc, ga4)
    assert refused["verdict"] == "incompatible"
    declared = evidence_join.evidence_compatibility(gsc, ga4, policy={"cross_source": "juxtapose"})
    assert declared["verdict"] == "compatible"
    assert declared["juxtaposed"] is True
    # Each axis is labeled with its own reporting basis; nothing is summed.
    assert declared["axes"]["left"]["provider"] == "gsc"
    assert declared["axes"]["right"]["provider"] == "ga4"


def test_compatibility_failed_or_skipped_collection_is_unusable():
    period = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
    left = _doc(period, timezone="America/Los_Angeles")
    broken = _doc(period, timezone="America/Los_Angeles")
    broken["mapping"]["collection"]["state"] = "failed"
    result = evidence_join.evidence_compatibility(left, broken)
    assert result["verdict"] == "unknown"
    assert any(
        entry["reason"] == "collection_failed" and entry["side"] == "right"
        for entry in result["reasons"]
    )


def test_compatibility_malformed_policy_is_rejected():
    doc = _doc({"start_date": "2026-01-01", "end_date": "2026-01-31"})
    with pytest.raises(evidence_import.EvidenceImportError, match="boundary_policy"):
        evidence_join.evidence_compatibility(doc, doc, policy={"boundary_policy": "fuzzy"})
    with pytest.raises(evidence_import.EvidenceImportError, match="may name only"):
        evidence_join.evidence_compatibility(doc, doc, policy={"mode": "blend"})


# --- quadrant eligibility ----------------------------------------------------------


def _quadrant_docs(tmp_path, left_clicks, right_sessions):
    left_manifest = _manifest(
        provider="gsc",
        timezone="America/Los_Angeles",
        search_engine="google",
        reporting_identity="prop-a",
    )
    left_manifest["row_shape"] = "flat"
    left = evidence_import.normalize_file(
        _write_json(
            tmp_path / "gsc.json",
            [
                {"url": "https://a.test/both", "clicks": left_clicks},
                {"url": "https://a.test/left-only", "clicks": "5"},
            ],
        ),
        manifest=left_manifest,
    )
    right_manifest = _manifest(
        provider="ga4",
        timezone="America/Los_Angeles",
        reporting_identity="prop-a",
    )
    right_manifest["row_shape"] = "flat"
    right_manifest["metrics"] = [{"name": "sessions", "type": "number", "unit": "count"}]
    right = evidence_import.normalize_file(
        _write_json(
            tmp_path / "ga4.json",
            [{"url": "https://a.test/both", "sessions": right_sessions}],
        ),
        manifest=right_manifest,
    )
    return left, right


def test_quadrant_requires_measured_values_and_a_declared_boundary_policy(tmp_path):
    left, right = _quadrant_docs(tmp_path, "0", 0)
    policy = {
        "boundary_policy": "strict",
        "cross_source": "juxtapose",
        "quadrant": {"left_metric": "clicks", "right_metric": "sessions"},
    }
    result = evidence_join.evidence_compatibility(left, right, policy=policy)
    quadrant = result["quadrant"]
    assert quadrant["eligible"] is True
    assert quadrant["eligible_key_count"] == 1
    assert quadrant["both_measured_zero_keys"] == 1
    # A supplied zero counts on both axes; it is not an inferred zero.
    assert left["rows"][0]["metrics"]["clicks"]["value"] == 0.0

    no_policy = evidence_join.evidence_compatibility(
        left,
        right,
        policy={
            "cross_source": "juxtapose",
            "quadrant": {"left_metric": "clicks", "right_metric": "sessions"},
        },
    )
    assert no_policy["quadrant"]["eligible"] is False
    assert "boundary_policy_undeclared" in no_policy["quadrant"]["reasons"]


def test_quadrant_never_counts_unavailable_values_as_zero(tmp_path):
    left, right = _quadrant_docs(tmp_path, "", None)
    policy = {
        "boundary_policy": "strict",
        "cross_source": "juxtapose",
        "quadrant": {"left_metric": "clicks", "right_metric": "sessions"},
    }
    result = evidence_join.evidence_compatibility(left, right, policy=policy)
    quadrant = result["quadrant"]
    assert quadrant["eligible"] is True
    assert quadrant["eligible_key_count"] == 0
    assert quadrant["excluded"] == {"left_unmeasured": 1, "right_unmeasured": 1}
    assert quadrant["both_measured_zero_keys"] == 0


# --- handlers and privacy ------------------------------------------------------------


def test_evidence_normalize_handler_returns_document_for_supplied_files(tmp_path):
    result = handlers.evidence_normalize(
        file=_write_csv(
            tmp_path / "rows.csv",
            [{"url": "https://a.test/one", "clicks": "2"}],
        ),
        mapping=_manifest(),
    )
    assert result["ok"] is True
    assert result["privacy"] == "supplied"
    assert result["document"]["summary"]["keyed"] == 1


def test_evidence_normalize_handler_redacts_restricted_sources(tmp_path):
    path = _gsc_envelope(
        tmp_path / "gsc.json",
        [{"keys": ["https://a.test/secret-page"], "clicks": 9}],
    )
    result = handlers.evidence_normalize(file=path)
    assert result["ok"] is True
    assert result["privacy"] == "restricted"
    assert result["rows_redacted"] is True
    assert "document" not in result
    assert result["summary"]["keyed"] == 1
    fields = result["provenance"]["fields"]
    assert fields["reporting_identity"] == {
        "value": None,
        "origin": "envelope",
        "redacted": True,
    }
    assert "a.test" not in json.dumps(result)


def test_evidence_join_handler_joins_and_compares_offline(tmp_path):
    result = handlers.evidence_join(
        audit={"run": {"crawl_partial": False}, "pages": [{"url": "https://a.test/one"}]},
        evidence=_write_csv(
            tmp_path / "rows.csv",
            [{"url": "https://a.test/one", "clicks": "4"}],
        ),
        compare=_write_csv(
            tmp_path / "ga4.csv",
            [{"url": "https://a.test/one", "sessions": "9"}],
        ),
        mapping={
            **_manifest(
                provider="gsc",
                timezone="America/Los_Angeles",
                search_engine="google",
            ),
            "row_shape": "flat",
        },
        compare_mapping={
            "format": MAPPING,
            "row_shape": "flat",
            "period": {"start_date": "2026-01-01", "end_date": "2026-01-31"},
            "metrics": [{"name": "sessions"}],
            "source": {
                "provider": "ga4",
                "timezone": "America/Los_Angeles",
                "reporting_identity": "fixture-property",
                "attribution": "data-driven",
            },
        },
        policy={"cross_source": "juxtapose"},
    )
    assert result["ok"] is True
    assert result["join"]["summary"]["matched"] == 1
    assert result["join"]["crawl"]["partial"] is False
    compatibility = result["compatibility"]
    assert compatibility["verdict"] == "compatible"
    assert compatibility["juxtaposed"] is True
    # GSC clicks and GA4 sessions remain labeled, distinct source metrics.
    assert compatibility["axes"]["left"]["metrics"] == ["clicks"]
    assert compatibility["axes"]["right"]["metrics"] == ["sessions"]


def test_evidence_join_cli_matches_handler_output(tmp_path, capsys):
    from seohead import cli

    csv_path = _write_csv(tmp_path / "rows.csv", [{"url": "https://a.test/one", "clicks": "4"}])
    pages = json.dumps([{"url": "https://a.test/one"}])
    rc = cli.main(
        [
            "evidence-join",
            "--pages",
            pages,
            "--evidence",
            csv_path,
            "--mapping",
            json.dumps(_manifest()),
        ]
    )
    assert rc == 0
    cli_result = json.loads(capsys.readouterr().out)
    handler_result = handlers.evidence_join(
        pages=json.loads(pages), evidence=csv_path, mapping=_manifest()
    )
    assert cli_result["join"]["summary"] == handler_result["join"]["summary"]


def test_evidence_import_never_touches_network(tmp_path, monkeypatch):
    attempts = []

    def forbidden(label):
        def _blocked(*_args, **_kwargs):
            attempts.append(label)
            raise AssertionError(f"evidence import attempted {label}")

        return _blocked

    monkeypatch.setattr(socket, "socket", forbidden("socket"))
    monkeypatch.setattr(socket, "getaddrinfo", forbidden("dns"))
    import httpx

    monkeypatch.setattr(httpx.Client, "send", forbidden("http"))

    path = _gsc_envelope(
        tmp_path / "gsc.json",
        [{"keys": ["https://a.test/one"], "clicks": 1}],
    )
    document = evidence_import.normalize_file(path)
    joined = evidence_join.join_evidence([{"url": "https://a.test/one"}], document)
    result = handlers.evidence_normalize(file=path)
    assert joined["summary"]["matched"] == 1
    assert result["ok"] is True
    assert attempts == []
