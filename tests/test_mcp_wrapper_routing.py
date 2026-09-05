"""Every seo_* MCP wrapper must forward to its matching handler through _checked.

Two independent temporary-source mutations against this file each passed all
98 existing registration/binding/MCP tests while silently breaking a tool:

1. ``seo_hreflang_check`` rewritten to call ``handlers.headers_check`` (the
   existing, compatible handler) instead of ``handlers.hreflang_check`` -- the
   hreflang tool returned the headers result.
2. ``_checked(...)`` removed from only that one wrapper -- a real local stdio
   call then returned a handler ``ok: false`` payload with MCP ``isError:
   false``, the exact success/failure conflation ``_checked`` exists to
   prevent (see its docstring in seohead/servers/mcp_server.py).

test_registration.py proves set membership (every handler has an MCP tool and
back); test_interface_binding.py proves the forwarded keywords are ones the
handler accepts. Neither proves *which* handler a wrapper calls, or that the
call is wrapped in the one function that turns a handler's own-reported
failure into an MCP error. This gate derives both, from the AST, once, so a
tool list never has to be hand-maintained a third time.
"""

from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
MCP_SERVER = ROOT / "seohead" / "servers" / "mcp_server.py"

# A wrapper whose name suffix and forwarded handler name are allowed to differ,
# with the reason. Empty today: every current seo_* wrapper's suffix already
# matches its handler's name (see test_every_wrapper_name_matches_its_handler).
REVIEWED_NAME_ALIASES: dict[str, str] = {}


def _seo_wrapper_functions(tree: ast.AST) -> list[ast.FunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("seo_")
    ]


def _handler_calls(func: ast.FunctionDef) -> list[ast.Call]:
    """Every direct `handlers.<name>(...)` call anywhere in the wrapper's body."""
    return [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "handlers"
    ]


def _checked_wrapped_ids(func: ast.FunctionDef) -> set[int]:
    """id() of every node that sits inside a `_checked(...)` call's arguments."""
    wrapped: set[int] = set()
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_checked"
        ):
            for arg in ast.walk(node):
                wrapped.add(id(arg))
    return wrapped


def _load_wrappers() -> list[ast.FunctionDef]:
    tree = ast.parse(MCP_SERVER.read_text(encoding="utf-8"))
    return _seo_wrapper_functions(tree)


WRAPPERS = _load_wrappers()


def test_at_least_the_known_wrapper_count_is_discovered():
    """A sanity floor: if source layout changes so the AST walk finds nothing,
    every assertion below would vacuously pass. 54 is the count on main at
    commit 436d784b."""
    assert len(WRAPPERS) >= 54, f"expected at least 54 seo_* wrappers, found {len(WRAPPERS)}"


def test_every_wrapper_calls_exactly_one_handler():
    problems = []
    for func in WRAPPERS:
        calls = _handler_calls(func)
        handler_names = {c.func.attr for c in calls}
        if len(handler_names) != 1:
            problems.append(
                f"{func.name}: forwards to {sorted(handler_names)}, expected exactly one"
            )
    assert not problems, "; ".join(problems)


def test_every_wrapper_name_matches_its_handler_or_has_a_reviewed_alias():
    problems = []
    for func in WRAPPERS:
        calls = _handler_calls(func)
        if not calls:
            problems.append(f"{func.name}: calls no handlers.* function")
            continue
        handler_name = calls[0].func.attr
        suffix = func.name[len("seo_") :]
        expected = REVIEWED_NAME_ALIASES.get(func.name, suffix)
        if handler_name != expected:
            problems.append(
                f"{func.name}: forwards to handlers.{handler_name}, expected handlers.{expected} "
                f"(add a REVIEWED_NAME_ALIASES entry if this is an intentional alias)"
            )
    assert not problems, "; ".join(problems)


def test_every_handler_call_is_wrapped_in_checked():
    problems = []
    for func in WRAPPERS:
        wrapped_ids = _checked_wrapped_ids(func)
        for call in _handler_calls(func):
            if id(call) not in wrapped_ids:
                problems.append(
                    f"{func.name}: handlers.{call.func.attr}(...) is not inside _checked(...)"
                )
    assert not problems, "; ".join(problems)


# -- mutation tests: prove the two historical breakages above are caught ------


def _parse_wrapper(source: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name.startswith("seo_")
    )


def test_gate_catches_a_wrapper_forwarding_to_the_wrong_handler():
    """Mutation 1: seo_hreflang_check rewritten to call handlers.headers_check."""
    func = _parse_wrapper(
        "def seo_hreflang_check(url):\n    return _checked(handlers.headers_check(url=url))\n"
    )
    handler_name = _handler_calls(func)[0].func.attr
    suffix = func.name[len("seo_") :]
    assert handler_name != suffix, "the mutated wrapper must not read as matching its name"


def test_gate_catches_checked_removed_from_one_wrapper():
    """Mutation 2: _checked(...) stripped from a single wrapper."""
    func = _parse_wrapper(
        "def seo_hreflang_check(url):\n    return handlers.hreflang_check(url=url)\n"
    )
    wrapped_ids = _checked_wrapped_ids(func)
    calls = _handler_calls(func)
    assert calls and id(calls[0]) not in wrapped_ids, (
        "an unwrapped handler call must not read as wrapped"
    )


def test_gate_stays_silent_on_a_correct_wrapper():
    """Negative control: a normal, correctly-wired wrapper must not be flagged by either check."""
    func = _parse_wrapper(
        "def seo_hreflang_check(url):\n    return _checked(handlers.hreflang_check(url=url))\n"
    )
    calls = _handler_calls(func)
    assert len(calls) == 1
    handler_name = calls[0].func.attr
    suffix = func.name[len("seo_") :]
    assert handler_name == suffix
    assert id(calls[0]) in _checked_wrapped_ids(func)
