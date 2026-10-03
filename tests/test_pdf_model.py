"""Offline tests for the lossless semantic technical-audit PDF model."""

from __future__ import annotations

import copy

import pytest

from seohead.audit.site import SCHEMA as SITE_AUDIT_SCHEMA
from seohead.projects.coverage import (
    coverage_status,
    initialize_coverage,
    record_execution,
    update_item,
)
from seohead.projects.workspace import create_project
from seohead.reports.pdf_model import SCHEMA, build_pdf_model


def _site_audit(**overrides):
    document = {
        "ok": True,
        "schema": SITE_AUDIT_SCHEMA,
        "domain": "audit.example.test",
        "url": "https://audit.example.test/",
        "generated_at": "2026-10-03T12:00:00Z",
        "findings": [],
        "pages": [],
        "summary": {
            "pages_checked": 0,
            "findings_total": 0,
            "findings_by_severity": {"critical": 0, "warning": 0, "notice": 0},
        },
    }
    document.update(overrides)
    return document


def _sf_audit(**overrides):
    document = {
        "schema_version": "2.0",
        "run": {
            "project": "audit.example.test",
            "source": "https://audit.example.test/",
            "scan_uuid": "scan-fixture-1",
            "generated_at": "2026-10-03T12:00:00Z",
            "crawl_valid": True,
            "crawl_partial": False,
        },
        "summary": {
            "totals": {"urls_crawled": 0, "issues_total": 0},
            "by_severity": {"critical": 0, "warning": 0, "notice": 0},
            "by_check": {},
        },
        "issues": [],
        "pages": [],
        "groups": [],
    }
    document.update(overrides)
    return document


def test_site_audit_model_preserves_identity_rows_and_declared_counts():
    finding = {
        "id": "finding-1",
        "source": "render_check",
        "severity": "warning",
        "url": "https://audit.example.test/page",
        "text": "Recorded observation",
        "check": "TITLE_MISSING",
        "details": {"source_value": "retained"},
    }
    page = {"url": "https://audit.example.test/page", "status": 200, "title": "Example"}
    document = _site_audit(
        findings=[finding],
        pages=[page],
        summary={
            "pages_checked": 17,
            "findings_total": 9,
            "findings_by_severity": {"critical": 1, "warning": 2, "notice": 6},
        },
    )

    model = build_pdf_model(document)

    assert model["schema"] == SCHEMA
    assert model["source"] == {
        "kind": "site-audit",
        "schema": SITE_AUDIT_SCHEMA,
        "domain": "audit.example.test",
        "url": "https://audit.example.test/",
        "generated_at": "2026-10-03T12:00:00Z",
        "run_id": None,
        "input_diagnostics": [],
    }
    assert model["summary"]["counts"]["findings"] == {
        "source_count": 1,
        "declared_total": {"state": "reported", "value": 9},
        "projected_count": 1,
        "declared_total_source_field": "findings_total",
        "declared_by_severity": {
            "state": "reported",
            "value": {"critical": 1, "warning": 2, "notice": 6},
        },
    }
    assert model["summary"]["counts"]["pages"]["source_count"] == 1
    assert model["summary"]["counts"]["pages"]["declared_total"] == {
        "state": "reported",
        "value": 17,
    }
    assert model["findings"][0]["source_ref"] == {
        "collection": "findings",
        "index": 0,
        "pointer": "#/findings/0",
        "id": "finding-1",
    }
    assert model["findings"][0]["record"] == finding
    assert model["findings"][0]["display"]["observation"] == "Recorded observation"
    assert model["pages"][0]["record"] == page
    assert model["pages"][0]["source_ref"]["pointer"] == "#/pages/0"
    # Declared totals may disagree with the source arrays. Keep both values.
    assert model["omissions"] == []


def test_missing_summary_count_is_unavailable_not_zero():
    model = build_pdf_model(_site_audit(summary={}))
    assert model["summary"]["counts"]["findings"]["source_count"] == 0
    assert model["summary"]["counts"]["findings"]["projected_count"] == 0
    assert model["summary"]["counts"]["findings"]["declared_total"] == {
        "state": "not_reported",
        "value": None,
    }


def test_zero_findings_and_empty_pages_are_measured_empty_collections():
    model = build_pdf_model(_site_audit())
    assert model["summary"]["counts"]["findings"]["source_count"] == 0
    assert model["summary"]["counts"]["findings"]["declared_total"]["value"] == 0
    assert model["summary"]["counts"]["pages"]["source_count"] == 0
    assert model["summary"]["counts"]["pages"]["declared_total"]["value"] == 0
    assert model["findings"] == []
    assert model["pages"] == []


def test_sf_model_preserves_full_source_record_and_collection_identity():
    issue = {
        "id": "issue-1",
        "check": "BROKEN_INTERNAL_LINK",
        "severity": "critical",
        "message": "Internal link points to a 4xx URL",
        "target_url": "https://audit.example.test/missing",
        "status_code": 404,
        "occurrences_count": 2,
        "fix_hint": "Update the source links.",
        "locations": [{"source_url": "https://audit.example.test/", "anchor": "Catalog"}],
        "details": {"complete_values": list(range(15))},
        "evidence": {"source_table": "issues", "observation_id": "observation-1"},
        "custom_source_field": "must survive",
    }
    page = {"url": "https://audit.example.test/", "status_code": 200, "metrics": {"title": "Home"}}
    document = _sf_audit(
        issues=[issue],
        pages=[page],
        summary={
            "totals": {"urls_crawled": 1, "issues_total": 1},
            "by_severity": {"critical": 1, "warning": 0, "notice": 0},
            "by_check": {"BROKEN_INTERNAL_LINK": 1},
        },
    )
    before = copy.deepcopy(document)

    model = build_pdf_model(document)

    assert model["source"]["kind"] == "sf-audit"
    assert model["source"]["schema"] == "2.0"
    assert model["source"]["run_id"] == "scan-fixture-1"
    assert model["run"]["state"] == "complete"
    assert model["findings"][0]["source_ref"]["pointer"] == "#/issues/0"
    assert model["findings"][0]["source_ref"]["id"] == "issue-1"
    assert model["findings"][0]["record"] == issue
    assert model["findings"][0]["record"]["details"]["complete_values"] == list(range(15))
    assert any(
        "5 more values omitted" in item for item in model["findings"][0]["display"]["details"]
    )
    assert model["pages"][0]["record"] == page
    assert model["summary"]["counts"]["findings"]["declared_total"] == {
        "state": "reported",
        "value": 1,
    }
    assert document == before


def test_run_scope_states_are_explicit_and_partial_failed_reasons_survive():
    partial = _sf_audit(
        run={
            "source": "https://audit.example.test/",
            "crawl_valid": True,
            "crawl_partial": True,
            "crawl_finish_reason": "url_limit",
            "crawl_scope_note": "2 of 20 declared URLs",
        }
    )
    model = build_pdf_model(partial)
    assert model["run"]["state"] == "partial"
    assert model["run"]["partial"] == {"state": "reported", "value": True}
    assert {item["value"] for item in model["run"]["reasons"]} == {"url_limit"}
    assert model["run"]["scope"]["crawl_scope_note"] == "2 of 20 declared URLs"

    failed = _sf_audit(
        run={
            "source": "https://audit.example.test/",
            "crawl_valid": False,
            "crawl_invalid_reason": "no response",
        }
    )
    failed_model = build_pdf_model(failed)
    assert failed_model["run"]["state"] == "failed"
    assert {item["value"] for item in failed_model["run"]["reasons"]} == {"no response"}

    unknown = build_pdf_model(_sf_audit(run={"source": "https://audit.example.test/"}))
    assert unknown["run"]["state"] == "unknown"
    assert unknown["run"]["validity"] == {"state": "not_reported", "value": None}


def test_check_coverage_failures_disabled_and_capabilities_remain_distinct():
    document = _sf_audit(
        run={
            "source": "https://audit.example.test/",
            "crawl_valid": True,
            "crawl_partial": False,
            "checks_skipped": [{"id": "SCHEMA_VALIDATE", "reason": "missing dependency"}],
            "checks_disabled": [{"id": "TITLE_LENGTH", "reason": "operator setting"}],
        },
        summary={
            "totals": {"urls_crawled": 2, "issues_total": 0},
            "by_severity": {"critical": 0, "warning": 0, "notice": 0},
            "by_check": {},
            "check_coverage": {
                "checks_total": 4,
                "checks_fired": 0,
                "checks_skipped": 1,
                "checks_disabled": 1,
                "checks_silent": 2,
                "checks_silent_ids": ["CANONICAL_MISSING", "H1_MISSING"],
            },
        },
    )
    model = build_pdf_model(document)
    checks = model["coverage"]["checks"]
    states = {(item["id"], item["state"]) for item in checks}
    assert ("SCHEMA_VALIDATE", "skipped") in states
    assert ("TITLE_LENGTH", "disabled") in states
    assert ("CANONICAL_MISSING", "ran_no_findings") in states
    assert ("H1_MISSING", "ran_no_findings") in states
    assert model["coverage"]["source_check_coverage"]["record"]["checks_total"] == 4
    assert model["summary"]["counts"]["checks"]["declared_total"] == {
        "state": "reported",
        "value": 4,
    }


def test_page_tool_failures_are_normalized_as_failed_coverage_rows():
    model = build_pdf_model(
        _site_audit(
            summary={
                "pages_checked": 3,
                "findings_total": 0,
                "page_tools_failed": [{"tool": "HTML_TOOL", "failed_pages": 3, "pages_checked": 3}],
            }
        )
    )

    failed = model["coverage"]["checks"]
    assert len(failed) == 1
    assert failed[0]["id"] == "HTML_TOOL"
    assert failed[0]["state"] == "failed"
    assert failed[0]["record"] == {
        "tool": "HTML_TOOL",
        "failed_pages": 3,
        "pages_checked": 3,
    }
    assert failed[0]["source_ref"]["pointer"] == "#/summary/page_tools_failed/0"
    assert model["summary"]["counts"]["checks"]["page_tools_failed"]["source_count"] == 1


def test_site_audit_evidence_capabilities_preserve_unavailable_and_not_requested():
    contract = {
        "scan_identity_state": "recorded",
        "scan_uuid": "scan-evidence-1",
        "population": {"urls_crawled": 3, "crawl_partial": True, "scope_reason": "url cap"},
        "capability_rows": [
            {"check": "TITLE_MISSING", "state": "measured", "reason": "captured"},
            {"check": "CRUX_ORIGIN", "state": "not_requested", "reason": "no origin query"},
            {"check": "GSC_URL_INSPECTION", "state": "unavailable", "reason": "not configured"},
        ],
    }
    document = _site_audit(
        pages=[{"url": "https://audit.example.test/", "status": 200}],
        summary={"pages_checked": 1, "findings_total": 0, "evidence_contract": contract},
    )
    model = build_pdf_model(document)
    assert model["source"]["run_id"] == "scan-evidence-1"
    assert model["coverage"]["source_evidence"]["record"] == contract
    capabilities = model["coverage"]["groups"]["capabilities"]["records"]
    assert [item["state"] for item in capabilities] == ["measured", "not_requested", "unavailable"]
    assert [item["state"] for item in model["coverage"]["checks"]] == [
        "measured",
        "not_requested",
        "unavailable",
    ]
    assert model["summary"]["counts"]["checks"]["capabilities"]["source_count"] == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [("findings_total", -2), ("pages_checked", -1), ("findings_total", "3")],
)
def test_invalid_site_declared_totals_are_rejected(field, value):
    with pytest.raises(ValueError, match="non-negative integer"):
        build_pdf_model(_site_audit(summary={field: value}))


@pytest.mark.parametrize("count", [0, -1, "many", True])
def test_invalid_sf_by_check_counts_are_rejected(count):
    document = _sf_audit(
        summary={
            "totals": {"urls_crawled": 1, "issues_total": 0},
            "by_severity": {"critical": 0},
            "by_check": {"TITLE_MISSING": count},
            "check_coverage": {
                "checks_total": 1,
                "checks_silent_ids": ["TITLE_MISSING"],
            },
        }
    )
    with pytest.raises(ValueError, match="positive integer"):
        build_pdf_model(document)


@pytest.mark.parametrize("count", [-1, "1", True])
def test_invalid_sf_declared_totals_are_rejected(count):
    document = _sf_audit(
        summary={
            "totals": {"urls_crawled": 0, "issues_total": count},
            "by_severity": {},
            "by_check": {},
        }
    )
    with pytest.raises(ValueError, match="non-negative integer"):
        build_pdf_model(document)


def test_check_cannot_be_both_fired_and_silent():
    document = _sf_audit(
        summary={
            "totals": {"urls_crawled": 1, "issues_total": 3},
            "by_severity": {"critical": 3},
            "by_check": {"H2_DUPLICATE": 3},
            "check_coverage": {
                "checks_total": 1,
                "checks_fired": 1,
                "checks_skipped": 0,
                "checks_disabled": 0,
                "checks_silent": 1,
                "checks_silent_ids": ["H2_DUPLICATE"],
            },
        }
    )

    with pytest.raises(ValueError, match="by_check and checks_silent_ids"):
        build_pdf_model(document)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("checks_total", "many", "non-negative integer"),
        ("checks_fired", -1, "non-negative integer"),
        ("checks_silent", True, "non-negative integer"),
    ],
)
def test_check_coverage_counts_are_typed_and_non_negative(field, value, message):
    document = _sf_audit(
        summary={
            "totals": {"urls_crawled": 0, "issues_total": 0},
            "by_severity": {"critical": 0},
            "by_check": {},
            "check_coverage": {field: value},
        }
    )

    with pytest.raises(ValueError, match=message):
        build_pdf_model(document)


def test_sf_severity_and_nested_total_counts_are_typed_and_non_negative():
    bad_severity = _sf_audit(
        summary={
            "totals": {"urls_crawled": 0, "issues_total": 0},
            "by_severity": {"critical": -3},
            "by_check": {},
        }
    )
    with pytest.raises(ValueError, match=r"summary\.by_severity\.critical"):
        build_pdf_model(bad_severity)

    bad_representation = _sf_audit(
        summary={
            "totals": {
                "urls_crawled": 0,
                "issues_total": 0,
                "pages_by_representation": {"static": "many"},
            },
            "by_severity": {},
            "by_check": {},
        }
    )
    with pytest.raises(ValueError, match=r"pages_by_representation\['static'\]"):
        build_pdf_model(bad_representation)


def test_project_checklist_and_verification_status_are_included_only_when_present(tmp_path):
    plain = build_pdf_model(_site_audit())
    assert plain["backlog"]["state"] == "not_requested"
    assert plain["backlog"]["source_count"] is None
    assert plain["summary"]["counts"]["backlog"]["source_count"] is None

    root = tmp_path / "project"
    create_project(root, "https://audit.example.test/")
    initialize_coverage(root)
    revision = coverage_status(root)["revision"]
    update_item(
        root, {"id": "custom:verify", "title": "Verify saved finding"}, expected_revision=revision
    )
    revision = coverage_status(root)["revision"]
    record_execution(
        root,
        "custom:verify",
        {
            "status": "succeeded",
            "reason": "Reviewed synthetic evidence",
            "reviewer": "Fixture reviewer",
            "signoff": True,
        },
        expected_revision=revision,
    )

    expected_items = coverage_status(root)["items"]
    model = build_pdf_model(_site_audit(), project=str(root))
    assert model["backlog"]["state"] == "recorded"
    assert model["backlog"]["source_count"] == len(expected_items)
    assert model["backlog"]["projected_count"] == len(expected_items)
    item = next(
        entry["record"]
        for entry in model["backlog"]["items"]
        if entry["record"]["id"] == "custom:verify"
    )
    assert item["id"] == "custom:verify"
    assert item["attempt_status"] == "succeeded"
    assert item["complete"] is True
    assert item["reason"] == "Reviewed synthetic evidence"


def test_large_source_arrays_are_conserved_without_caps():
    size = 1205
    document = _site_audit(
        findings=[
            {
                "id": f"finding-{index}",
                "severity": "notice",
                "source": "fixture",
                "text": f"Finding {index}",
            }
            for index in range(size)
        ],
        pages=[
            {"url": f"https://audit.example.test/page/{index}", "status": 200}
            for index in range(size)
        ],
        summary={
            "pages_checked": size,
            "findings_total": size,
            "findings_by_severity": {"critical": 0, "warning": 0, "notice": size},
        },
    )
    model = build_pdf_model(document)
    assert model["summary"]["counts"]["findings"]["source_count"] == size
    assert model["summary"]["counts"]["findings"]["projected_count"] == size
    assert model["summary"]["counts"]["pages"]["source_count"] == size
    assert model["summary"]["counts"]["pages"]["projected_count"] == size
    assert model["findings"][-1]["source_ref"]["pointer"] == f"#/findings/{size - 1}"
    assert model["pages"][-1]["source_ref"]["pointer"] == f"#/pages/{size - 1}"


@pytest.mark.parametrize(
    "document, message",
    [
        (
            {"schema": "seohead.site-audit/999", "findings": [], "pages": [], "summary": {}},
            "schema",
        ),
        (
            {
                "schema": SITE_AUDIT_SCHEMA,
                "findings": ["not an object"],
                "pages": [],
                "summary": {},
            },
            "finding objects",
        ),
        (
            {"schema": SITE_AUDIT_SCHEMA, "findings": [], "pages": [None], "summary": {}},
            "page objects",
        ),
    ],
)
def test_unsupported_or_malformed_documents_are_rejected(document, message):
    with pytest.raises(ValueError, match=message):
        build_pdf_model(document)


@pytest.mark.parametrize(
    "document, message",
    [
        (_sf_audit(summary={"totals": "wrong type"}), "summary.totals"),
        (
            _site_audit(
                summary={
                    "findings_total": 0,
                    "evidence_contract": {"capability_rows": ["wrong type"]},
                }
            ),
            "capability_rows",
        ),
        (_site_audit(summary={"page_tools_failed": ["wrong type"]}), "page_tools_failed"),
        (_sf_audit(run={"checks_disabled": "wrong type"}), "run.checks_disabled"),
    ],
)
def test_malformed_optional_coverage_containers_are_rejected(document, message):
    with pytest.raises(ValueError, match=message):
        build_pdf_model(document)
