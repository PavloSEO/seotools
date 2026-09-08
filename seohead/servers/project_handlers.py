"""Temporary public wrappers for project workspaces; CLI/MCP registration is integrated separately."""

from __future__ import annotations

from typing import Any

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
