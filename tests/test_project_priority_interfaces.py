"""The priority preview/apply boundary is shared by CLI and MCP."""

import json

import pytest

from seohead import cli
from seohead.servers import handlers
from seohead.servers.mcp_server import build_server


@pytest.mark.parametrize("prefix", [["project-priorities"], ["project", "priorities"]])
def test_priority_cli_preview_and_apply_mapping(prefix):
    parser = cli.build_parser()
    args = parser.parse_args([*prefix, "--directory", "project"])
    name, data = cli._build_kwargs("project-priorities", args)
    assert name == "project_priorities"
    assert data == {"directory": "project"}
    args = parser.parse_args(
        [
            *prefix,
            "--directory",
            "project",
            "--apply",
            "--expected-revision",
            "2",
            "--input",
            json.dumps({"policy": {"format": "test"}}),
        ]
    )
    assert cli._build_kwargs("project-priorities", args)[1] == {
        "directory": "project",
        "apply": True,
        "expected_revision": 2,
        "policy": {"format": "test"},
    }


def test_priority_mcp_forwards_the_explicit_apply_boundary(monkeypatch):
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "applied": False}

    monkeypatch.setattr(handlers, "project_priorities", fake)
    tool = build_server()._tool_manager.get_tool("seo_project_priorities")
    tool.fn(directory="project")
    assert captured == {
        "directory": "project",
        "policy": None,
        "apply": False,
        "expected_revision": None,
    }
    assert tool.annotations.readOnlyHint is False
    tool.fn(directory="project", apply=True, expected_revision=2)
    assert captured["apply"] and captured["expected_revision"] == 2
