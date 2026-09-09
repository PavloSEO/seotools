"""Portable source-derived project catalogue contracts for #686."""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

import pytest

from seohead.projects import catalogue
from seohead.sf.core.registry import CHECKS

ROOT = Path(__file__).resolve().parents[1]


def test_packaged_catalogue_matches_the_deterministic_source_generator():
    document = catalogue.build_catalogue(ROOT)
    loaded = catalogue.load_catalogue()
    assert document["entries"] == list(loaded.values())
    assert set(loaded) == {entry["id"] for entry in document["entries"]}


def test_catalogue_covers_each_declared_source_once_and_keeps_markdown_content():
    loaded = catalogue.load_catalogue()
    assert {
        identifier.removeprefix("check:")
        for identifier in loaded
        if identifier.startswith("check:")
    } == set(CHECKS)
    workflow = sorted((ROOT / ".claude" / "skills").glob("*/SKILL.md"))
    general = sorted((ROOT / "seohead" / "skills").glob("*/SKILL.md"))
    scenarios = sorted(
        path for path in (ROOT / "docs" / "scenarios").glob("*.md") if path.name != "README.md"
    )
    assert {entry["source"] for entry in loaded.values() if entry["kind"] == "skill"} == {
        path.relative_to(ROOT).as_posix() for path in workflow + general
    }
    assert {entry["source"] for entry in loaded.values() if entry["kind"] == "scenario"} == {
        path.relative_to(ROOT).as_posix() for path in scenarios
    }
    for entry in loaded.values():
        if entry["kind"] in {"skill", "scenario"}:
            assert entry["content"] == (ROOT / entry["source"]).read_text(encoding="utf-8")
        else:
            assert set(entry) == {"id", "kind", "title", "definition_hash", "source"}
            assert entry["source"] == "runtime:CHECKS"


def test_synthetic_registry_addition_and_change_get_distinct_pending_definitions():
    checks = dict(CHECKS)
    check_id = next(iter(CHECKS))
    changed = dict(checks[check_id])
    changed["message"] = "Synthetic changed definition"
    checks[check_id] = changed
    checks["SYNTHETIC_PENDING"] = {
        "severity": "notice",
        "source": "fixture",
        "message": "Synthetic pending definition",
        "fix": "Use only in this test.",
    }
    synthetic = {
        entry["id"]: entry for entry in catalogue.build_catalogue(ROOT, checks=checks)["entries"]
    }
    current = catalogue.load_catalogue()
    assert "check:SYNTHETIC_PENDING" in synthetic
    assert "check:SYNTHETIC_PENDING" not in current
    assert (
        synthetic[f"check:{check_id}"]["definition_hash"]
        != current[f"check:{check_id}"]["definition_hash"]
    )


def test_duplicate_catalogue_ids_are_rejected_before_generation():
    entry = {
        "id": "scenario:duplicate",
        "kind": "scenario",
        "title": "Duplicate",
        "definition_hash": "0" * 64,
        "source": "docs/scenarios/duplicate.md",
        "content": "# Duplicate\n",
    }
    with pytest.raises(ValueError, match="duplicate project catalogue IDs"):
        catalogue._unique([entry, dict(entry)])


def test_packaged_loader_has_no_cli_or_server_import_dependency(monkeypatch):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "seohead.cli" or name.startswith("seohead.servers"):
            raise AssertionError(f"catalogue loader imported interface module {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "seohead.projects.catalogue", raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    reloaded = importlib.import_module("seohead.projects.catalogue")
    assert reloaded.load_catalogue()
