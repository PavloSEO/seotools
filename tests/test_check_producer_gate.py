"""Every check ID a producer can emit must be registered; every check function must run.

``AuditContext.add``/``skip``/``add_group`` (seohead/sf/core/context.py) accept a
bare string ID and ``registry.check_meta`` fabricates default metadata for one
it has never heard of, so a typo'd ID quietly emits a finding outside
``CHECKS`` instead of failing loudly. Symmetrically, the rule modules dispatch
their ``check_*`` functions by hand (``rules.ALL_CHECKS``, direct calls in
``run_inlinks``/``run_heuristics``) rather than by reflection, so a function
that is implemented but never added to the dispatch list is simply never
called -- nothing before this gate derived "every function that exists" and
compared it to "every function that runs".

Both gates are source-derived (AST for literal call sites, the live objects
for the handful of maps a check ID reaches through a loop variable rather than
a literal) so that a new check_id or check_* function is covered automatically
without a third place to remember to update.
"""

from __future__ import annotations

import ast
import pathlib

from seohead.sf.core import aggregate, inlinks, rules
from seohead.sf.core.registry import CHECKS

ROOT = pathlib.Path(__file__).resolve().parent.parent
CORE = ROOT / "seohead" / "sf" / "core"

CTX_ID_METHODS = {"add", "skip", "add_group", "retract"}

SOURCE_FILES = [
    CORE / "rules.py",
    CORE / "inlinks.py",
    CORE / "heuristics.py",
    CORE / "aggregate.py",
    CORE / "sitemap_coverage.py",
]

# IDs that intentionally never appear in CHECKS: each names the absence of a
# whole export/stage (context.run_rules bailing out entirely), not one check's
# finding, so it has no severity/message/fix to register. Add here only with a
# comment saying why, never to silence a real typo.
NAMED_SENTINEL_EXCLUSIONS = {
    "INTERNAL_ALL",  # seohead/sf/core/rules.py: run_rules() when Internal:All never loaded
}


def _literal_check_ids(path: pathlib.Path) -> set[str]:
    """Every string literal passed as the first argument to ctx.add/skip/add_group/retract."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CTX_ID_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "ctx"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def _producer_map_ids() -> set[str]:
    """IDs reached only via a loop variable, read from the live objects rather than the AST.

    inlinks.run_inlinks calls ``ctx.skip(internal_check, ...)`` where
    ``internal_check`` comes from unpacking ``INLINK_SOURCES.values()``;
    rules.check_document_skeleton/check_native_exports and
    aggregate._withhold_*_findings do the same over their own small maps.
    Reading the actual dict/tuple/frozenset is exact and needs no data-flow
    tracing through the loop.
    """
    ids: set[str] = set()
    for internal_check, external_check in inlinks.INLINK_SOURCES.values():
        ids.add(internal_check)
        ids.add(external_check)
    ids.update(rules._NATIVE_EXPORT_CHECKS.values())
    ids.update(rules._SKELETON_CHECKS)
    ids.update(aggregate.UNLINKED_FINDING_CHECKS)
    ids.update(aggregate.GRAPH_WIDE_FINDING_CHECKS)
    return ids


def _unregistered(ids: set[str]) -> list[str]:
    return sorted(ids - set(CHECKS) - NAMED_SENTINEL_EXCLUSIONS)


def test_every_literal_check_id_is_registered_or_named():
    literal_ids: set[str] = set()
    for path in SOURCE_FILES:
        literal_ids |= _literal_check_ids(path)
    unregistered = _unregistered(literal_ids | _producer_map_ids())
    assert not unregistered, f"check IDs used but missing from CHECKS: {unregistered}"


def test_named_sentinel_exclusions_stay_actually_unregistered():
    """If a sentinel later gets a real CHECKS entry, drop it here -- it would
    otherwise silently stop being exercised by the mismatch it exists to allow."""
    stale = sorted(s for s in NAMED_SENTINEL_EXCLUSIONS if s in CHECKS)
    assert not stale, f"now registered in CHECKS, remove from NAMED_SENTINEL_EXCLUSIONS: {stale}"


def test_gate_flags_an_unknown_literal_id(tmp_path):
    """Positive control: a synthetic source using a made-up ID must be caught."""
    fake = tmp_path / "fake_rules.py"
    fake.write_text('def check_fake(ctx):\n    ctx.add("NOT_A_REAL_CHECK_ID")\n')
    found = _literal_check_ids(fake)
    assert _unregistered(found) == ["NOT_A_REAL_CHECK_ID"]


def test_gate_is_silent_on_a_genuine_check_id(tmp_path):
    """Negative control: a real, registered ID next to the bogus one must stay silent."""
    real_id = next(iter(CHECKS))
    fake = tmp_path / "fake_rules.py"
    fake.write_text(f'def check_real(ctx):\n    ctx.add("{real_id}")\n')
    found = _literal_check_ids(fake)
    assert _unregistered(found) == []


# -- dispatcher completeness: every public check_* function actually runs -----

# (module path, name of its run_* entry point, name of the dispatch list to read
# directly when the entry point loops over a list of functions instead of
# calling each by name -- rules.py does; inlinks.py and heuristics.py call
# each check_* function by name directly in the entry point's body).
DISPATCH_TARGETS: dict[pathlib.Path, tuple[str, str | None]] = {
    CORE / "rules.py": ("run_rules", "ALL_CHECKS"),
    CORE / "inlinks.py": ("run_inlinks", None),
    CORE / "heuristics.py": ("run_heuristics", None),
}

# Public check_* functions that exist but are deliberately not dispatched by
# their module's run_* entry point, with the reason. Empty today -- kept so a
# future exception has somewhere to be named instead of silently patched
# around in the gate itself.
DISPATCHER_EXCLUSIONS: dict[str, set[str]] = {}


def _public_check_functions(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("check_")
    }


def _dispatched_names(path: pathlib.Path, run_func_name: str, list_name: str | None) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    if list_name is not None:
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == list_name for t in node.targets
            ):
                names |= {e.id for e in node.value.elts if isinstance(e, ast.Name)}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == run_func_name:
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    names.add(child.func.id)
    return names


def test_every_public_check_function_is_dispatched_or_excluded():
    problems = []
    for path, (run_name, list_name) in DISPATCH_TARGETS.items():
        defined = _public_check_functions(path)
        dispatched = _dispatched_names(path, run_name, list_name)
        excluded = DISPATCHER_EXCLUSIONS.get(path.name, set())
        missing = sorted(defined - dispatched - excluded)
        if missing:
            problems.append(f"{path.name}: check_* defined but never dispatched: {missing}")
    assert not problems, "; ".join(problems)


def test_gate_flags_an_undispatched_function(tmp_path):
    """Mutation test: a check_* function left out of ALL_CHECKS must be caught."""
    fake = tmp_path / "fake_rules.py"
    fake.write_text(
        "def check_used(ctx):\n"
        "    pass\n"
        "\n"
        "\n"
        "def check_orphan(ctx):\n"
        "    pass\n"
        "\n"
        "\n"
        "ALL_CHECKS = [check_used]\n"
        "\n"
        "\n"
        "def run_rules(ctx):\n"
        "    for check in ALL_CHECKS:\n"
        "        check(ctx)\n"
    )
    defined = _public_check_functions(fake)
    dispatched = _dispatched_names(fake, "run_rules", "ALL_CHECKS")
    assert defined - dispatched == {"check_orphan"}
