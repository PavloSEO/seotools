"""Verified producer metadata embedded by clean distribution builds.

This is provenance for an installed package, not an attestation system.  A
manifest names the Git revision that built it and hashes the Python sources it
ships, so a runtime without a source checkout can reject altered metadata or
code instead of guessing a revision from an unrelated repository.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from base64 import urlsafe_b64encode
from csv import reader
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_FILENAME = "_build_provenance.json"
MANIFEST_VERSION = 1
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")


class BuildProvenanceError(ValueError):
    """Raised when a packaged producer identity is absent or cannot be verified."""


@dataclass(frozen=True)
class PackagedProvenance:
    """Validated package identity from the embedded manifest."""

    version: str
    revision: str


def package_source_files(source_root: Path) -> tuple[str, ...]:
    """Return every packaged runtime file, relative to ``source_root``.

    The manifest intentionally covers Python, SQL, JSON, skills, and any other
    shipped package data.  It excludes only itself and bytecode caches, so a
    changed ``scan_v1.sql`` cannot validate under the producer revision that
    built a different schema.
    """
    package = source_root / "seohead"
    if not package.is_dir():
        raise BuildProvenanceError("packaged provenance has no seohead package directory")
    package_root = package.resolve()
    files: list[str] = []
    for path in sorted(package.rglob("*")):
        if path.is_symlink():
            raise BuildProvenanceError("packaged provenance refuses symlinked source paths")
        if path == _manifest_path(source_root) or "__pycache__" in path.parts:
            continue
        if not path.is_file():
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        if package_root not in path.resolve().parents:
            raise BuildProvenanceError(
                "packaged provenance refuses symlinked or escaping source files"
            )
        files.append(path.relative_to(source_root).as_posix())
    return tuple(files)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(source_root: Path, *, version: str, revision: str) -> dict[str, Any]:
    """Build validated metadata for a clean source tree or sdist staging tree."""
    if not isinstance(version, str) or not version:
        raise BuildProvenanceError("packaged provenance version must be a nonempty string")
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        raise BuildProvenanceError("packaged provenance revision must be a full lowercase Git SHA")
    files = package_source_files(source_root)
    if not files:
        raise BuildProvenanceError("packaged provenance has no runtime source files")
    return {
        "manifest_version": MANIFEST_VERSION,
        "package_version": version,
        "revision": revision,
        "files": {relative: _digest(source_root / relative) for relative in files},
    }


def _manifest_path(source_root: Path) -> Path:
    return source_root / "seohead" / MANIFEST_FILENAME


def write_manifest(source_root: Path, manifest: dict[str, Any]) -> None:
    """Write metadata only into a build or sdist staging tree."""
    _manifest_path(source_root).write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def remove_manifest(source_root: Path) -> None:
    """Remove stale metadata from a managed build or release tree only."""
    _manifest_path(source_root).unlink(missing_ok=True)


def _read_manifest(source_root: Path) -> dict[str, Any]:
    path = _manifest_path(source_root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BuildProvenanceError("packaged producer metadata is unavailable") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildProvenanceError("packaged producer metadata is unreadable") from exc
    if not isinstance(value, dict):
        raise BuildProvenanceError("packaged producer metadata must be a JSON object")
    return value


def validate_manifest(
    source_root: Path, manifest: dict[str, Any] | None = None
) -> PackagedProvenance:
    """Validate schema and every packaged Python hash without mutating runtime files."""
    value = _read_manifest(source_root) if manifest is None else manifest
    if set(value) != {"manifest_version", "package_version", "revision", "files"}:
        raise BuildProvenanceError("packaged producer metadata has an unsupported schema")
    if type(value["manifest_version"]) is not int or value["manifest_version"] != MANIFEST_VERSION:
        raise BuildProvenanceError("packaged producer metadata version is unsupported")
    if not isinstance(value["package_version"], str) or not value["package_version"]:
        raise BuildProvenanceError("packaged producer metadata has an invalid package version")
    if not isinstance(value["revision"], str) or not _REVISION.fullmatch(value["revision"]):
        raise BuildProvenanceError("packaged producer metadata has an invalid Git revision")
    files = value["files"]
    if not isinstance(files, dict) or not files:
        raise BuildProvenanceError("packaged producer metadata has no source hashes")
    expected_files = set(package_source_files(source_root))
    if set(files) != expected_files:
        raise BuildProvenanceError("packaged producer metadata source file list disagrees")
    for relative, expected_hash in files.items():
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise BuildProvenanceError("packaged producer metadata has an invalid source hash")
        if not _SHA256.fullmatch(expected_hash) or _digest(source_root / relative) != expected_hash:
            raise BuildProvenanceError(f"packaged producer source hash does not match: {relative}")
    return PackagedProvenance(version=value["package_version"], revision=value["revision"])


def _verify_installed_manifest_record(path: Path) -> None:
    """Require wheel RECORD to protect the manifest bytes without self-hashing it."""
    try:
        distribution = importlib.metadata.distribution("seohead-seotools")
    except importlib.metadata.PackageNotFoundError as exc:
        raise BuildProvenanceError(
            "installed packaged producer metadata has no wheel RECORD"
        ) from exc
    member = next(
        (
            str(item)
            for item in distribution.files or ()
            if distribution.locate_file(item).resolve() == path.resolve()
        ),
        None,
    )
    record = distribution.read_text("RECORD")
    if member is None or record is None:
        raise BuildProvenanceError(
            "installed packaged producer metadata is absent from wheel RECORD"
        )
    rows = {row[0]: row[1] for row in reader(record.splitlines()) if len(row) >= 2}
    expected = rows.get(member)
    actual = urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).decode().rstrip("=")
    if expected != f"sha256={actual}":
        raise BuildProvenanceError(
            "installed packaged producer metadata hash does not match wheel RECORD"
        )


def packaged_provenance(source_root: Path | None = None) -> PackagedProvenance:
    """Return verified installed-package provenance, never consulting Git.

    An explicit root is for build staging and tests, where a wheel ``RECORD``
    does not exist yet. Runtime uses the default and therefore verifies both the
    manifest's source hashes and the wheel's independent metadata-file hash.
    """
    root = source_root or Path(__file__).resolve().parent.parent
    provenance = validate_manifest(root)
    if source_root is None:
        _verify_installed_manifest_record(_manifest_path(root))
    return provenance
