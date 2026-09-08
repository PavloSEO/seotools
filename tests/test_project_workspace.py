"""Portable project workspace contracts for #648 child A / #678."""

from __future__ import annotations

import json
import shutil

import pytest

from seohead import cli
from seohead.projects import workspace
from seohead.projects.workspace import create_project, open_project, project_status


def test_create_open_status_and_move_preserve_relative_artifact_references(tmp_path):
    project = tmp_path / "example"
    created = create_project(
        project,
        "https://example.test/",
        facts=[{"provenance": "fixture", "observed_at": None, "value": "synthetic", "name": "cms"}],
        template_references=["ecommerce/product-card"],
        profile_references=["ecommerce/basic"],
    )
    assert created["project"]["artifact_directories"]["scans"] == "scans"
    assert created["project"]["project_uuid"]
    assert created["project"]["created_at"].endswith("Z")
    status = project_status(project)
    assert status["checklist"]["state"] == "not_initialized"
    assert "0/0" not in json.dumps(status)
    assert status["preparation"]["state"] == "pending"

    moved = tmp_path / "moved"
    shutil.move(project, moved)
    assert open_project(moved)["project"]["site"]["target"] == "https://example.test/"
    assert project_status(moved)["scans"]["items"] == []


def test_creation_never_overwrites_and_conflicting_identity_refuses(tmp_path):
    project = tmp_path / "example"
    create_project(project, "https://example.test/")
    before = (project / "project.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        create_project(project, "https://other.test/")
    assert (project / "project.json").read_bytes() == before
    with pytest.raises(ValueError, match="conflicts"):
        open_project(project, expected_site="other.test")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda project: (project / "scans").rmdir(),
        lambda project: (project / "project.json").write_text('{"version":999}', encoding="utf-8"),
    ],
)
def test_malformed_or_incomplete_projects_never_open_as_successful(tmp_path, mutate):
    project = tmp_path / "example"
    create_project(project, "https://example.test/")
    mutate(project)
    with pytest.raises(ValueError):
        open_project(project)


def test_references_and_facts_refuse_traversal_and_secrets(tmp_path):
    with pytest.raises(ValueError, match="relative"):
        create_project(tmp_path / "unsafe", "https://example.test/", template_references=["../run"])
    with pytest.raises(ValueError, match="credentials"):
        create_project(
            tmp_path / "secrets",
            "https://example.test/",
            facts=[
                {"name": "api_token", "value": "x", "provenance": "fixture", "observed_at": None}
            ],
        )


def test_project_path_rejects_symlink_escape(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "project-link"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="already exists"):
        create_project(link, "https://example.test/")
    with pytest.raises(ValueError, match="traversal"):
        create_project(tmp_path / "nested" / ".." / "outside", "https://example.test/")


def test_facts_are_key_order_independent_and_preserve_scalar_values(tmp_path):
    facts = [
        {"value": "text", "observed_at": None, "name": "string", "provenance": "fixture"},
        {
            "provenance": "fixture",
            "name": "integer",
            "value": 7,
            "observed_at": "2026-01-02T03:04:05Z",
        },
        {"name": "boolean", "value": True, "provenance": "fixture", "observed_at": None},
    ]
    created = create_project(tmp_path / "facts", "https://example.test/", facts=facts)
    assert created["project"]["facts"] == [
        {"name": "string", "value": "text", "provenance": "fixture", "observed_at": None},
        {
            "name": "integer",
            "value": 7,
            "provenance": "fixture",
            "observed_at": "2026-01-02T03:04:05Z",
        },
        {"name": "boolean", "value": True, "provenance": "fixture", "observed_at": None},
    ]


def test_site_label_never_overrides_target_host_and_boolean_version_refuses(tmp_path):
    project = tmp_path / "labeled"
    create_project(project, "https://Example.test/", label="Client label")
    assert open_project(project, expected_site="example.test")["project"]["site"] == {
        "host": "example.test",
        "target": "https://example.test/",
        "label": "Client label",
    }
    document = json.loads((project / "project.json").read_text(encoding="utf-8"))
    document["version"] = True
    (project / "project.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        open_project(project)


@pytest.mark.parametrize(
    "site",
    [
        {"id": 7, "target": "https://example.test/"},
        {"host": "example.test", "label": 7, "target": "https://example.test/"},
    ],
)
def test_unknown_or_wrongly_typed_site_identity_refuses_cleanly(tmp_path, site):
    project = tmp_path / "identity"
    create_project(project, "https://example.test/")
    document = json.loads((project / "project.json").read_text(encoding="utf-8"))
    document["site"] = site
    (project / "project.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="site identity"):
        open_project(project)


def test_failed_manifest_publish_never_creates_a_false_project(tmp_path, monkeypatch):
    project = tmp_path / "interrupted"

    def fail(*_args, **_kwargs):
        raise OSError("simulated publish interruption")

    monkeypatch.setattr(workspace, "_publish_manifest", fail)
    with pytest.raises(OSError, match="interruption"):
        create_project(project, "https://example.test/")
    assert not (project / "project.json").exists()
    with pytest.raises(ValueError, match=r"project\.json"):
        open_project(project)


def test_nested_and_flat_project_cli_aliases(tmp_path, capsys):
    project = tmp_path / "cli"
    assert (
        cli.main(
            ["project", "new", "--directory", str(project), "--target", "https://example.test/"]
        )
        == 0
    )
    assert cli.main(["project-status", "--directory", str(project)]) == 0
    assert cli.main(["project", "open", "--directory", str(project)]) == 0
    assert '"not_initialized"' in capsys.readouterr().out
