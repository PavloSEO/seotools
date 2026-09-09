"""Build and load the portable catalogue used by project coverage.

The installed package reads only its generated JSON resource.  Source discovery is
kept in this module for the release-time generator, but is never called by
``load_catalogue``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Any

CATALOGUE_FORMAT = "seohead.project_catalogue.v1"
CATALOGUE_VERSION = 1
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SKILL_NAME = re.compile(r"^name:\s*(.+?)\s*$", re.MULTILINE)


def _definition_hash(definition: Mapping[str, Any]) -> str:
    payload = json.dumps(
        definition, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _title_from_skill(content: str, path: Path) -> str:
    if not content.startswith("---\n"):
        raise ValueError(f"skill {path} must start with YAML front matter")
    closing = content.find("\n---", 4)
    if closing == -1:
        raise ValueError(f"skill {path} has unclosed YAML front matter")
    match = _SKILL_NAME.search(content[4:closing])
    if not match or not match.group(1).strip():
        raise ValueError(f"skill {path} must declare a name")
    return match.group(1).strip().strip('"')


def _title_from_scenario(content: str, path: Path) -> str:
    for line in content.splitlines():
        if line.startswith("# ") and line[2:].strip():
            return line[2:].strip()
    raise ValueError(f"scenario {path} must have a level-one heading")


def _entry(
    identifier: str,
    kind: str,
    title: str,
    source: str,
    definition: Mapping[str, Any],
    *,
    content: str | None = None,
) -> dict[str, str]:
    entry = {
        "id": identifier,
        "kind": kind,
        "title": title,
        "definition_hash": _definition_hash(definition),
        "source": source,
    }
    if content is not None:
        entry["content"] = content
    return entry


def _unique(entries: Sequence[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for entry in entries:
        identifier = entry["id"]
        if identifier in seen:
            duplicates.add(identifier)
        seen.add(identifier)
    if duplicates:
        raise ValueError("duplicate project catalogue IDs: " + ", ".join(sorted(duplicates)))
    return sorted(entries, key=lambda entry: entry["id"])


def build_catalogue(
    source_root: str | Path,
    *,
    checks: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Derive a deterministic catalogue from a source checkout.

    ``checks`` exists for offline generator tests.  Production generation reads
    the runtime registry directly, while installed callers use ``load_catalogue``.
    """
    root = Path(source_root).resolve()
    if checks is None:
        from seohead.sf.core.registry import CHECKS

        checks = CHECKS
    entries: list[dict[str, str]] = []
    for check_id, metadata in checks.items():
        if not isinstance(check_id, str) or not isinstance(metadata, Mapping):
            raise ValueError("CHECKS must map string IDs to metadata mappings")
        title = metadata.get("message")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"CHECKS entry {check_id!r} must have a message")
        identifier = f"check:{check_id}"
        entries.append(
            _entry(
                identifier,
                "check",
                title,
                "runtime:CHECKS",
                {
                    "id": identifier,
                    "kind": "check",
                    "source": "runtime:CHECKS",
                    "metadata": dict(metadata),
                },
            )
        )

    skill_roots = (
        ("workflow", root / ".claude" / "skills"),
        ("general", root / "seohead" / "skills"),
    )
    for family, skill_root in skill_roots:
        for path in sorted(skill_root.glob("*/SKILL.md")):
            content = path.read_text(encoding="utf-8")
            name = path.parent.name
            source = path.relative_to(root).as_posix()
            identifier = f"skill:{family}/{name}"
            entries.append(
                _entry(
                    identifier,
                    "skill",
                    _title_from_skill(content, path),
                    source,
                    {"id": identifier, "kind": "skill", "source": source, "content": content},
                    content=content,
                )
            )

    scenarios = root / "docs" / "scenarios"
    for path in sorted(scenarios.glob("*.md")):
        if path.name == "README.md":
            continue
        content = path.read_text(encoding="utf-8")
        source = path.relative_to(root).as_posix()
        identifier = f"scenario:{path.stem}"
        entries.append(
            _entry(
                identifier,
                "scenario",
                _title_from_scenario(content, path),
                source,
                {"id": identifier, "kind": "scenario", "source": source, "content": content},
                content=content,
            )
        )
    return {
        "format": CATALOGUE_FORMAT,
        "version": CATALOGUE_VERSION,
        "entries": _unique(entries),
    }


def serialise_catalogue(document: Mapping[str, Any]) -> str:
    """Return the one canonical on-disk representation used by the generator."""
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _validated_entries(document: Any) -> dict[str, dict[str, str]]:
    if not isinstance(document, dict) or set(document) != {"format", "version", "entries"}:
        raise RuntimeError("packaged project catalogue has an unsupported shape")
    if document["format"] != CATALOGUE_FORMAT or document["version"] != CATALOGUE_VERSION:
        raise RuntimeError("packaged project catalogue has an unsupported format or version")
    entries = document["entries"]
    if not isinstance(entries, list):
        raise RuntimeError("packaged project catalogue entries must be a list")
    result: dict[str, dict[str, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("packaged project catalogue has an invalid entry")
        kind = entry.get("kind")
        required = {"id", "kind", "title", "definition_hash", "source"}
        if kind in {"skill", "scenario"}:
            required.add("content")
        if kind not in {"check", "skill", "scenario"} or set(entry) != required:
            raise RuntimeError("packaged project catalogue has an invalid entry shape")
        if any(type(entry[key]) is not str or not entry[key] for key in required):
            raise RuntimeError("packaged project catalogue has invalid entry values")
        identifier = entry["id"]
        prefix = {"check": "check:", "skill": "skill:", "scenario": "scenario:"}[kind]
        if not identifier.startswith(prefix) or not _HASH.fullmatch(entry["definition_hash"]):
            raise RuntimeError("packaged project catalogue has an invalid entry identifier or hash")
        if identifier in result:
            raise RuntimeError(f"packaged project catalogue has duplicate ID {identifier!r}")
        result[identifier] = dict(entry)
    return result


def load_catalogue() -> dict[str, dict[str, str]]:
    """Load the generated catalogue from package data, without a source checkout."""
    try:
        raw = files("seohead.data").joinpath("project_catalogue.json").read_text(encoding="utf-8")
        document = json.loads(raw)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise RuntimeError("packaged project catalogue is missing or invalid") from exc
    return _validated_entries(document)
