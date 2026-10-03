"""Scoped audit applicability: agreed populations, honest denominators, reviewed exclusions."""

import json

import pytest

from seohead.projects.coverage import (
    coverage_status,
    initialize_coverage,
    record_execution,
    update_item,
)
from seohead.projects.workspace import create_project
from tests.test_scan_reanalysis_integration import _source


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    create_project(root, "https://example.test/")
    return root


def plan(tasks=None, **population):
    return {
        "reviewer": "Lead auditor",
        "population": {
            "size": None,
            "urls": [],
            "name": None,
            "source": "Agreed audit scope memo 2026-10-01",
            "reason": None,
            "templates": None,
            **population,
        },
        **({"tasks": tasks} if tasks is not None else {}),
    }


def edit(root, **item):
    return update_item(root, item, coverage_status(root)["revision"])


def record(root, item_id, **entry):
    return record_execution(root, item_id, entry, coverage_status(root)["revision"])


def row(status, item_id):
    return next(item for item in status["items"] if item["id"] == item_id)


def init_scoped(project, tasks=None, **population):
    return initialize_coverage(project, plan=plan(tasks, **population))


def scoped_check(project, urls=None, template=None, item_id="custom:scoped-check"):
    edit(
        project,
        id=item_id,
        execution_kind="automatic",
        operation="check:BROKEN_PAGE_4XX",
        scope={
            "site": "https://example.test/",
            "template": template,
            "urls": urls or [],
        },
    )


def test_complete_set_enumeration_justifies_the_url_denominator(project):
    status = init_scoped(
        project,
        kind="complete_set",
        urls=[
            "https://example.test/",
            "https://example.test/a",
            "https://example.test/b",
        ],
    )
    axis = status["coverage"]["url_population"]
    assert axis["denominator"] == 3 and axis["numerator"] == 0
    assert axis["population_kind"] == "complete_set" and axis["state"] == "measured"
    assert status["plan"]["site"] == "https://example.test/"
    assert status["plan"]["revision"] == status["revision"]
    _source(project / "scans/source.sqlite")
    scoped_check(project, urls=["https://example.test/"])
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan covers the agreed home URL",
        artifact="scans/source.sqlite",
    )
    axis = status["coverage"]["url_population"]
    assert axis["numerator"] == 1 and axis["denominator"] == 3
    assert axis["measured_urls"] == 1 and axis["state"] == "measured"
    assert status["coverage"]["checks"]["measured_urls"] == 1


def test_enumerated_sample_reports_its_named_basis_not_site_coverage(project):
    status = init_scoped(
        project,
        kind="sample",
        name="pilot section sample",
        urls=["https://example.test/"],
    )
    _source(project / "scans/source.sqlite")
    scoped_check(project, urls=["https://example.test/"])
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan covers the named sample",
        artifact="scans/source.sqlite",
    )
    axis = status["coverage"]["url_population"]
    assert axis["numerator"] == 1 and axis["denominator"] == 1
    assert axis["population_kind"] == "sample" and axis["population_name"] == "pilot section sample"
    assert "named sample" in axis["reason"]
    assert status["coverage"]["checks"]["numerator"] == 1


def test_sized_but_unenumerated_population_stays_unverifiable(project):
    init_scoped(project, kind="complete_set", size=50)
    _source(project / "scans/source.sqlite")
    scoped_check(project, urls=["https://example.test/"])
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan; eligibility follows the agreed population",
        artifact="scans/source.sqlite",
    )
    axis = status["coverage"]["url_population"]
    assert axis["denominator"] == 50 and axis["numerator"] == 0
    assert axis["state"] == "partial" and axis["unverified_measurements"] == 1
    assert axis["measured_urls"] == 1
    init_scoped(project, kind="sample", name="unlisted sample", size=50)
    status = coverage_status(project)
    axis = status["coverage"]["url_population"]
    assert axis["denominator"] == 50 and axis["numerator"] == 0
    assert axis["state"] == "measured" and axis["unverified_measurements"] == 0
    assert row(status, "custom:scoped-check")["stale"]


def _mark_partial(path):
    """Flag the retained fixture scan as a partial crawl, keeping audit and header aligned."""
    import hashlib
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("UPDATE scan SET crawl_partial=1 WHERE singleton=1")
    audit = json.loads(
        con.execute("SELECT document_json FROM audit WHERE singleton=1").fetchone()[0]
    )
    audit["run"]["crawl_partial"] = True
    raw = json.dumps(audit)
    con.execute(
        "UPDATE audit SET document_json=?, sha256=? WHERE singleton=1",
        (raw, hashlib.sha256(raw.encode()).hexdigest()),
    )
    con.commit()
    con.close()


def test_limited_measurement_does_not_complete_a_site_scoped_task(project):
    init_scoped(project, kind="complete_set", urls=["https://example.test/"])
    _source(project / "scans/source.sqlite")
    _mark_partial(project / "scans/source.sqlite")
    scoped_check(project)
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan stopped before the crawl finished",
        artifact="scans/source.sqlite",
    )
    measured = row(status, "custom:scoped-check")
    assert measured["state"] == "run" and not measured["complete"]
    assert measured["measurement"]["state"] == "limited"
    assert "custom:scoped-check" in status["views"]["remaining"]
    assert status["coverage"]["audit_tasks"]["unfinished"] >= 1
    assert status["complete"] is False


def test_bounded_scope_may_complete_even_from_a_partial_source(project):
    init_scoped(project, kind="sample", name="one page", urls=["https://example.test/"])
    _source(project / "scans/source.sqlite")
    _mark_partial(project / "scans/source.sqlite")
    scoped_check(project, urls=["https://example.test/"])
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan covered the whole declared sample scope",
        artifact="scans/source.sqlite",
    )
    measured = row(status, "custom:scoped-check")
    assert measured["complete"] and measured["measurement"]["state"] == "limited"


def test_unknown_population_returns_no_denominator_with_a_reason(project):
    status = init_scoped(
        project,
        kind="unknown",
        reason="Site population was never agreed; a crawl census is pending",
    )
    axis = status["coverage"]["url_population"]
    assert axis["numerator"] is None and axis["denominator"] is None
    assert axis["state"] == "unknown"
    assert "never agreed" in axis["reason"]
    tasks = status["coverage"]["audit_tasks"]
    assert tasks["denominator"] > 0 and tasks["state"] == "measured"
    assert status["complete"] is False


def test_missing_plan_returns_unknown_url_coverage(project):
    status = initialize_coverage(project)
    axis = status["coverage"]["url_population"]
    assert axis["numerator"] is None and axis["denominator"] is None
    assert axis["state"] == "unknown" and axis["population_kind"] is None
    assert "no agreed URL population" in axis["reason"]


def test_empty_applicable_set_is_undefined_not_complete(project):
    status = initialize_coverage(project)
    deliverables = status["coverage"]["deliverable_review"]
    assert deliverables["numerator"] == 0 and deliverables["denominator"] == 0
    assert deliverables["state"] == "unknown" and "not 100%" in deliverables["reason"]
    assert status["complete"] is False


def test_distinct_task_url_and_check_denominators_do_not_mix(project):
    status = init_scoped(project, kind="complete_set", size=200)
    coverage = status["coverage"]
    assert coverage["audit_tasks"]["denominator"] == status["counts"]["total"]
    assert coverage["url_population"]["denominator"] == 200
    check_rows = [item for item in status["items"] if item["execution_kind"] == "automatic"]
    assert coverage["checks"]["denominator"] == len(check_rows)
    manual = coverage["manual_review"]
    assert manual["denominator"] < coverage["audit_tasks"]["denominator"]


def test_unavailable_attempt_remains_unfinished(project):
    initialize_coverage(project)
    scoped_check(project)
    status = record(
        project,
        "custom:scoped-check",
        status="unavailable",
        reason="Scan provider export is missing",
    )
    assert not row(status, "custom:scoped-check")["complete"]
    assert status["coverage"]["checks"]["unfinished"] >= 1
    assert "custom:scoped-check" in status["views"]["blocked"]
    assert status["complete"] is False


def test_exclusion_requires_reason_reviewer_and_evidence_basis(project):
    initialize_coverage(project)
    edit(project, id="custom:out-of-scope")
    for entry, match in (
        (
            {"status": "not_applicable", "reason": "Not in scope"},
            "reviewer",
        ),
        (
            {
                "status": "not_applicable",
                "reason": "Not in scope",
                "reviewer": "Lead auditor",
            },
            "evidence basis",
        ),
    ):
        with pytest.raises(ValueError, match=match):
            record(project, "custom:out-of-scope", **entry)
    status = coverage_status(project)
    assert row(status, "custom:out-of-scope")["applicability"] == "applicable"
    assert status["counts"]["excluded"] == 0


def test_reviewed_exclusion_leaves_the_denominator_and_stays_visible(project):
    status = initialize_coverage(project)
    total = status["counts"]["total"]
    edit(project, id="custom:excluded")
    status = record(
        project,
        "custom:excluded",
        status="not_applicable",
        reason="Client contract excludes the blog section",
        reviewer="Lead auditor",
        evidence="Signed scope amendment 2026-10-02",
    )
    assert status["counts"]["total"] == total
    assert status["counts"]["excluded"] == 1
    excluded = row(status, "custom:excluded")
    assert excluded["applicability"] == "excluded" and excluded["state"] == "not_applicable"
    assert not excluded["complete"]
    assert "custom:excluded" not in status["views"]["remaining"]
    entry = next(item for item in status["exclusions"] if item["id"] == "custom:excluded")
    assert entry["reason"] == "Client contract excludes the blog section"
    assert entry["reviewer"] == "Lead auditor"
    assert entry["basis"] == {"evidence": "Signed scope amendment 2026-10-02"}
    assert entry["revision"] == status["revision"]
    tasks = status["coverage"]["audit_tasks"]
    assert tasks["excluded"] == 1 and tasks["denominator"] == total


def test_exclusion_artifact_is_digested_and_stale_returns_to_pending(project):
    initialize_coverage(project)
    edit(project, id="custom:excluded")
    memo = project / "reports/scope-memo.md"
    memo.write_text("Agreed scope amendment; synthetic fixture", encoding="utf-8")
    status = record(
        project,
        "custom:excluded",
        status="not_applicable",
        reason="Out of the amended scope",
        reviewer="Lead auditor",
        artifact="reports/scope-memo.md",
    )
    basis = row(status, "custom:excluded")["exclusion"]["basis"]
    assert basis["artifact"] == "reports/scope-memo.md" and len(basis["sha256"]) == 64
    memo.write_text("Changed after the decision", encoding="utf-8")
    status = coverage_status(project)
    excluded = row(status, "custom:excluded")
    assert excluded["applicability"] == "pending_exclusion"
    assert "custom:excluded" in status["views"]["pending_exclusion"]
    assert "custom:excluded" in status["views"]["remaining"]
    assert status["counts"]["excluded"] == 0


def test_legacy_unverifiable_exclusion_stays_pending_without_data_loss(project):
    initialize_coverage(project)
    edit(project, id="custom:legacy")
    status = record(
        project,
        "custom:legacy",
        status="not_applicable",
        reason="Out of scope",
        reviewer="Lead auditor",
        evidence="Signed scope amendment 2026-10-02",
    )
    path = project / "coverage.json"
    document = json.loads(path.read_text())
    del document["items"]["custom:legacy"]["records"][-1]["evidence"]
    path.write_text(json.dumps(document))
    status = coverage_status(project)
    legacy = row(status, "custom:legacy")
    assert legacy["applicability"] == "pending_exclusion"
    assert "verifiable evidence basis" in legacy["applicability_reason"]
    assert legacy["attempts"] == 1
    assert "custom:legacy" in status["views"]["remaining"]


def test_disabled_item_cannot_silently_leave_the_denominator(project):
    initialize_coverage(project)
    edit(project, id="custom:dropped")
    total = coverage_status(project)["counts"]["total"]
    status = edit(project, id="custom:dropped", enabled=False)
    assert status["counts"]["total"] == total
    assert status["counts"]["disabled"] == 1
    dropped = row(status, "custom:dropped")
    assert dropped["applicability"] == "pending_exclusion"
    assert "inside the agreed denominator" in dropped["applicability_reason"]
    assert "custom:dropped" in status["views"]["remaining"]
    status = record(
        project,
        "custom:dropped",
        status="not_applicable",
        reason="Scope confirmed out by the client",
        reviewer="Lead auditor",
        evidence="Signed scope amendment 2026-10-02",
    )
    assert row(status, "custom:dropped")["applicability"] == "excluded"
    assert status["counts"]["excluded"] == 1


def test_removed_catalogue_item_keeps_its_denominator_membership(project, monkeypatch):
    from seohead.projects import coverage

    initialize_coverage(project)
    item_id = "skill:workflow/control"
    total = coverage_status(project)["counts"]["total"]
    catalogue = coverage.load_catalogue()
    del catalogue[item_id]
    monkeypatch.setattr(coverage, "load_catalogue", lambda: catalogue)
    status = coverage_status(project)
    assert row(status, item_id)["stale"]
    assert status["counts"]["total"] == total
    status = record(
        project,
        item_id,
        status="not_applicable",
        reason="Check removed from the agreed audit scope",
        reviewer="Lead auditor",
        evidence="Retired from the packaged catalogue",
    )
    removed = row(status, item_id)
    assert removed["applicability"] == "excluded"
    assert removed["exclusion"]["basis"] == {"evidence": "Retired from the packaged catalogue"}
    assert status["counts"]["excluded"] == 1
    assert status["counts"]["total"] == total - 1


def test_stale_definition_returns_an_exclusion_to_pending_review(project):
    initialize_coverage(project)
    edit(project, id="custom:excluded")
    record(
        project,
        "custom:excluded",
        status="not_applicable",
        reason="Out of scope",
        reviewer="Lead auditor",
        evidence="Signed scope amendment 2026-10-02",
    )
    status = edit(project, id="custom:excluded", title="Renamed out-of-scope item")
    assert row(status, "custom:excluded")["applicability"] == "pending_exclusion"
    assert "stale" in row(status, "custom:excluded")["applicability_reason"]
    status = record(
        project,
        "custom:excluded",
        status="not_applicable",
        reason="Reconfirmed out of scope after the rename",
        reviewer="Lead auditor",
        evidence="Signed scope amendment 2026-10-02",
    )
    assert row(status, "custom:excluded")["applicability"] == "excluded"


def test_manual_and_deliverable_axes_report_approval_separately(project):
    initialize_coverage(project)
    edit(project, id="custom:review-task")
    edit(project, id="custom:report-task", execution_kind="deliverable")
    status = record(
        project,
        "custom:review-task",
        status="succeeded",
        reason="Reviewed by a specialist",
        reviewer="Specialist",
        signoff=True,
    )
    assert status["coverage"]["manual_review"]["numerator"] == 1
    assert status["coverage"]["deliverable_review"]["numerator"] == 0
    report = project / "reports/summary.md"
    report.write_text("Synthetic client deliverable", encoding="utf-8")
    status = record(
        project,
        "custom:report-task",
        status="succeeded",
        reason="Approved deliverable",
        artifact="reports/summary.md",
        reviewer="Specialist",
        review="approved",
    )
    assert status["coverage"]["deliverable_review"]["numerator"] == 1
    assert "custom:report-task" in status["views"]["deliverable_ready"]


def test_plan_recording_upgrades_to_v3_and_survives_reconcile_and_priorities(project):
    initialize_coverage(project)
    status = init_scoped(project, kind="unknown", reason="Population pending agreement")
    assert status["plan"]["reviewer"] == "Lead auditor"
    document = json.loads((project / "coverage.json").read_text())
    assert document["format"] == "seohead.coverage.v3" and document["version"] == 3
    status = initialize_coverage(project)
    assert status["plan"]["population"]["kind"] == "unknown"
    from seohead.projects.priorities import project_priorities

    result = project_priorities(str(project), apply=True, expected_revision=status["revision"])
    assert result["applied"]
    document = json.loads((project / "coverage.json").read_text())
    assert document["format"] == "seohead.coverage.v3"
    assert coverage_status(project)["plan"]["population"]["kind"] == "unknown"


def test_plan_validation_binds_site_identity_and_population_shape(project):
    initialize_coverage(project)
    with pytest.raises(ValueError, match="reviewer"):
        initialize_coverage(project, plan={"population": {}})
    with pytest.raises(ValueError, match="another site"):
        init_scoped(project, kind="complete_set", urls=["https://other.test/"])
    with pytest.raises(ValueError, match="no size"):
        init_scoped(project, kind="unknown", size=10, reason="Population pending agreement")
    with pytest.raises(ValueError, match="sample name"):
        init_scoped(project, kind="sample", urls=["https://example.test/"])
    with pytest.raises(ValueError, match="disagrees"):
        init_scoped(
            project,
            kind="complete_set",
            size=7,
            urls=["https://example.test/"],
        )
    before = (project / "coverage.json").read_bytes()
    revision = coverage_status(project)["revision"]
    with pytest.raises(ValueError, match="revision conflict"):
        initialize_coverage(
            project, expected_revision=revision + 9, plan=plan(kind="unknown", reason="x")
        )
    assert (project / "coverage.json").read_bytes() == before


def test_second_plan_replaces_the_agreement_with_a_new_revision(project):
    initialize_coverage(project)
    first = init_scoped(project, kind="unknown", reason="Population pending agreement")
    second = init_scoped(project, kind="complete_set", urls=["https://example.test/"])
    assert second["plan"]["revision"] == second["revision"] > first["plan"]["revision"]
    assert second["coverage"]["url_population"]["denominator"] == 1
    history = second["plan_history"]
    assert len(history) == 1 and history[0]["revision"] == first["plan"]["revision"]
    assert history[0]["population"]["kind"] == "unknown"


def _product_population(urls, **fields):
    return {
        "kind": "complete_set",
        "size": None,
        "urls": urls,
        "name": None,
        "source": "Agreed template export 2026-10-01",
        "reason": None,
        **fields,
    }


def test_template_population_outside_the_enumerated_site_set_is_refused(project):
    initialize_coverage(project)
    before = (project / "coverage.json").read_bytes()
    with pytest.raises(ValueError, match="outside the enumerated site population"):
        init_scoped(
            project,
            kind="complete_set",
            urls=["https://example.test/a"],
            templates={
                "product": _product_population(["https://example.test/b", "https://example.test/c"])
            },
        )
    assert (project / "coverage.json").read_bytes() == before
    status = coverage_status(project)
    assert status["plan"] is None
    axis = status["coverage"]["url_population"]
    assert axis["numerator"] is None and axis["state"] == "unknown"


def test_declared_template_membership_cannot_exceed_the_site_population(project):
    initialize_coverage(project)
    with pytest.raises(ValueError, match="larger than the agreed site population"):
        init_scoped(
            project,
            kind="complete_set",
            size=1,
            templates={
                "product": _product_population(["https://example.test/b", "https://example.test/c"])
            },
        )
    with pytest.raises(ValueError, match="larger than the agreed site population"):
        init_scoped(
            project,
            kind="complete_set",
            size=1,
            templates={"product": _product_population([], size=2)},
        )
    with pytest.raises(ValueError, match="more URLs than the agreed site population"):
        init_scoped(
            project,
            kind="complete_set",
            size=2,
            templates={
                "product": _product_population(
                    ["https://example.test/b", "https://example.test/c"]
                ),
                "category": _product_population(["https://example.test/d"]),
            },
        )


def test_stored_plan_with_an_incoherent_template_population_is_refused_on_read(project):
    init_scoped(
        project,
        kind="complete_set",
        urls=["https://example.test/a", "https://example.test/b"],
        templates={"product": _product_population(["https://example.test/b"])},
    )
    path = project / "coverage.json"
    document = json.loads(path.read_text())
    document["plans"][-1]["population"]["templates"]["product"] = _product_population(
        ["https://example.test/c"]
    )
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="outside the enumerated site population"):
        coverage_status(project)


def test_size_only_template_population_cannot_verify_membership(project):
    init_scoped(
        project,
        kind="complete_set",
        urls=["https://example.test/a", "https://example.test/b"],
        templates={"product": _product_population([], size=1)},
    )
    _source(project / "scans/b.sqlite", start_url="https://example.test/b")
    scoped_check(project, urls=["https://example.test/b"], template="product")
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan covers one declared-scope URL",
        artifact="scans/b.sqlite",
    )
    axis = status["coverage"]["url_population"]
    assert axis["denominator"] == 2 and axis["numerator"] == 0
    assert axis["measured_urls"] == 1
    assert axis["state"] == "partial" and axis["unverified_measurements"] == 1


def test_template_measurements_count_inside_the_site_denominator(project):
    initialize_coverage(project)
    items = (
        ("custom:home-check", "https://example.test/a", None),
        ("custom:product-b-check", "https://example.test/b", "product"),
        ("custom:product-c-check", "https://example.test/c", "product"),
    )
    for item_id, url, template in items:
        scoped_check(project, urls=[url], template=template, item_id=item_id)
    status = init_scoped(
        project,
        kind="complete_set",
        urls=[
            "https://example.test/a",
            "https://example.test/b",
            "https://example.test/c",
        ],
        templates={
            "product": _product_population(["https://example.test/b", "https://example.test/c"])
        },
        tasks={
            "kind": "selection",
            "ids": [item_id for item_id, _url, _template in items],
            "source": "Agreed audit scope memo 2026-10-01",
        },
    )
    assert status["coverage"]["url_population"]["denominator"] == 3
    for item_id, url, _template in items:
        artifact = f"scans/{url.rsplit('/', 1)[-1]}.sqlite"
        _source(project / artifact, start_url=url)
        status = record(
            project,
            item_id,
            status="succeeded",
            reason="Saved fixture scan covers the agreed URL",
            artifact=artifact,
        )
    axis = status["coverage"]["url_population"]
    assert axis["numerator"] == 3 and axis["denominator"] == 3
    assert axis["state"] == "measured" and axis["unverified_measurements"] == 0
    assert status["complete"] is True


def test_identical_plan_is_an_idempotent_reconcile_not_a_new_agreement(project):
    agreed = init_scoped(project, kind="complete_set", urls=["https://example.test/"])
    _source(project / "scans/source.sqlite")
    scoped_check(project, urls=["https://example.test/"])
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan under the standing agreement",
        artifact="scans/source.sqlite",
    )
    assert row(status, "custom:scoped-check")["complete"]
    revision = status["revision"]
    status = init_scoped(project, kind="complete_set", urls=["https://example.test/"])
    assert status["revision"] == revision
    assert status["plan_history"] == []
    assert status["plan"]["revision"] == agreed["plan"]["revision"]
    measured = row(status, "custom:scoped-check")
    assert measured["complete"] and not measured["stale"]


def test_newer_plan_invalidates_earlier_evidence_without_deleting_it(project):
    init_scoped(project, kind="unknown", reason="Population pending agreement")
    _source(project / "scans/source.sqlite")
    scoped_check(project, urls=["https://example.test/"])
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan under the first agreement",
        artifact="scans/source.sqlite",
    )
    assert row(status, "custom:scoped-check")["complete"]
    status = init_scoped(project, kind="complete_set", urls=["https://example.test/"])
    measured = row(status, "custom:scoped-check")
    assert measured["stale"] and not measured["complete"]
    assert "predates the current agreed audit scope" in measured["reason"]
    assert measured["attempts"] == 1 and measured["state"] == "not_run"
    assert status["coverage"]["url_population"]["numerator"] == 0
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan re-verified under the new agreement",
        artifact="scans/source.sqlite",
    )
    measured = row(status, "custom:scoped-check")
    assert measured["complete"] and not measured["stale"]
    assert measured["attempts"] == 2


def test_selection_task_agreement_scopes_the_applicable_denominator(project):
    initialize_coverage(project)
    scoped_check(project, urls=["https://example.test/"])
    catalogue_rows = len(coverage_status(project)["items"])
    status = init_scoped(
        project,
        kind="complete_set",
        urls=["https://example.test/"],
        tasks={
            "kind": "selection",
            "ids": ["custom:scoped-check"],
            "source": "Agreed audit scope memo 2026-10-01",
        },
    )
    assert status["plan"]["tasks"]["kind"] == "selection"
    assert status["counts"]["total"] == 1
    assert status["counts"]["not_agreed"] == catalogue_rows - 1
    tasks = status["coverage"]["audit_tasks"]
    assert tasks["denominator"] == 1 and tasks["not_agreed"] == catalogue_rows - 1
    assert "check:BROKEN_PAGE_4XX" in status["views"]["not_agreed"]
    _source(project / "scans/source.sqlite")
    status = record(
        project,
        "custom:scoped-check",
        status="succeeded",
        reason="Saved fixture scan covers the agreed URL",
        artifact="scans/source.sqlite",
    )
    assert status["coverage"]["audit_tasks"]["numerator"] == 1
    assert status["coverage"]["url_population"]["numerator"] == 1
    assert status["complete"] is True


def test_selection_requires_existing_items_and_a_source(project):
    initialize_coverage(project)
    with pytest.raises(ValueError, match="must exist"):
        init_scoped(
            project,
            kind="complete_set",
            urls=["https://example.test/"],
            tasks={
                "kind": "selection",
                "ids": ["custom:not-added-yet"],
                "source": "Agreed audit scope memo 2026-10-01",
            },
        )
    scoped_check(project)
    for tasks, match in (
        (
            {
                "kind": "selection",
                "ids": [],
                "source": "Agreed audit scope memo 2026-10-01",
            },
            "nonempty",
        ),
        ({"kind": "selection", "ids": ["custom:scoped-check"]}, "sourced selection"),
        (
            {
                "kind": "selection",
                "ids": ["custom:scoped-check", "custom:scoped-check"],
                "source": "Agreed audit scope memo 2026-10-01",
            },
            "duplicate",
        ),
        ({"kind": "unknown"}, "task agreement"),
    ):
        with pytest.raises(ValueError, match=match):
            init_scoped(
                project,
                kind="complete_set",
                urls=["https://example.test/"],
                tasks=tasks,
            )


def test_all_tasks_done_with_unknown_population_is_not_a_complete_audit(project):
    initialize_coverage(project)
    edit(project, id="custom:review-task")
    status = init_scoped(
        project,
        kind="unknown",
        reason="Population pending agreement",
        tasks={
            "kind": "selection",
            "ids": ["custom:review-task"],
            "source": "Agreed audit scope memo 2026-10-01",
        },
    )
    status = record(
        project,
        "custom:review-task",
        status="succeeded",
        reason="Reviewed by a specialist",
        reviewer="Specialist",
        signoff=True,
    )
    assert status["coverage"]["audit_tasks"]["numerator"] == 1
    assert status["coverage"]["url_population"]["state"] == "unknown"
    assert status["complete"] is False


def test_legacy_single_plan_document_still_loads(project):
    initialize_coverage(project)
    init_scoped(project, kind="unknown", reason="Population pending agreement")
    path = project / "coverage.json"
    document = json.loads(path.read_text())
    document["plan"] = document.pop("plans")[0]
    path.write_text(json.dumps(document))
    status = coverage_status(project)
    assert status["plan"]["population"]["kind"] == "unknown"
    assert status["plan"]["tasks"] == {"kind": "all_agreed"}
    assert status["plan_history"] == []
    status = init_scoped(project, kind="complete_set", urls=["https://example.test/"])
    assert status["plan"]["population"]["kind"] == "complete_set"
    assert len(status["plan_history"]) == 1


def test_aggregate_merges_axes_without_fabricating_denominators(tmp_path):
    import json as jsonlib

    from seohead.projects.runtime import PREPARATION_FORMAT, aggregate_coverage

    root = tmp_path / "project"
    primary = create_project(root, "https://example.test/")["project"]
    init_scoped(root, kind="complete_set", urls=["https://example.test/"])
    child = root / "competitors" / "candidate"
    child.parent.mkdir()
    competitor = create_project(child, "https://competitor.test/")["project"]
    initialize_coverage(child)
    (root / "preparation.json").write_text(
        jsonlib.dumps(
            {
                "format": PREPARATION_FORMAT,
                "project_uuid": primary["project_uuid"],
                "revision": 1,
                "state": "partial",
                "steps": {},
                "competitors": [
                    {
                        "url": competitor["site"]["target"],
                        "directory": "competitors/candidate",
                        "project_uuid": competitor["project_uuid"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = aggregate_coverage(str(root), coverage_status(root))
    axis = result["coverage"]["url_population"]
    assert axis["denominator"] is None and axis["state"] == "unknown"
    tasks = result["coverage"]["audit_tasks"]
    assert tasks["denominator"] > 0 and tasks["state"] == "measured"
    assert axis["population_kind"] == "mixed"
