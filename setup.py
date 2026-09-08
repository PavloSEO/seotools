"""Setuptools hooks that stage, never modify, packaged producer provenance."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from setuptools import setup  # noqa: E402
from setuptools.command.build_py import build_py as _build_py  # noqa: E402
from setuptools.command.sdist import sdist as _sdist  # noqa: E402

from seohead.build_provenance import (  # noqa: E402
    BuildProvenanceError,
    build_manifest,
    remove_manifest,
    validate_manifest,
    write_manifest,
)

_SHA = "0123456789abcdef"


def _checkout_revision(source_root: Path) -> tuple[bool, str | None]:
    """Return ``(is_source_checkout, clean_revision)`` without borrowing outer Git."""
    try:
        top = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if top.returncode or Path(top.stdout.strip()).resolve() != source_root:
        return False, None
    status = subprocess.run(
        ["git", "-C", str(source_root), "status", "--porcelain"],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    revision = subprocess.run(
        ["git", "-C", str(source_root), "rev-parse", "HEAD"],
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )
    candidate = revision.stdout.strip()
    if status.returncode or revision.returncode or status.stdout or len(candidate) != 40:
        return True, None
    if any(character not in _SHA for character in candidate):
        return True, None
    return True, candidate


SOURCE_CHECKOUT, SOURCE_REVISION = _checkout_revision(ROOT)


def _source_checkout_revision(source_root: Path) -> tuple[bool, str | None]:
    """Keep the source identity observed before setuptools creates egg-info."""
    if source_root.resolve() == ROOT:
        return SOURCE_CHECKOUT, SOURCE_REVISION
    return _checkout_revision(source_root)


def _staged_manifest(source_root: Path, version: str, revision: str | None = None) -> dict | None:
    """Reuse sdist metadata, otherwise emit only clean-checkout provenance."""
    existing = source_root / "seohead" / "_build_provenance.json"
    if revision is not None:
        return build_manifest(source_root, version=version, revision=revision)
    is_checkout, clean_revision = _source_checkout_revision(source_root)
    if is_checkout:
        return (
            build_manifest(source_root, version=version, revision=clean_revision)
            if clean_revision is not None
            else None
        )
    if existing.is_file():
        try:
            validate_manifest(source_root)
            return json.loads(existing.read_text(encoding="utf-8"))
        except BuildProvenanceError as exc:
            raise RuntimeError(f"cannot reuse packaged producer metadata: {exc}") from exc
    return None


class build_py(_build_py):
    """Copy generated metadata into build_lib after regular package staging."""

    def run(self) -> None:
        super().run()
        target_root = Path(self.build_lib)
        is_checkout, revision = SOURCE_CHECKOUT, SOURCE_REVISION
        if is_checkout:
            manifest = (
                build_manifest(
                    target_root, version=self.distribution.get_version(), revision=revision
                )
                if revision is not None
                else None
            )
        else:
            source_manifest = _staged_manifest(ROOT, self.distribution.get_version())
            manifest = (
                build_manifest(
                    target_root,
                    version=self.distribution.get_version(),
                    revision=source_manifest["revision"],
                )
                if source_manifest is not None
                else None
            )
        self._provenance_output = None
        if manifest is not None:
            write_manifest(target_root, manifest)
            self._provenance_output = str(target_root / "seohead" / "_build_provenance.json")
        else:
            remove_manifest(target_root)

    def get_outputs(self, include_bytecode: bool = True) -> list[str]:
        outputs = super().get_outputs(include_bytecode)
        output = getattr(self, "_provenance_output", None)
        return outputs + ([output] if output else [])


class sdist(_sdist):
    """Place clean-checkout metadata only in the release tree before archiving."""

    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, files)
        is_checkout, revision = SOURCE_CHECKOUT, SOURCE_REVISION
        staged = Path(base_dir)
        if is_checkout and revision is None:
            remove_manifest(staged)
            return
        if is_checkout:
            write_manifest(
                staged,
                _staged_manifest(staged, self.distribution.get_version(), revision=revision) or {},
            )
        elif (staged / "seohead" / "_build_provenance.json").is_file():
            validate_manifest(staged)


setup(cmdclass={"build_py": build_py, "sdist": sdist})
