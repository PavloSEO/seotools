"""Checklist coverage commands keep their CLI and MCP argument contracts aligned."""

from __future__ import annotations

from seohead import cli
from seohead.servers import handlers
from seohead.servers.mcp_server import build_server


def test_cli_checklist_commands_accept_json_payloads_and_flag_overrides():
    init = cli.build_parser().parse_args(
        [
            "project",
            "checklist-init",
            "--directory",
            "from-flag",
            "--expected-revision",
            "4",
            "--input",
            '{"directory":"from-input","template":{"format":"seohead.checklist-template.v1","items":[]}}',
        ]
    )
    command = "project-" + init.project_command
    handler, kwargs = cli._build_kwargs(command, init)
    assert handler == "project_checklist_init"
    assert kwargs == {
        "directory": "from-flag",
        "template": {"format": "seohead.checklist-template.v1", "items": []},
        "expected_revision": 4,
    }

    update = cli.build_parser().parse_args(
        [
            "project-checklist-update",
            "--directory",
            "project",
            "--expected-revision",
            "5",
            "--input",
            '{"item":{"id":"custom:copy-review"}}',
        ]
    )
    handler, kwargs = cli._build_kwargs("project-checklist-update", update)
    assert handler == "project_checklist_update"
    assert kwargs == {
        "directory": "project",
        "expected_revision": 5,
        "item": {"id": "custom:copy-review"},
    }

    record = cli.build_parser().parse_args(
        [
            "project",
            "checklist-record",
            "--directory",
            "project",
            "--item-id",
            "custom:copy-review",
            "--expected-revision",
            "6",
            "--input",
            '{"record":{"status":"not_applicable","reason":"No copy"}}',
        ]
    )
    command = "project-" + record.project_command
    handler, kwargs = cli._build_kwargs(command, record)
    assert handler == "project_checklist_record"
    assert kwargs == {
        "directory": "project",
        "item_id": "custom:copy-review",
        "expected_revision": 6,
        "record": {"status": "not_applicable", "reason": "No copy"},
    }

    report = cli.build_parser().parse_args(
        [
            "report-build",
            "--audit",
            "audit.json",
            "--format",
            "md",
            "--out",
            "report.md",
            "--project",
            "project",
        ]
    )
    handler, kwargs = cli._build_kwargs("report-build", report)
    assert handler == "report_build"
    assert kwargs == {
        "audit": "audit.json",
        "fmt": "md",
        "out": "report.md",
        "project": "project",
    }


def test_mcp_checklist_tools_forward_data_without_execution(monkeypatch):
    captured = {}

    def capture(name):
        def call(**kwargs):
            captured[name] = kwargs
            return {"state": "initialized", "revision": 1, "counts": {}, "views": {}, "items": []}

        return call

    monkeypatch.setattr(handlers, "project_checklist_init", capture("init"))
    monkeypatch.setattr(handlers, "project_checklist_update", capture("update"))
    monkeypatch.setattr(handlers, "project_checklist_record", capture("record"))
    monkeypatch.setattr(handlers, "report_build", capture("report"))
    manager = build_server()._tool_manager

    manager.get_tool("seo_project_checklist_init").fn(
        directory="project", template=None, expected_revision=0
    )
    manager.get_tool("seo_project_checklist_update").fn(
        directory="project", item={"id": "custom:copy-review"}, expected_revision=1
    )
    manager.get_tool("seo_project_checklist_record").fn(
        directory="project",
        item_id="custom:copy-review",
        record={"status": "not_applicable", "reason": "No copy"},
        expected_revision=2,
    )
    manager.get_tool("seo_report_build").fn(
        audit={"schema_version": "2.0"}, fmt="md", out="report.md", project="project"
    )

    assert captured == {
        "init": {"directory": "project", "template": None, "expected_revision": 0},
        "update": {
            "directory": "project",
            "item": {"id": "custom:copy-review"},
            "expected_revision": 1,
        },
        "record": {
            "directory": "project",
            "item_id": "custom:copy-review",
            "record": {"status": "not_applicable", "reason": "No copy"},
            "expected_revision": 2,
        },
        "report": {
            "audit": {"schema_version": "2.0"},
            "fmt": "md",
            "out": "report.md",
            "project": "project",
        },
    }
