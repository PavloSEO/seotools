"""Tests for seohead/reports/facts.py: the fact() state machine and the
multi-site facts.v1 export it builds.

All fixtures are synthetic and offline; no network access, no real credentials,
no crawling. This module reads documents only.
"""

from __future__ import annotations

import json

import pytest

from seohead.audit.site import SCHEMA as SITE_AUDIT_SCHEMA
from seohead.reports import facts
from seohead.reports.facts import FactError, build_facts_export, fact

# ── fixtures ─────────────────────────────────────────────────────────────

SF_DOCUMENT_A = {
    "schema_version": "2.0",
    "tool": "sf-analyzer",
    "run": {
        "input_mode": "crawl",
        "source": "https://a-example.com/",
        "generated_at": "2026-09-05T00:00:00Z",
        "crawl_partial": False,
    },
    "summary": {
        "totals": {"urls_crawled": 10, "html_indexable": 8, "issues_total": 2},
        "by_severity": {"critical": 1, "warning": 1, "notice": 0},
        "by_check": {"THIN_CONTENT": 2},
        "check_coverage": {"checks_available": 90, "checks_skipped": 5, "checks_disabled": 1},
    },
    "issues": [],
    "pages": [],
    "groups": [],
}

SF_DOCUMENT_PARTIAL = {
    "schema_version": "2.0",
    "tool": "sf-analyzer",
    "run": {
        "input_mode": "crawl",
        "source": "https://c-example.com/",
        "generated_at": "2026-09-05T00:00:00Z",
        "crawl_partial": True,
        "crawl_finish_reason": "hit max_urls before the sitemap was exhausted",
    },
    "summary": {
        "totals": {"urls_crawled": 4, "html_indexable": 0, "issues_total": 0},
        "by_severity": {"critical": 0, "warning": 0, "notice": 0},
    },
    "issues": [],
    "pages": [],
    "groups": [],
}

SITE_DOCUMENT_A = {
    "ok": True,
    "schema": SITE_AUDIT_SCHEMA,
    "url": "https://a-example.com/",
    "domain": "a-example.com",
    "generated_at": "2026-09-05T00:00:00Z",
    "site": {},
    "pages": [],
    "findings": [],
    "summary": {
        "pages_checked": 5,
        "findings_total": 0,
        "findings_by_severity": {"critical": 0, "warning": 0, "notice": 0},
        "tools_run": ["robots_check", "security_check"],
        "tools_failed": [],
    },
}

SITE_DOCUMENT_B = {
    "ok": True,
    "schema": SITE_AUDIT_SCHEMA,
    "url": "https://b-example.com/",
    "domain": "b-example.com",
    "generated_at": "2026-09-05T00:00:00Z",
    "site": {},
    "pages": [],
    "findings": [{"severity": "warning", "text": "x"}],
    "summary": {
        "pages_checked": 3,
        "findings_total": 1,
        "findings_by_severity": {"critical": 0, "warning": 1, "notice": 0},
        "tools_run": ["robots_check"],
        "tools_failed": [{"tool": "security_check", "error": "timeout"}],
    },
}


# ── fact() constructor: the state-machine invariants ────────────────────


def test_fact_measured_accepts_any_value_with_no_reason():
    f = fact(5, "measured", source="s", method="m")
    assert f["value"] == 5
    assert f["state"] == "measured"
    assert f["reason"] is None


def test_fact_absent_carries_a_true_zero_with_a_reason():
    f = fact(0, "absent", source="s", method="m", reason="checked, found none")
    assert f["value"] == 0
    assert f["state"] == "absent"


def test_fact_partial_requires_a_bound():
    with pytest.raises(FactError):
        fact(4, "partial", source="s", method="m", reason="crawl stopped early")


def test_fact_partial_with_bound_and_reason_succeeds():
    f = fact(4, "partial", source="s", method="m", reason="crawl stopped early", bound=">= 4")
    assert f["state"] == "partial"
    assert f["bound"] == ">= 4"


def test_fact_unavailable_must_not_carry_a_value():
    with pytest.raises(FactError):
        fact(0, "unavailable", source="s", method="m", reason="not run")


def test_fact_not_requested_must_not_carry_a_value():
    with pytest.raises(FactError):
        fact("anything", "not_requested", source="s", method="m", reason="never asked")


def test_fact_non_measured_without_reason_raises():
    with pytest.raises(FactError):
        fact(None, "unavailable", source="s", method="m")


def test_fact_unknown_state_raises():
    with pytest.raises(FactError):
        fact(1, "bogus", source="s", method="m", reason="x")


# ── document-wide invariant: every leaf fact in a real export obeys the ──
# ── same rules the constructor enforces, with a fully-measured negative ──
# ── control so this cannot pass vacuously.                               ──


def _iter_leaf_facts(document):
    for site in document["sites"]:
        yield from site["facts"].values()


def test_every_leaf_fact_in_an_export_obeys_the_state_invariants():
    document = build_facts_export(
        [
            {"label": "a-example.com", "crawl_audit": SF_DOCUMENT_A, "site_audit": SITE_DOCUMENT_A},
            {"label": "b-example.com", "site_audit": SITE_DOCUMENT_B},
            {"label": "c-example.com", "crawl_audit": SF_DOCUMENT_PARTIAL},
        ]
    )
    leaves = list(_iter_leaf_facts(document))
    assert leaves, "fixture produced no facts -- invariant below would be vacuous"

    saw_measured = saw_absent = saw_partial = saw_unavailable = saw_not_requested = False
    for leaf in leaves:
        state = leaf["state"]
        assert state in facts.FACT_STATES
        if state in ("unavailable", "not_requested"):
            assert leaf["value"] is None
        if state != "measured":
            assert leaf["reason"], f"non-measured fact with no reason: {leaf}"
        if state == "partial":
            assert leaf["bound"], f"partial fact with no bound: {leaf}"
        saw_measured |= state == "measured"
        saw_absent |= state == "absent"
        saw_partial |= state == "partial"
        saw_unavailable |= state == "unavailable"
        saw_not_requested |= state == "not_requested"

    # Positive control: this fixture actually exercises all five states.
    assert saw_measured and saw_absent and saw_partial and saw_unavailable and saw_not_requested

    # Negative control: a-example.com's crawl_pages_crawled is a real,
    # non-degenerate measurement (crawl_partial is False) -- it must read as
    # plain "measured", not get swept into absent/unavailable by a bug that
    # would make the invariant above pass on broken data.
    site_a = next(s for s in document["sites"] if s["label"] == "a-example.com")
    assert site_a["facts"]["crawl_pages_crawled"] == {
        "value": 10,
        "state": "measured",
        "source": "sf_audit_run",
        "method": "summary.totals.urls_crawled",
        "reason": None,
        "bound": None,
        "paid": False,
        "note": None,
        "is_not": "not the number of pages the site actually has",
    }


def test_export_is_json_serializable():
    document = build_facts_export(
        [{"label": "a-example.com", "crawl_audit": SF_DOCUMENT_A, "site_audit": SITE_DOCUMENT_A}]
    )
    json.dumps(document)  # must not raise


# ── partial state: a genuinely bounded crawl ─────────────────────────────


def test_crawl_partial_flag_produces_a_partial_pages_crawled_fact():
    document = build_facts_export([{"label": "c-example.com", "crawl_audit": SF_DOCUMENT_PARTIAL}])
    leaf = document["sites"][0]["facts"]["crawl_pages_crawled"]
    assert leaf["state"] == "partial"
    assert leaf["value"] == 4
    assert "hit max_urls" in leaf["reason"]
    assert leaf["bound"]


# ── absent state: a true zero is not the same as "we didn't look" ───────


def test_zero_issues_reads_as_absent_not_unavailable():
    document = build_facts_export([{"label": "c-example.com", "crawl_audit": SF_DOCUMENT_PARTIAL}])
    leaf = document["sites"][0]["facts"]["crawl_issues_total"]
    assert leaf["state"] == "absent"
    assert leaf["value"] == 0


# ── index counts: permanently not_requested, never a paid guess ─────────


def test_index_counts_are_permanently_not_requested():
    document = build_facts_export([{"label": "a-example.com"}])
    facts_row = document["sites"][0]["facts"]
    for key in ("index_count_google", "index_count_yandex"):
        leaf = facts_row[key]
        assert leaf["state"] == "not_requested"
        assert leaf["value"] is None
        assert leaf["paid"] is False
        assert "no configured provider returns a result count" in leaf["reason"]


# ── zero readable documents: named, full unavailable row, not dropped ───


def test_site_with_no_documents_gets_a_full_unavailable_row_and_is_named():
    document = build_facts_export(
        [
            {"label": "a-example.com", "crawl_audit": SF_DOCUMENT_A},
            {"label": "empty-example.com"},
        ]
    )
    labels = [site["label"] for site in document["sites"]]
    assert "empty-example.com" in labels  # never dropped silently

    empty_row = next(s for s in document["sites"] if s["label"] == "empty-example.com")
    assert "no readable documents" in empty_row["note"]
    non_index_facts = {
        k: v for k, v in empty_row["facts"].items() if not k.startswith("index_count_")
    }
    assert non_index_facts, "fixture produced no document-backed facts to check"
    for leaf in non_index_facts.values():
        assert leaf["state"] == "unavailable"
        assert leaf["value"] is None


# ── site-identity binding: the two failures the design review found ─────


def test_mismatched_domain_is_refused_naming_file_label_and_actual_domain():
    mismatched = {**SF_DOCUMENT_A, "run": {**SF_DOCUMENT_A["run"], "source": "https://other.com/"}}
    with pytest.raises(ValueError) as excinfo:
        build_facts_export([{"label": "a-example.com", "crawl_audit": mismatched}])
    message = str(excinfo.value)
    assert "a-example.com" in message  # the label
    assert "other.com" in message  # the document's own domain


def test_matching_domain_is_accepted():
    document = build_facts_export(
        [{"label": "a-example.com", "crawl_audit": SF_DOCUMENT_A, "site_audit": SITE_DOCUMENT_A}]
    )
    assert document["sites"][0]["domain"] == "a-example.com"


def test_site_audit_domain_mismatch_is_also_refused():
    mismatched = {**SITE_DOCUMENT_A, "domain": "other.com"}
    with pytest.raises(ValueError) as excinfo:
        build_facts_export([{"label": "a-example.com", "site_audit": mismatched}])
    assert "other.com" in str(excinfo.value)


# ── duplicate registrable domain: refused by name before any work ───────


def test_same_exact_domain_under_two_labels_is_refused_before_loading_anything():
    # A crawl_audit that itself would raise on load proves the duplicate check
    # runs first: "before doing any work" means before this document is ever
    # opened.
    poison_document = object()  # would blow up _load() if ever touched
    with pytest.raises(ValueError) as excinfo:
        build_facts_export(
            [
                {"label": "a-example.com"},
                {"label": "a-example.com", "crawl_audit": poison_document},
            ]
        )
    message = str(excinfo.value)
    assert "a-example.com" in message


def test_two_subdomains_of_one_registrable_domain_are_allowed_and_noted():
    document = build_facts_export(
        [{"label": "blog.a-example.com"}, {"label": "shop.a-example.com"}]
    )
    labels = {s["label"] for s in document["sites"]}
    assert labels == {"blog.a-example.com", "shop.a-example.com"}
    for site in document["sites"]:
        assert "a-example.com" in site["note"]
        assert "shares registrable domain" in site["note"]


# ── no derived ratio: only operands appear, never a quotient ────────────


def test_export_contains_no_percentage_or_ratio_keys():
    document = build_facts_export(
        [{"label": "a-example.com", "crawl_audit": SF_DOCUMENT_A, "site_audit": SITE_DOCUMENT_A}]
    )
    for key in document["sites"][0]["facts"]:
        lowered = key.lower()
        assert "ratio" not in lowered
        assert "percent" not in lowered
        assert "score" not in lowered
        assert "rank" not in lowered


# ── handler surface ───────────────────────────────────────────────────


def test_handler_wraps_a_value_error_as_ok_false():
    from seohead.servers.handlers import facts_export

    result = facts_export(sites=[{"label": "a-example.com"}, {"label": "a-example.com"}])
    assert result["ok"] is False
    assert "a-example.com" in result["error"]


def test_handler_requires_sites():
    from seohead.servers.handlers import facts_export

    with pytest.raises(ValueError):
        facts_export(sites=None)


def test_handler_returns_ok_true_document():
    from seohead.servers.handlers import facts_export

    result = facts_export(sites=[{"label": "a-example.com", "site_audit": SITE_DOCUMENT_A}])
    assert result["ok"] is True
    assert result["schema_version"] == "facts.v1"
    assert result["sites"][0]["label"] == "a-example.com"
