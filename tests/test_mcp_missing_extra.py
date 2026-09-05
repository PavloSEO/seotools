"""`seohead mcp` must diagnose a missing optional SDK, not crash (#366).

The MCP server cannot emit any protocol data before its transport starts, so
the CLI boundary for this command is a one-line stderr diagnostic and exit
code 1, with stdout left empty -- never an uncaught ``ModuleNotFoundError``
traceback from the console entry point.
"""

from __future__ import annotations

import builtins
import contextlib
import io
from unittest.mock import patch

import pytest

from seohead import cli


def _block_mcp_imports(name, *args, **kwargs):
    if name == "mcp" or name.startswith("mcp."):
        raise ModuleNotFoundError("No module named 'mcp'", name=name)
    return _real_import(name, *args, **kwargs)


_real_import = builtins.__import__


def test_cli_mcp_reports_the_missing_extra_and_exits_1_without_a_traceback():
    stderr, stdout = io.StringIO(), io.StringIO()
    with (
        patch("builtins.__import__", side_effect=_block_mcp_imports),
        contextlib.redirect_stderr(stderr),
        contextlib.redirect_stdout(stdout),
    ):
        code = cli.main(["mcp"])

    assert code == 1
    assert stdout.getvalue() == ""
    message = stderr.getvalue()
    assert message.count("\n") == 1, message  # one concise diagnostic, not a traceback
    assert "mcp" in message.lower()
    assert "install" in message.lower()
    assert 'pip install "seohead-seotools[mcp]"' in message


def test_cli_mcp_leaves_the_real_server_untouched_when_the_sdk_is_installed(monkeypatch):
    """Negative control: the neighbouring legitimate case (SDK present, nothing missing)
    must stay silent -- no diagnostic, normal server construction, exit code 0."""
    pytest.importorskip("mcp")
    started = []
    monkeypatch.setattr("seohead.servers.mcp_server.main", lambda: started.append(True) or 0)
    assert cli.main(["mcp"]) == 0
    assert started == [True]


def test_the_module_s_own_main_gives_the_same_diagnostic(capsys):
    """The module docstring advertises `python -m seohead.servers.mcp_server` as
    equivalent to `seohead mcp` (its `if __name__ == "__main__"` block just calls this
    same `main()`), so both entry points must agree instead of one crashing while the
    other explains itself -- this is the single place that diagnostic is produced."""
    from seohead.servers.mcp_server import main as mcp_main

    with patch("builtins.__import__", side_effect=_block_mcp_imports):
        assert mcp_main() == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert 'pip install "seohead-seotools[mcp]"' in err
