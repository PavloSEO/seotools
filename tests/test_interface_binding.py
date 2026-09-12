"""Every interface tool must be able to reach the handler it forwards to.

CI already asserts that each CLI command and MCP tool *registers*. Registration
only proves the decorator ran; it says nothing about whether the body can call
what it calls. seo_crawl_site forwarded a keyword its handler never accepted and
raised TypeError on every invocation for as long as it existed, invisibly,
because nothing ever called it.

Issue #152 found the opposite gap in the same pair of surfaces: the CLI could
name ``--sitemap`` and ``--config`` for ``crawl-site``, but the MCP tool had no
parameter for either, so a client driving this toolkit only through MCP could
never reach the sitemap-reconciliation crawl mode at all. Nothing above caught
it because every existing test here starts from what MCP forwards and asks
whether the handler accepts it -- never the reverse question, "can the CLI
name something MCP can't."
"""

from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from seohead.servers import handlers

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SERVER = ROOT / "seohead" / "servers" / "mcp_server.py"
CLI = ROOT / "seohead" / "cli.py"


def _forwarding_calls() -> list[tuple[str, str, set[str]]]:
    """Each (tool, handler, forwarded keywords) triple found in the MCP server.

    Attributed to the innermost enclosing function: the tools are nested inside
    build_server, so walking every function would report each call twice, once
    under the tool and once under its enclosing scope.
    """
    out: list[tuple[str, str, set[str]]] = []

    def visit(node: ast.AST, owner: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, child.name)
                continue
            if isinstance(child, ast.Call):
                func = child.func
                if (
                    isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "handlers"
                ):
                    out.append((owner, func.attr, {kw.arg for kw in child.keywords if kw.arg}))
            visit(child, owner)

    visit(ast.parse(MCP_SERVER.read_text(encoding="utf-8")), "<module>")
    return out


FORWARDS = _forwarding_calls()


def _literal_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _cmd_names_from_test(test: ast.AST) -> list[str]:
    """``cmd == "x"`` or ``cmd in ("a", "b")`` -> the literal command name(s), else []."""
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        op, right = test.ops[0], test.comparators[0]
        if isinstance(op, ast.Eq):
            name = _literal_str(right)
            return [name] if name else []
        if isinstance(op, ast.In) and isinstance(right, ast.Tuple | ast.List):
            return [s for s in (_literal_str(e) for e in right.elts) if s]
    return []


def _cli_explicit_flags() -> dict[str, set[str]]:
    """cmd -> the handler kwargs ``_build_kwargs`` can set from a named flag.

    Everything ``_build_kwargs`` does is one of two shapes: ``kw["x"] = ...``
    (a literal key), or ``for flag in ("a", "b"): ... kw[flag] = ...`` (a
    variable key drawn from a literal tuple, the shape ``crawl-site`` uses).
    Both are walked here so a future flag added either way is still counted.
    ``--input`` is deliberately invisible to this: it is the escape hatch that
    already exists for the CLI, not a named flag a user has to know exists.
    """
    tree = ast.parse(CLI.read_text(encoding="utf-8"))
    build_kwargs = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_build_kwargs"
    )
    result: dict[str, set[str]] = {}

    def record(cmds: list[str], key: str) -> None:
        if key.startswith("_"):  # a CLI-only sentinel popped before the handler call
            return
        for cmd in cmds:
            result.setdefault(cmd, set()).add(key)

    def walk(body: list[ast.stmt], cmds: list[str], loop_var: str | None, loop_keys: set[str]):
        for stmt in body:
            if isinstance(stmt, ast.If):
                sub_cmds = _cmd_names_from_test(stmt.test) or cmds
                walk(stmt.body, sub_cmds, loop_var, loop_keys)
                walk(stmt.orelse, cmds, loop_var, loop_keys)  # elif is nested here
                continue
            if isinstance(stmt, ast.For):
                keys = set()
                if isinstance(stmt.iter, ast.Tuple | ast.List):
                    keys = {s for s in (_literal_str(e) for e in stmt.iter.elts) if s}
                target = stmt.target.id if isinstance(stmt.target, ast.Name) else None
                walk(stmt.body, cmds, target, keys)
                continue
            for node in ast.walk(stmt):
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if not (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "kw"
                    ):
                        continue
                    key = _literal_str(target.slice)
                    if key:
                        record(cmds, key)
                    elif (
                        loop_var
                        and isinstance(target.slice, ast.Name)
                        and target.slice.id == loop_var
                    ):
                        for k in loop_keys:
                            record(cmds, k)

    walk(build_kwargs.body, [], None, set())
    return result


CLI_EXPLICIT_FLAGS = _cli_explicit_flags()
MCP_FORWARDED_BY_HANDLER: dict[str, set[str]] = {}
for _tool, _handler, _kws in FORWARDS:
    MCP_FORWARDED_BY_HANDLER.setdefault(_handler, set()).update(_kws)

# (cmd, handler_name) for every command reachable from both interfaces, so a handler
# added to one surface's explicit-flag set and not the other's is exactly what fails below.
_SHARED_HANDLERS = sorted(
    (cmd, cmd.replace("-", "_"))
    for cmd in CLI_EXPLICIT_FLAGS
    if cmd.replace("-", "_") in MCP_FORWARDED_BY_HANDLER
)


def test_the_ast_walk_found_crawl_site_and_its_tuple_loop():
    # Pins the one non-obvious shape (for flag in (...): kw[flag] = ...) this walker exists
    # to handle -- a refactor of _build_kwargs that changes that shape should fail loudly here.
    assert {"config", "max_urls", "max_depth", "min_delay", "out_dir", "robots", "sitemap"} <= (
        CLI_EXPLICIT_FLAGS.get("crawl-site") or set()
    )


@pytest.mark.parametrize(
    ("cmd", "handler_name"), _SHARED_HANDLERS, ids=[c for c, _ in _SHARED_HANDLERS]
)
def test_mcp_exposes_everything_the_cli_can_name_explicitly(cmd, handler_name):
    """The reverse direction from the rest of this module (#152): a kwarg the CLI
    can set from a named flag must also be reachable from MCP, since MCP has no
    ``--input``-style escape hatch for a parameter nobody thought to add.
    """
    cli_flags = CLI_EXPLICIT_FLAGS[cmd]
    mcp_flags = MCP_FORWARDED_BY_HANDLER[handler_name]
    missing = cli_flags - mcp_flags
    assert not missing, (
        f"'{cmd}' can set {sorted(missing)} from a named CLI flag, but no MCP tool "
        f"forwarding to handlers.{handler_name} accepts them: {sorted(mcp_flags)}"
    )


def test_the_scan_found_the_tools():
    # A refactor that moves the calls must fail loudly here rather than make
    # every assertion below vacuously true.
    assert len(FORWARDS) > 30


@pytest.mark.parametrize(
    ("tool", "handler_name", "forwarded"), FORWARDS, ids=[f[0] for f in FORWARDS]
)
def test_forwarded_keywords_exist_on_the_handler(tool, handler_name, forwarded):
    handler = getattr(handlers, handler_name, None)
    assert handler is not None, f"{tool} forwards to handlers.{handler_name}, which does not exist"
    accepted = set(inspect.signature(handler).parameters)
    unexpected = forwarded - accepted
    assert not unexpected, (
        f"{tool} passes {sorted(unexpected)} to handlers.{handler_name}, "
        f"which accepts {sorted(accepted)}"
    )


def test_crawl_site_accepts_every_robots_policy():
    # The boolean this tool used to take could not express report_only, which is
    # why the handler takes a policy instead.
    from seohead.crawl.settings import ROBOTS_POLICIES

    accepted = inspect.signature(handlers.crawl_site).parameters
    assert "robots" in accepted
    assert set(ROBOTS_POLICIES) == {"respect", "report_only", "ignore"}


def test_seo_crawl_site_declares_sitemap_urls_and_config():
    """Declared, not just forwarded (#152): the JSON schema an MCP client actually sees
    is what ``list_tools`` publishes, built from the tool's own parameter defaults --
    a client can only send what shows up here."""
    import asyncio

    from seohead.servers.mcp_server import build_server

    tools = asyncio.run(build_server().list_tools())
    schema = next(t for t in tools if t.name == "seo_crawl_site").inputSchema
    assert {"sitemap", "urls", "config"} <= set(schema["properties"])
    # url must not be required any more: urls-only (no start URL to follow links from)
    # is the handler's second entry mode, and it has to be reachable without a dummy url.
    assert "url" not in (schema.get("required") or [])


def test_seo_crawl_site_forwards_sitemap_urls_and_config_to_the_handler():
    """End to end, not just present on the signature: an MCP client's call must
    actually reach ``handlers.crawl_site`` with these arguments (#152)."""
    import asyncio
    from unittest.mock import patch

    from seohead.servers.mcp_server import build_server

    server = build_server()
    with patch.object(handlers, "crawl_site", return_value={"urls_collected": 0}) as spy:
        asyncio.run(
            server.call_tool(
                "seo_crawl_site",
                {
                    "url": "https://example.com/",
                    "sitemap": "https://example.com/sitemap.xml",
                    "urls": ["https://example.com/a"],
                    "config": "crawl.json",
                },
            )
        )
    assert spy.call_args.kwargs["sitemap"] == "https://example.com/sitemap.xml"
    assert spy.call_args.kwargs["urls"] == ["https://example.com/a"]
    assert spy.call_args.kwargs["config"] == "crawl.json"


# ── the core/interface boundary, with no hand-written list of directories ────

# The two packages that are allowed to compose everything else. Everything under
# seohead/ that is not one of these is core, whether or not it existed when this
# test was written.
INTERFACE_PACKAGES = {"servers"}
INTERFACE_MODULES = {"cli.py", "__main__.py"}


def _core_python_files() -> list[pathlib.Path]:
    """Every core source file, discovered rather than listed.

    CI enforced this rule with a grep over five directory names typed by hand,
    and the package has eight -- so ``seohead/audit/``, the one directory that
    actually crossed the boundary, was the one nobody checked (#221). A gate
    whose scope is maintained by memory has the same failure mode as the
    checkpoint in #188 and the export list in #136: it silently stops covering
    what it claims to, and says nothing.
    """
    package = ROOT / "seohead"
    return [
        path
        for path in package.rglob("*.py")
        if path.parts[path.parts.index("seohead") + 1] not in INTERFACE_PACKAGES
        and path.relative_to(package).as_posix() not in INTERFACE_MODULES
        and "__pycache__" not in path.parts
    ]


def _imports_the_interface(source: str) -> list[str]:
    """Import statements naming seohead.servers or seohead.cli, at any nesting.

    Deferred imports inside a function body count. They are how the cycle in
    #221 survived import time, which is exactly why the rule exists -- a cycle
    that only fails at call time is worse than one that fails on import, not
    better.
    """
    found: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[:2] in (["seohead", "servers"], ["seohead", "cli"]):
                found.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[:2] in (["seohead", "servers"], ["seohead", "cli"]):
                    found.append(alias.name)
    return found


def test_no_core_module_imports_the_interface_layer():
    """Core gathers evidence and judges it; the interface composes core. The
    dependency runs one way, and a module that reaches back inverts it."""
    offenders: list[str] = []
    for path in _core_python_files():
        for module in _imports_the_interface(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)} imports {module}")
    assert not offenders, "core modules must not import the CLI or server layer:\n" + "\n".join(
        sorted(offenders)
    )


def test_the_discovery_actually_sees_every_core_directory():
    """A guard that silently matched nothing would pass this file forever.

    Named directories here are the ones that existed when #221 was fixed; the
    assertion is that discovery finds at least these, not that the list is
    complete -- a new directory is covered without editing anything, which is
    the entire point.
    """
    found = {
        path.parts[path.parts.index("seohead") + 1]
        for path in _core_python_files()
        if path.name != "__init__.py"
    }
    for expected in ("audit", "crawl", "data_sources", "recon", "reports", "sf", "tools"):
        assert expected in found, f"boundary check no longer looks at seohead/{expected}/"
