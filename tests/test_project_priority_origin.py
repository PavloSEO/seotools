"""Priority provenance and non-semantic scheduling changes for #648 child B."""

from __future__ import annotations

import json

import pytest

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


def _update(project, item):
    return update_item(project, item, coverage_status(project)["revision"])


def _record(project, item_id):
    return record_execution(
        project,
        item_id,
        {"status": "succeeded", "reason": "Reviewed", "reviewer": "Specialist", "signoff": True},
        coverage_status(project)["revision"],
    )


def _row(project, item_id):
    return next(item for item in coverage_status(project)["items"] if item["id"] == item_id)


def test_defaults_are_marked_default_and_explicit_priorities_are_operator(project):
    assert _row(project, "check:BROKEN_PAGE_4XX")["priority_origin"] == "default"
    _update(project, {"id": "custom:implicit"})
    assert _row(project, "custom:implicit")["priority_origin"] == "default"
    _update(project, {"id": "custom:explicit", "priority": "P1"})
    assert _row(project, "custom:explicit")["priority_origin"] == "operator"

    initialize_coverage(
        project,
        {
            "format": "seohead.checklist-template.v1",
            "items": [{"id": "custom:template", "priority": "P2"}],
        },
        coverage_status(project)["revision"],
    )
    template = _row(project, "custom:template")
    assert (template["priority"], template["priority_origin"]) == ("P2", "operator")


def test_priority_origin_cannot_be_supplied_by_operator_input(project):
    before = (project / "coverage.json").read_bytes()
    with pytest.raises(ValueError, match="priority origin"):
        _update(project, {"id": "custom:spoof", "priority_origin": "default"})
    assert (project / "coverage.json").read_bytes() == before


def test_scheduling_changes_keep_completion_but_semantic_changes_stale_it(project):
    _update(project, {"id": "custom:scheduled"})
    _record(project, "custom:scheduled")

    _update(project, {"id": "custom:scheduled", "priority": "P0", "order": 77})
    scheduled = _row(project, "custom:scheduled")
    assert scheduled["state"] == "run" and scheduled["complete"] is True
    assert scheduled["stale"] is False and scheduled["priority_origin"] == "operator"
    assert scheduled["definition_versions"] == 2 and scheduled["attempts"] == 1

    _update(project, {"id": "custom:scheduled", "enabled": False})
    disabled = _row(project, "custom:scheduled")
    assert disabled["state"] == "run" and disabled["stale"] is False
    assert disabled["complete"] is False and disabled["attempts"] == 1

    _update(project, {"id": "custom:scheduled", "enabled": True, "title": "Changed review"})
    changed = _row(project, "custom:scheduled")
    assert changed["state"] == "not_run" and changed["stale"] is True
    assert changed["reason"] == "item definition changed"


def test_priority_origin_is_persisted_in_current_and_history_definitions(project):
    _update(project, {"id": "custom:history", "priority": "P2"})
    document = json.loads((project / "coverage.json").read_text())
    item = document["items"]["custom:history"]
    assert item["definition"]["priority_origin"] == "operator"
    assert item["definitions"][-1]["definition"]["priority_origin"] == "operator"


def test_scope_and_operation_changes_are_not_scheduling_changes(project):
    _update(project, {"id": "custom:scoped"})
    _record(project, "custom:scoped")
    _update(
        project,
        {
            "id": "custom:scoped",
            "scope": {
                "site": "https://example.test/",
                "template": "product",
                "urls": ["https://example.test/"],
            },
        },
    )
    assert _row(project, "custom:scoped")["stale"] is True

    from seohead.projects.coverage import _completion_hash

    original = {
        "id": "custom:auto",
        "title": "Automatic",
        "kind": "custom",
        "scope": {"site": "https://example.test/", "template": None, "urls": []},
        "dependencies": [],
        "execution_kind": "automatic",
        "priority": "P1",
        "priority_origin": "default",
        "enabled": True,
        "order": 0,
        "operation": "check:BROKEN_PAGE_4XX",
        "source_hash": None,
    }
    changed = {**original, "operation": "check:TITLE_MISSING"}
    assert _completion_hash(original) != _completion_hash(changed)


def test_shipped_ecommerce_template_is_reusable(tmp_path):
    import json
    from pathlib import Path

    from seohead.projects.coverage import initialize_coverage
    from seohead.projects.workspace import create_project

    template = json.loads(
        (Path(__file__).parents[1] / "examples/ecommerce-checklist.json").read_text()
    )
    for name in ("first", "second"):
        root = tmp_path / name
        create_project(root, "https://example.test/")
        status = initialize_coverage(root, template=template)
        items = {item["id"]: item for item in status["items"]}
        assert items["custom:product-response"]["priority_origin"] == "operator"
        assert items["custom:product-review"]["blocked_by"] == ["custom:product-response"]
        assert status["counts"]["complete"] == 0
