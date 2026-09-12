"""Every tool must be reachable through both the CLI and MCP.

Adding a tool requires registration in four places: the core module,
``handlers.HANDLERS``, the CLI command table, and ``@mcp.tool()``. A historical
drift case normalized ``soft-404-check`` to ``soft_404_check`` while its handler
was named ``soft404_check``. The CLI then failed with ``error: 'soft_404_check'``
even though unit tests passed and MCP still worked.

These tests compare the public registries so that such drift cannot recur silently.
"""

from __future__ import annotations

import re
from pathlib import Path

from seohead import cli
from seohead.cli import COMMANDS
from seohead.servers.handlers import HANDLERS

ROOT = Path(__file__).resolve().parent.parent
MCP_SOURCE = (ROOT / "seohead" / "servers" / "mcp_server.py").read_text(encoding="utf-8")

# Crawl-audit tools are registered separately by sf_mcp under the sf_* prefix
# and therefore do not pass through HANDLERS.
MCP_TOOLS = {name[len("seo_") :] for name in re.findall(r"def (seo_\w+)\(", MCP_SOURCE)}


def _handler_of(command: str) -> str:
    """Apply the same normalization used by ``cli._build_kwargs``."""
    return command.replace("-", "_")


def test_every_cli_command_has_a_handler():
    missing = sorted(c for c in COMMANDS if _handler_of(c) not in HANDLERS)
    assert not missing, (
        f"CLI commands without handlers: {missing}. "
        f"A hyphenated command name must normalize to a HANDLERS key"
    )


def test_every_handler_is_reachable_from_cli():
    exposed = {_handler_of(c) for c in COMMANDS}
    missing = sorted(set(HANDLERS) - exposed)
    assert not missing, f"handlers without CLI commands: {missing}"


def test_every_handler_is_exposed_over_mcp():
    missing = sorted(set(HANDLERS) - MCP_TOOLS)
    assert not missing, f"handlers without MCP tools: {missing}"


def test_every_mcp_tool_has_a_handler():
    missing = sorted(MCP_TOOLS - set(HANDLERS))
    assert not missing, f"MCP tools without handlers: {missing}"


def test_cli_commands_are_unique():
    assert len(COMMANDS) == len(set(COMMANDS)), "COMMANDS contains duplicates"


def test_cli_command_names_are_well_formed():
    """Allow only lowercase letters, digits, and hyphens for reliable normalization."""
    bad = [c for c in COMMANDS if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", c)]
    assert not bad, f"malformed command names: {bad}"


def test_input_is_json_and_scan_is_the_only_saved_artifact_flag():
    """#701: parser registration may never give --input two incompatible meanings."""
    parser = cli.build_parser()
    stack = [parser]
    actions = []
    while stack:
        current = stack.pop()
        for action in current._actions:
            actions.append(action)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                stack.extend(choices.values())

    assert not [
        action
        for action in actions
        if "--input" in action.option_strings and action.dest == "input_path"
    ]
    assert sum("--scan" in action.option_strings for action in actions) >= 6
