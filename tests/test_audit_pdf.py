# ruff: noqa: RUF001 -- Intentional Russian test expectations.
from __future__ import annotations

from copy import deepcopy

import pytest

from seohead.reports.audit_pdf import render_audit_pdf_html


def _model(*, state: str = "partial", findings: list[dict] | None = None) -> dict:
    rows = findings or [
        {
            "source_ref": {
                "collection": "/findings",
                "index": 0,
                "id": "finding-example-1",
            },
            "record": {
                "severity": "critical",
                "url": "https://example.invalid/каталог/",
                "status_code": 404,
                "occurrences_count": 2,
                "fix_hint": "Restore the missing destination.",
                "details": {"anchor": "Каталог <ссылок>"},
                "locations": [{"source_url": "https://example.invalid/", "anchor": "Каталог"}],
                "remediation_status": "open",
            },
            "display": {
                "check_key": "BROKEN_INTERNAL_LINK",
                "title": "Broken internal link",
                "observation": "The saved audit recorded a missing destination.",
                "reproduction": "The synthetic URL returned HTTP 404.",
            },
        }
    ]
    return {
        "schema": "seohead.technical-audit-pdf/1",
        "source": {
            "kind": "site-audit",
            "schema": "seohead.site-audit/1",
            "domain": "example.invalid",
            "url": "https://example.invalid/",
            "generated_at": "2026-10-03T12:00:00Z",
            "run_id": "synthetic-run-1",
            "input_diagnostics": [{"kind": "synthetic", "reason": "Fixture metadata"}],
        },
        "run": {
            "state": state,
            "scope": {"urls_crawled": 12, "scope_reason": "Synthetic sample"},
            "reasons": [
                {
                    "source_field": "crawl_finish_reason",
                    "value": "Synthetic stop after the configured page limit.",
                }
            ],
        },
        "summary": {
            "source": {
                "pages_checked": 12,
                "findings_total": len(rows),
                "findings_by_severity": {"critical": 1, "warning": 0, "notice": 0},
            },
            "counts": {
                "findings": {
                    "source_count": len(rows),
                    "declared_total": {"state": "reported", "value": len(rows)},
                    "projected_count": len(rows),
                },
                "pages": {
                    "source_count": 2,
                    "declared_total": {"state": "reported", "value": 2},
                    "projected_count": 2,
                },
                "checks": {
                    "declared_total": {"state": "reported", "value": 4},
                    "ran": {"state": "reported", "source_count": 2, "projected_count": 2},
                    "silent": {"state": "unavailable", "source_count": None, "projected_count": 0},
                    "failed": {"state": "reported", "source_count": 1, "projected_count": 1},
                    "skipped": {"state": "reported", "source_count": 1, "projected_count": 1},
                    "disabled": {"state": "reported", "source_count": 0, "projected_count": 0},
                    "capabilities": {"state": "reported", "source_count": 1, "projected_count": 1},
                    "page_tools_failed": {
                        "state": "unavailable",
                        "source_count": None,
                        "projected_count": 0,
                    },
                },
                "backlog": {
                    "state": "recorded",
                    "source_count": 1,
                    "declared_count": {"state": "reported", "value": 1},
                    "projected_count": 1,
                },
            },
        },
        "coverage": {
            "state": "reported",
            "source_evidence": {
                "state": "reported",
                "record": {"population": {"state": "partial", "reason": "Only 12 synthetic URLs."}},
            },
            "source_check_coverage": {
                "state": "unavailable",
                "record": None,
            },
            "groups": {
                "ran": {
                    "state": "reported",
                    "source_count": 2,
                    "projected_count": 2,
                    "records": ["CHECK_RUN_A", "CHECK_RUN_B"],
                },
                "failed": {
                    "state": "reported",
                    "source_count": 1,
                    "projected_count": 1,
                    "records": [{"tool": "CHECK_A", "error": "Synthetic timeout"}],
                },
                "skipped": {
                    "state": "reported",
                    "source_count": 1,
                    "projected_count": 1,
                    "records": [{"id": "CHECK_B", "reason": "Synthetic input unavailable"}],
                },
                "disabled": {
                    "state": "reported",
                    "source_count": 0,
                    "projected_count": 0,
                    "records": [],
                },
                "capabilities": {
                    "state": "reported",
                    "source_count": 1,
                    "projected_count": 1,
                    "records": [
                        {
                            "check": "SYNTHETIC_GROUP",
                            "state": "measured",
                            "reason": "Synthetic evidence",
                        }
                    ],
                },
                "silent": {
                    "state": "unavailable",
                    "source_count": None,
                    "projected_count": 0,
                    "records": [],
                },
                "page_tools_failed": {
                    "state": "unavailable",
                    "source_count": None,
                    "projected_count": 0,
                    "records": [],
                },
            },
            "checks": [
                {
                    "source_ref": {
                        "collection": "summary.tools_failed",
                        "index": 0,
                        "pointer": "#/summary/tools_failed/0",
                    },
                    "id": "CHECK_A",
                    "state": "failed",
                    "reason": "Synthetic timeout",
                    "record": {"tool": "CHECK_A", "error": "Synthetic timeout"},
                },
                {
                    "source_ref": {
                        "collection": "summary.tools_run",
                        "index": 0,
                        "pointer": "#/summary/tools_run/0",
                    },
                    "id": "CHECK_RUN_A",
                    "state": "ran",
                    "reason": None,
                    "record": "CHECK_RUN_A",
                },
                {
                    "source_ref": {
                        "collection": "summary.tools_run",
                        "index": 1,
                        "pointer": "#/summary/tools_run/1",
                    },
                    "id": "CHECK_RUN_B",
                    "state": "ran",
                    "reason": None,
                    "record": "CHECK_RUN_B",
                },
                {
                    "source_ref": {
                        "collection": "summary.evidence_contract.capability_rows",
                        "index": 0,
                        "pointer": "#/summary/evidence_contract/capability_rows/0",
                    },
                    "id": "SYNTHETIC_GROUP",
                    "state": "unreported",
                    "reason": "Synthetic evidence",
                    "record": {
                        "id": "SYNTHETIC_GROUP",
                        "state": "measured",
                        "reason": "Synthetic evidence",
                    },
                },
                {
                    "source_ref": {
                        "collection": "summary.tools_failed",
                        "index": 1,
                        "pointer": "#/summary/tools_failed/1",
                    },
                    "id": "CHECK_B",
                    "state": "skipped",
                    "reason": "Synthetic input unavailable",
                    "record": {"id": "CHECK_B", "reason": "Synthetic input unavailable"},
                },
            ],
        },
        "findings": rows,
        "pages": [
            {
                "source_ref": {"collection": "/pages", "index": 0},
                "record": {
                    "url": "https://example.invalid/",
                    "status": 200,
                    "title": "Synthetic home",
                },
            },
            {
                "source_ref": {"collection": "/pages", "index": 1},
                "record": {
                    "url": "https://example.invalid/last/",
                    "status": 200,
                    "title": "Final synthetic page",
                },
            },
        ],
        "backlog": {
            "state": "recorded",
            "source_count": 1,
            "declared_count": {"state": "reported", "value": 1},
            "projected_count": 1,
            "project": "Synthetic project",
            "status": {"state": "recorded", "counts": {"total": 1}},
            "items": [
                {
                    "source_ref": {
                        "collection": "summary.project_coverage.status.items",
                        "index": 0,
                        "pointer": "#/summary/project_coverage/status/items/0",
                        "id": "task-1",
                    },
                    "record": {
                        "id": "task-1",
                        "title": "Review the sample page",
                        "state": "not_run",
                        "verification_status": "not_requested",
                    },
                }
            ],
        },
        "omissions": [],
    }


@pytest.mark.parametrize(
    ("lang", "expected"),
    [("en", "Technical SEO audit"), ("ru", "Технический SEO-аудит")],
)
def test_renders_localized_audit_report_with_neutral_branding(lang, expected):
    html = render_audit_pdf_html(
        _model(), lang=lang, brand={"accent": "#1565C0", "name": "Demo label"}
    )

    assert f'<html lang="{lang}">' in html
    assert f"<h1>{expected}</h1>" in html
    assert "Demo label" in html
    assert "13.333in 7.5in" in html
    assert "counter(page)" in html
    assert "<svg" in html
    if lang == "ru":
        assert "Аудит сайта" in html
        assert "Критические" in html
        assert "Число повторений" in html
        assert "Проверка исправления" in html
        assert "Покрытие проверок по источнику" in html
        assert (
            "\u041d\u0435 \u0437\u0430\u043f\u0443\u0441\u043a\u0430\u043b\u043e\u0441\u044c"
            in html
        )
        assert "not_run" not in html


@pytest.mark.parametrize(
    ("lang", "label", "status"),
    [
        ("en", "Project checklist items", "Not requested"),
        ("ru", "Задачи проекта", "Не запрашивалось"),
    ],
)
def test_unrequested_backlog_kpi_is_not_presented_as_zero(lang, label, status):
    model = _model()
    model["backlog"]["state"] = "not_requested"
    model["summary"]["counts"]["backlog"] = {
        "state": "not_requested",
        "source_count": None,
        "declared_count": {"state": "not_requested", "value": None},
        "projected_count": 0,
    }

    html = render_audit_pdf_html(model, lang=lang)

    assert f"<span>{label}</span><strong>{status}</strong>" in html
    assert f"<span>{label}</span><strong>0</strong>" not in html


def test_partial_scope_and_each_coverage_state_are_visible():
    html = render_audit_pdf_html(_model())

    assert "This audit is partial" in html
    assert "Synthetic stop after the configured page limit." in html
    assert "Synthetic timeout" in html
    assert "Synthetic input unavailable" in html
    assert "Only 12 synthetic URLs." in html
    assert "Source check coverage" in html
    assert "SYNTHETIC_GROUP" in html


def test_coverage_reported_does_not_mean_every_check_passed():
    model = _model()
    model["coverage"]["state"] = "reported"
    html = render_audit_pdf_html(model)

    assert "Coverage metadata recorded; individual checks may still be unavailable." in html
    assert "Synthetic timeout" in html
    assert "Synthetic input unavailable" in html


def test_nested_count_groups_and_unavailable_coverage_remain_distinct():
    model = _model()
    model["summary"]["counts"]["findings"]["declared_total"]["value"] = 9
    html = render_audit_pdf_html(model)

    assert "Coverage records" in html
    assert "Declared: 9" in html
    assert "Source evidence" in html
    assert ">Unavailable</td>" in html
    assert "Source check coverage" in html
    assert "Recorded" in html
    assert "Page checks failed" in html
    assert "CHECKS_A" not in html
    assert "<td>task-1</td><td>Review the sample page</td>" in html


def test_summary_card_separates_coverage_rows_from_declared_check_inventory():
    html = render_audit_pdf_html(_model())

    assert "Coverage records" in html
    assert ">5</strong>" in html
    assert "Check inventory: 4" in html


def test_capability_state_uses_measurement_state_from_source_record():
    model = _model()
    model["coverage"]["checks"] = [
        {
            "source_ref": {
                "collection": "summary.evidence_contract.capability_rows",
                "index": 0,
                "pointer": "#/summary/evidence_contract/capability_rows/0",
            },
            "id": "TITLE_MISSING",
            "state": "unreported",
            "reason": "source evidence was not available",
            "record": {
                "check": "TITLE_MISSING",
                "state": "unavailable",
                "reason": "source evidence was not available",
            },
        }
    ]
    html = render_audit_pdf_html(model)

    assert "TITLE_MISSING" in html
    assert ">Unavailable</td>" in html
    assert "source evidence was not available" in html


def test_backlog_wrapper_keeps_source_reference_and_record_fields():
    html = render_audit_pdf_html(_model())

    assert "<td>task-1</td><td>Review the sample page</td>" in html
    assert "<td>Not run</td><td>Not requested</td>" in html


def test_page_tool_failures_are_rendered_from_model_coverage_rows():
    model = _model()
    model["coverage"]["groups"]["page_tools_failed"] = {
        "state": "reported",
        "source_count": 1,
        "projected_count": 1,
        "records": [{"tool": "HTML_TOOL", "failed_pages": 3, "pages_checked": 3}],
    }
    model["summary"]["counts"]["checks"]["page_tools_failed"] = {
        "state": "reported",
        "source_count": 1,
        "projected_count": 1,
    }
    model["coverage"]["checks"].append(
        {
            "source_ref": {
                "collection": "summary.page_tools_failed",
                "index": 0,
                "pointer": "#/summary/page_tools_failed/0",
            },
            "id": "HTML_TOOL",
            "state": "failed",
            "reason": "All 3 synthetic pages failed",
            "record": {"tool": "HTML_TOOL", "failed_pages": 3, "pages_checked": 3},
        }
    )
    html = render_audit_pdf_html(model)

    assert "Page checks failed" in html
    assert "HTML_TOOL" in html
    assert "All 3 synthetic pages failed; Errors on 3 of 3 pages" in html


def test_nested_summary_and_coverage_evidence_are_readable_without_raw_json():
    model = _model()
    model["summary"]["source"].update(
        {
            "tools_run": ["parse", "robots"],
            "tools_failed": [{"tool": "robots_check", "error": "Synthetic unavailable response"}],
            "findings_by_severity": {"critical": 1, "warning": 0},
        }
    )
    model["coverage"]["source_evidence"]["record"] = {
        "scan_identity_state": "partial",
        "scan_uuid": "synthetic-scan-1",
        "evidence_contract": {
            "capability_rows": [
                {"check": "TITLE_MISSING", "state": "measured", "reason": "Synthetic fixture"}
            ]
        },
    }
    model["coverage"]["source_evidence"]["source_ref"] = {"pointer": "#/summary/evidence_contract"}
    model["coverage"]["checks"] = [
        {
            "source_ref": {
                "collection": "summary.tools_run",
                "index": 0,
                "pointer": "#/summary/tools_run/0",
            },
            "id": "parse",
            "state": "ran",
            "record": "parse",
        },
        {
            "source_ref": {
                "collection": "summary.tools_failed",
                "index": 0,
                "pointer": "#/summary/tools_failed/0",
            },
            "id": "robots_check",
            "state": "failed",
            "reason": "Synthetic unavailable response",
            "record": {"tool": "robots_check", "error": "Synthetic unavailable response"},
        },
        {
            "source_ref": {
                "collection": "summary.evidence_contract.capability_rows",
                "index": 0,
                "pointer": "#/summary/evidence_contract/capability_rows/0",
            },
            "id": "TITLE_MISSING",
            "state": "measured",
            "reason": "Synthetic fixture",
            "record": {
                "check": "TITLE_MISSING",
                "state": "measured",
                "reason": "Synthetic fixture",
            },
        },
    ]

    html = render_audit_pdf_html(model)

    for expected in (
        "Scan identity status: partial",
        "Scan ID: synthetic-scan-1",
        "parse",
        "robots_check",
        "Synthetic unavailable response",
        "1 Capability records listed below",
        "TITLE_MISSING",
        "Synthetic fixture",
        "#/summary/evidence_contract",
        "#/summary/tools_failed/0",
    ):
        assert expected in html
    assert '{"critical":1,"warning":0}' not in html
    assert '{"capability_rows"' not in html
    assert "Source reference" in html
    assert '{"tool":"robots_check"' not in html


@pytest.mark.parametrize(
    ("check", "title", "reason", "expected_title", "expected_reason"),
    [
        (
            "TITLE_MISSING",
            "Title element is missing",
            "no stable saved-evidence reference is present in this audit",
            "Отсутствует заголовок страницы",
            "В этом аудите нет стабильной ссылки на сохранённое свидетельство",
        ),
        (
            "SCHEMA_MISSING",
            "Audit finding",
            "saved evidence is unavailable",
            "Проблема аудита",
            "Сохранённое свидетельство недоступно",
        ),
    ],
)
def test_generated_finding_fallbacks_are_localized_in_russian(
    check, title, reason, expected_title, expected_reason
):
    model = _model()
    model["findings"][0]["display"].update(
        {
            "check_key": check,
            "title": title,
            "evidence_reference": {"state": "unavailable", "reason": reason},
        }
    )

    html = render_audit_pdf_html(model, lang="ru")

    assert expected_title in html
    assert expected_reason in html
    assert title not in html
    assert reason not in html


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        (
            "en",
            "Completed within the recorded scope; this does not establish exhaustive site coverage.",
        ),
        (
            "ru",
            "Завершён в пределах записанного объёма; это не подтверждает полный обход сайта.",
        ),
    ],
)
def test_complete_run_status_does_not_claim_full_site_coverage(lang, expected):
    model = _model(state="complete")
    model["run"]["scope"]["operation"] = "bounded_site_audit"
    model["run"]["reasons"] = []
    html = render_audit_pdf_html(model, lang=lang)

    assert expected in html
    assert "Bounded site audit" in html if lang == "en" else "Ограниченный аудит сайта" in html


def test_unknown_run_and_absent_counts_remain_unknown():
    model = _model(state="unknown")
    model["summary"]["counts"]["pages"]["projected_count"] = None
    model["summary"]["counts"]["checks"]["ran"] = {
        "state": "unavailable",
        "source_count": None,
        "projected_count": 0,
    }
    model["summary"]["counts"]["checks"]["failed"] = {
        "state": "unavailable",
        "source_count": None,
        "projected_count": 0,
    }
    model["summary"]["counts"]["checks"]["skipped"] = {
        "state": "unavailable",
        "source_count": None,
        "projected_count": 0,
    }
    model["summary"]["counts"]["checks"]["disabled"] = {
        "state": "unavailable",
        "source_count": None,
        "projected_count": 0,
    }
    model["coverage"]["state"] = "unavailable"
    model["coverage"]["checks"] = []
    html = render_audit_pdf_html(model)

    assert 'data-state="unknown"' in html
    assert "The source does not establish whether this audit completed." in html
    assert "Not reported" in html
    assert "Check-state counts were not supplied by the source audit." in html


def test_render_escapes_source_text_and_preserves_raw_evidence_fields():
    model = _model()
    model["findings"][0]["record"]["details"] = {"payload": "<script>alert(1)</script>"}
    html = render_audit_pdf_html(model)

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "occurrences_count" not in html
    assert "Occurrences count" in html
    assert "https://example.invalid/каталог/" in html
    assert "https://example.invalid/last/" in html


@pytest.mark.parametrize(
    ("lang", "state", "expected_status"),
    [
        ("en", "unavailable", "Status: Unavailable"),
        ("ru", "partial", "Статус: Частичный"),
        ("en", "future_state", "Status: future_state"),
    ],
)
def test_evidence_reference_is_human_readable_and_preserves_state(lang, state, expected_status):
    model = _model()
    model["findings"][0]["display"]["evidence_reference"] = {
        "state": state,
        "reason": "no stable saved-evidence reference is present",
        "id": "evidence-1",
        "source_table": "page_observations",
        "observation_id": "observation-1",
        "role": "finding",
    }
    original = deepcopy(model)

    html = render_audit_pdf_html(model, lang=lang)

    assert expected_status in html
    assert "no stable saved-evidence reference is present" in html
    assert (
        "Evidence ID: evidence-1" in html
        if lang == "en"
        else "Идентификатор свидетельства: evidence-1" in html
    )
    assert (
        "Source table: page_observations" in html
        if lang == "en"
        else "Таблица источника: page_observations" in html
    )
    assert (
        "Observation ID: observation-1" in html
        if lang == "en"
        else "Идентификатор наблюдения: observation-1" in html
    )
    assert "Role: finding" in html if lang == "en" else "Роль: finding" in html
    assert '{"id":"evidence-1"' not in html
    assert model == original


def test_large_synthetic_findings_keep_first_and_last_records_without_caps():
    findings = [
        {
            "source_ref": {
                "collection": "/findings",
                "index": index,
                "id_if_present": f"sample-{index:04d}",
            },
            "record": {
                "severity": "warning",
                "url": f"https://example.invalid/sample-{index:04d}/",
            },
            "display": {"title": f"Synthetic finding {index:04d}"},
        }
        for index in range(150)
    ]
    html = render_audit_pdf_html(_model(findings=findings))

    assert html.count('class="finding-card severity-warning"') == 150
    assert "Synthetic finding 0000" in html
    assert "Synthetic finding 0149" in html
    assert "sample-0149" in html


@pytest.mark.parametrize("model", [{}, {"schema": "seohead.technical-audit-pdf/2"}])
def test_rejects_unrecognized_model_schema(model):
    with pytest.raises(ValueError, match=r"seohead\.technical-audit-pdf/1"):
        render_audit_pdf_html(model)


def test_rejects_unsupported_language():
    with pytest.raises(ValueError, match="unsupported report language"):
        render_audit_pdf_html(_model(), lang="fr")
