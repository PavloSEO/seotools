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


def project_facts(
    directory: str,
    facts: list[dict[str, Any]] | None = None,
    detect: bool = False,
    apply: bool = False,
    tools: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Preview or record project facts; a detection runs only when explicitly requested."""
    from seohead.projects.facts import project_facts as core

    return core(directory, facts=facts, detect=detect, apply=apply, tools=tools)


def project_checklist_init(
    directory: str, template: dict | None = None, expected_revision: int | None = None
) -> dict[str, Any]:
    """Create or reconcile a project's local checklist without running any item."""
    return initialize_coverage(directory, template=template, expected_revision=expected_revision)


def project_checklist_update(directory: str, item: dict, expected_revision: int) -> dict[str, Any]:
    """Update one checklist definition without executing it."""
    from seohead.projects.runtime import resolve_item_scope

    if not isinstance(item, dict):
        raise ValueError("item must be an object")
    target, local_id = resolve_item_scope(directory, item.get("id"))
    return update_item(target, item={**item, "id": local_id}, expected_revision=expected_revision)


def project_checklist_record(
    directory: str, item_id: str, record: dict, expected_revision: int
) -> dict[str, Any]:
    """Record supplied evidence for one item without running its operation."""
    from seohead.projects.runtime import resolve_item_scope

    target, local_id = resolve_item_scope(directory, item_id)
    return record_execution(
        target, item_id=local_id, record=record, expected_revision=expected_revision
    )


def project_priorities(
    directory: str,
    policy: dict | None = None,
    apply: bool = False,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Preview or explicitly apply an offline priority policy."""
    from seohead.projects.priorities import project_priorities as core

    return core(directory, policy=policy, apply=apply, expected_revision=expected_revision)
