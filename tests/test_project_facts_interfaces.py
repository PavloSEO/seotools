"""The fact preview/record boundary is shared by CLI and MCP."""

import json

import pytest

from seohead import cli
from seohead.servers import handlers
from seohead.servers.mcp_server import build_server


@pytest.mark.parametrize("prefix", [["project-facts"], ["project", "facts"]])
def test_fact_cli_preview_and_record_mapping(prefix):
    parser = cli.build_parser()
    args = parser.parse_args([*prefix, "--directory", "project"])
    name, data = cli._build_kwargs("project-facts", args)
    assert name == "project_facts"
    assert data == {"directory": "project"}

    supplied = [{"name": "cms", "value": "MODX", "provenance": "client", "observed_at": None}]
    args = parser.parse_args(
        [
            *prefix,
            "--directory",
            "project",
            "--detect",
            "--apply",
            "--input",
            json.dumps({"facts": supplied}),
        ]
    )
    assert cli._build_kwargs("project-facts", args)[1] == {
        "directory": "project",
        "detect": True,
        "apply": True,
        "facts": supplied,
    }


def test_fact_mcp_forwards_the_explicit_detect_and_apply_boundary(monkeypatch):
    captured = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "applied": False}

    monkeypatch.setattr(handlers, "project_facts", fake)
    tool = build_server()._tool_manager.get_tool("seo_project_facts")
    tool.fn(directory="project")
    assert captured == {"directory": "project", "facts": None, "detect": False, "apply": False}
    assert tool.annotations.readOnlyHint is False

    tool.fn(directory="project", detect=True, apply=True)
    assert captured["detect"] and captured["apply"]


def test_the_handler_injects_the_shared_single_page_tools(monkeypatch):
    captured = {}

    def fake(directory, facts=None, detect=False, apply=False, tools=None):
        captured.update(directory=directory, tools=tools)
        return {"ok": True}

    monkeypatch.setattr("seohead.servers.project_handlers.project_facts", fake)
    handlers.project_facts(directory="project", detect=True)
    assert captured["tools"] is handlers.HANDLERS
