"""Checklist acceptance: evidence identity, editable history and concurrent writers."""

import json
import socket
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from seohead.projects.coverage import (
    coverage_status,
    initialize_coverage,
    record_execution,
    update_item,
)
from seohead.projects.workspace import create_project, project_status
from tests.test_scan_reanalysis_integration import _source


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    create_project(root, "https://example.test/")
    initialize_coverage(root)
    return root


def edit(root, **item):
    return update_item(root, item, coverage_status(root)["revision"])


def record(root, item_id, **entry):
    return record_execution(root, item_id, entry, coverage_status(root)["revision"])


def row(status, item_id):
    return next(item for item in status["items"] if item["id"] == item_id)


def test_ecommerce_history_and_completion_axes(project, monkeypatch):
    scope = {
        "site": "https://example.test/",
        "template": "product",
        "urls": ["https://example.test/"],
    }
    initial = coverage_status(project)["counts"]["remaining"]
    template = {
        "format": "seohead.checklist-template.v1",
        "items": [
            {
                "id": "custom:product-auto",
                "title": "Product response check",
                "execution_kind": "automatic",
                "operation": "check:BROKEN_PAGE_4XX",
                "scope": scope,
            },
            {
                "id": "custom:product-review",
                "title": "Review product purchase flow",
                "dependencies": ["custom:product-auto"],
                "scope": scope,
            },
        ],
    }
    initialize_coverage(project, template)
    assert coverage_status(project)["counts"]["remaining"] == initial + 2
    with pytest.raises(ValueError, match="dependencies"):
        record(
            project,
            "custom:product-review",
            status="succeeded",
            reason="Reviewed purchase flow",
            reviewer="Specialist",
            signoff=True,
        )
    _source(project / "scans/source.sqlite")
    original = (project / "scans/source.sqlite").read_bytes()
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network"))
    status = record(
        project,
        "custom:product-auto",
        status="succeeded",
        reason="Saved product sample checked",
        artifact="scans/source.sqlite",
    )
    measured = row(status, "custom:product-auto")
    assert measured["complete"] and measured["measurement"]["state"] == "limited"
    assert (project / "scans/source.sqlite").read_bytes() == original
    status = record(
        project,
        "custom:product-review",
        status="succeeded",
        reason="Reviewed purchase flow",
        reviewer="Specialist",
        signoff=True,
    )
    assert status["counts"]["remaining"] == initial
    status = edit(project, id="custom:product-review", title="Review updated purchase flow")
    assert row(status, "custom:product-review")["stale"]
    assert row(status, "custom:product-review")["definition_versions"] == 2
    assert row(status, "custom:product-review")["attempts"] == 1
    assert not row(status, "custom:product-auto")["stale"]
    record(
        project,
        "custom:product-review",
        status="not_applicable",
        reason="Purchase flow is out of the agreed scope",
        reviewer="Specialist",
    )
    assert coverage_status(project)["counts"]["remaining"] == initial
    assert project_status(project)["checklist"] == coverage_status(project)


def test_competitor_missing_failed_and_wrong_site_stay_pending(project):
    edit(
        project,
        id="custom:competitor",
        execution_kind="automatic",
        operation="check:BROKEN_PAGE_4XX",
        scope={"site": "https://competitor.test/", "template": None, "urls": []},
    )
    for status in ("failed", "unavailable", "running"):
        result = record(
            project, "custom:competitor", status=status, reason="Competitor source pending"
        )
        assert not row(result, "custom:competitor")["complete"]
    with pytest.raises(ValueError, match="missing"):
        record(
            project,
            "custom:competitor",
            status="succeeded",
            reason="Attempt",
            artifact="scans/missing.sqlite",
        )
    _source(project / "scans/primary.sqlite")
    with pytest.raises(ValueError, match="site"):
        record(
            project,
            "custom:competitor",
            status="succeeded",
            reason="Attempt",
            artifact="scans/primary.sqlite",
        )
    assert not row(coverage_status(project), "custom:competitor")["complete"]


def test_manual_and_deliverable_review_are_distinct(project):
    edit(project, id="custom:manual")
    edit(project, id="custom:delivery", execution_kind="deliverable")
    output = project / "reports/client.md"
    output.write_text("Reviewed synthetic client deliverable")
    with pytest.raises(ValueError, match="approved"):
        record(
            project,
            "custom:delivery",
            status="succeeded",
            reason="Created file",
            artifact="reports/client.md",
            reviewer="Specialist",
        )
    with pytest.raises(ValueError, match="approved"):
        record(
            project,
            "custom:delivery",
            status="succeeded",
            reason="Signed",
            reviewer="Specialist",
            signoff=True,
        )
    result = record(
        project,
        "custom:delivery",
        status="succeeded",
        reason="Reviewed package",
        artifact="reports/client.md",
        reviewer="Specialist",
        review="approved",
    )
    assert "custom:delivery" in result["views"]["deliverable_ready"]
    record(
        project,
        "custom:manual",
        status="succeeded",
        reason="Reviewed flow",
        reviewer="Specialist",
        signoff=True,
    )
    output.write_text("Changed after review")
    status = coverage_status(project)
    assert row(status, "custom:delivery")["stale"]
    assert row(status, "custom:manual")["complete"]


def test_concurrent_conflicts_preserve_successful_write(project):
    revision = coverage_status(project)["revision"]
    barrier = Barrier(2)

    def write(n):
        barrier.wait()
        try:
            update_item(project, {"id": f"custom:writer{n}"}, revision)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, range(2)))
    assert sum(results) == 1
    status = coverage_status(project)
    assert status["revision"] == revision + 1
    assert sum(item["id"].startswith("custom:writer") for item in status["items"]) == 1


def test_interrupted_publication_keeps_previous_bytes(project, monkeypatch):
    before = (project / "coverage.json").read_bytes()

    def interrupted(*a):
        raise OSError("simulated interrupted publication")

    monkeypatch.setattr("seohead.projects.coverage.os.replace", interrupted)
    with pytest.raises(OSError):
        edit(project, id="custom:interrupted")
    assert (project / "coverage.json").read_bytes() == before
    assert not list(project.glob(".coverage-*"))
    assert not (project / ".coverage.lock").exists()


@pytest.mark.parametrize(
    "item",
    [
        {"id": "custom:../bad"},
        {"id": "custom:x", "execution_kind": "shell"},
        {"id": "custom:x", "operation": "rm -rf"},
        {"id": "custom:x", "priority": "P3"},
        {"id": "custom:x", "enabled": 1},
        {"id": "custom:x", "dependencies": ["custom:missing"]},
        {
            "id": "custom:x",
            "scope": {
                "site": "https://example.test/",
                "template": None,
                "urls": ["https://competitor.test/"],
            },
        },
    ],
)
def test_invalid_definitions_do_not_change_file(project, item):
    before = (project / "coverage.json").read_bytes()
    with pytest.raises(ValueError):
        update_item(project, item, coverage_status(project)["revision"])
    assert (project / "coverage.json").read_bytes() == before


def test_schema_identity_and_path_refusals(project):
    path = project / "coverage.json"
    original = json.loads(path.read_text())
    for key, value in [("version", 99), ("project_uuid", "other")]:
        path.write_text(json.dumps({**original, key: value}))
        with pytest.raises(ValueError):
            coverage_status(project)
    path.write_text(json.dumps(original))
    edit(project, id="custom:manual")
    for artifact in ("../outside", "/tmp/outside", "project.json", "reports/missing"):
        with pytest.raises(ValueError):
            record(
                project,
                "custom:manual",
                status="succeeded",
                reason="Review",
                artifact=artifact,
                reviewer="Specialist",
                review="approved",
            )


def test_definition_drift_and_new_entries_are_pending(project, monkeypatch):
    from seohead.projects import coverage

    record(
        project,
        "skill:workflow/control",
        status="succeeded",
        reason="Reviewed scope",
        reviewer="Specialist",
        signoff=True,
    )
    catalogue = coverage.load_catalogue()
    catalogue["skill:workflow/control"]["definition_hash"] = "b" * 64
    monkeypatch.setattr(coverage, "load_catalogue", lambda: catalogue)
    assert row(coverage_status(project), "skill:workflow/control")["stale"]
    result = initialize_coverage(project)
    assert row(result, "skill:workflow/control")["stale"]
    assert row(result, "skill:workflow/control")["attempts"] == 1


def test_disable_and_order_do_not_erase_history(project):
    edit(project, id="custom:x")
    record(
        project,
        "custom:x",
        status="succeeded",
        reason="Reviewed",
        reviewer="Specialist",
        signoff=True,
    )
    result = edit(project, id="custom:x", enabled=False, order=999)
    assert result["counts"]["disabled"] == 1
    assert row(result, "custom:x")["attempts"] == 1
    assert "custom:x" not in result["views"]["remaining"]


def test_new_catalogue_entry_is_pending_before_reconcile(project, monkeypatch):
    from seohead.projects import coverage

    catalogue = coverage.load_catalogue()
    before = coverage_status(project)["counts"]["remaining"]
    original = (project / "coverage.json").read_bytes()
    catalogue["check:SYNTHETIC_NEW"] = {
        **catalogue["check:BROKEN_PAGE_4XX"],
        "id": "check:SYNTHETIC_NEW",
    }
    monkeypatch.setattr(coverage, "load_catalogue", lambda: catalogue)
    status = coverage_status(project)
    assert status["counts"]["remaining"] == before + 1
    assert row(status, "check:SYNTHETIC_NEW")["state"] == "not_run"
    assert (project / "coverage.json").read_bytes() == original


@pytest.mark.parametrize("value", [None, [], "bad", {"status": []}])
def test_malformed_execution_refuses_without_write(project, value):
    path = project / "coverage.json"
    original = path.read_bytes()
    with pytest.raises(ValueError):
        record_execution(
            project, "check:BROKEN_PAGE_4XX", value, coverage_status(project)["revision"]
        )
    assert path.read_bytes() == original


def test_malformed_history_and_definitions_refuse(project):
    edit(project, id="custom:manual")
    record(
        project,
        "custom:manual",
        status="succeeded",
        reason="Reviewed",
        reviewer="Specialist",
        signoff=True,
    )
    path = project / "coverage.json"
    original = path.read_text()
    for mutation in ("record", "definition", "approval"):
        doc = json.loads(original)
        item = doc["items"]["custom:manual"]
        if mutation == "record":
            item["records"][-1].pop("definition_hash")
        elif mutation == "definition":
            item["definition"]["execution_kind"] = []
            item["definitions"][-1]["definition"]["execution_kind"] = []
        else:
            item["records"][-1].pop("signoff")
        path.write_text(json.dumps(doc))
        with pytest.raises(ValueError):
            coverage_status(project)
    path.write_text(original)


def test_skipped_check_and_absent_template_never_complete(project):
    _source(project / "scans/source.sqlite")
    edit(
        project,
        id="custom:absent",
        execution_kind="automatic",
        operation="check:BROKEN_PAGE_4XX",
        scope={"site": "https://example.test/", "template": "product", "urls": []},
    )
    with pytest.raises(ValueError, match="sample URLs"):
        record(
            project,
            "custom:absent",
            status="succeeded",
            reason="Try source",
            artifact="scans/source.sqlite",
        )
    from seohead.storage import open_scan

    with open_scan(project / "scans/source.sqlite") as con:
        audit = json.loads(con.execute("SELECT document_json FROM audit").fetchone()[0])
    skipped = audit["run"]["checks_skipped"][0]["id"]
    with pytest.raises(ValueError, match="does not prove"):
        record(
            project,
            "check:" + skipped,
            status="succeeded",
            reason="Try source",
            artifact="scans/source.sqlite",
        )
