"""Negative contracts for project checklist writes and stale source definitions."""

from __future__ import annotations

import pytest

from seohead.projects import coverage
from seohead.projects.coverage import (
    coverage_status,
    initialize_coverage,
    record_execution,
    update_item,
)
from seohead.projects.workspace import create_project


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    create_project(root, "https://example.test/")
    initialize_coverage(root)
    return root


@pytest.mark.parametrize("item_id", [[], {"id": "custom:x"}, object()])
def test_malformed_item_identifier_refuses_before_any_checklist_write(project, item_id):
    path = project / "coverage.json"
    before = path.read_bytes()

    with pytest.raises(ValueError, match="checklist item identifier"):
        record_execution(
            project,
            item_id,
            {"status": "running", "reason": "Synthetic incomplete attempt"},
            coverage_status(project)["revision"],
        )

    assert path.read_bytes() == before


@pytest.mark.parametrize("revision", [None, True, "1", 1.0])
def test_missing_or_noninteger_revision_refuses_without_losing_history(project, revision):
    path = project / "coverage.json"
    before = path.read_bytes()

    with pytest.raises(ValueError, match="expected_revision must be an integer"):
        update_item(project, {"id": "custom:unsafe"}, revision)
    assert path.read_bytes() == before

    with pytest.raises(ValueError, match="expected_revision must be an integer"):
        record_execution(
            project,
            "skill:workflow/control",
            {"status": "running", "reason": "Synthetic incomplete attempt"},
            revision,
        )
    assert path.read_bytes() == before


def test_removed_catalogue_source_stales_history_and_returns_item_to_remaining_queue(
    project, monkeypatch
):
    item_id = "skill:workflow/control"
    revision = coverage_status(project)["revision"]
    record_execution(
        project,
        item_id,
        {
            "status": "succeeded",
            "reason": "Scoped by a specialist",
            "reviewer": "Specialist",
            "signoff": True,
        },
        revision,
    )
    catalogue = dict(coverage.load_catalogue())
    catalogue.pop(item_id)
    monkeypatch.setattr(coverage, "load_catalogue", lambda: catalogue)

    status = coverage_status(project)
    item = next(row for row in status["items"] if row["id"] == item_id)
    assert item["attempts"] == 1
    assert item["stale"] and not item["complete"]
    assert item_id in status["views"]["remaining"]
