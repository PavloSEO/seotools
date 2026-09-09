"""Public handler wrappers for local project workspace operations."""

from __future__ import annotations

from typing import Any

from seohead.projects.coverage import initialize_coverage, record_execution, update_item
from seohead.projects.workspace import create_project, open_project, project_status


def project_new(
    directory: str,
    target: str,
    label: str | None = None,
    facts: list[dict[str, Any]] | None = None,
    template_references: list[str] | None = None,
    profile_references: list[str] | None = None,
) -> dict[str, Any]:
    return create_project(
        directory,
        target,
        label=label,
        facts=facts,
        template_references=template_references,
        profile_references=profile_references,
    )


def project_open(directory: str, expected_site: str | None = None) -> dict[str, Any]:
    return open_project(directory, expected_site=expected_site)


def project_basic_status(directory: str) -> dict[str, Any]:
    return project_status(directory)


def project_checklist_init(
    directory: str, template: dict | None = None, expected_revision: int | None = None
) -> dict[str, Any]:
    """Create or reconcile a project's local checklist without running any item."""
    return initialize_coverage(directory, template=template, expected_revision=expected_revision)


def project_checklist_update(directory: str, item: dict, expected_revision: int) -> dict[str, Any]:
    """Update one checklist definition without executing it."""
    return update_item(directory, item=item, expected_revision=expected_revision)


def project_checklist_record(
    directory: str, item_id: str, record: dict, expected_revision: int
) -> dict[str, Any]:
    """Record supplied evidence for one item without running its operation."""
    return record_execution(
        directory, item_id=item_id, record=record, expected_revision=expected_revision
    )
