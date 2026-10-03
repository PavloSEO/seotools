#!/usr/bin/env python3
"""Generate or verify the software-review evidence pack for a tagged build.

    python scripts/generate_review_pack.py generate --dist dist --tag v3.0.0 --out review-pack
    python scripts/generate_review_pack.py verify --pack review-pack

The pack is what an internal security, legal, or procurement reviewer asks for: a
resolved dependency/license inventory for every supported install profile, a
release-level provenance record tying the built distributions to a tag and a
source revision, and a SHA-256 manifest covering all of it. It is produced from a
disposable tag value in CI or a synthetic checkout -- it never pushes a tag,
creates a release, or touches a network.

Three boundaries the reader should not blur:

* the wheel already carries ``seohead/_build_provenance.json``, an embedded
  source-hash manifest validated at build and runtime (``seohead/build_provenance.py``);
  the pack's provenance record is release-level -- it names the tag, the
  distributions, and what that embedded manifest proved -- and is not a
  cryptographic attestation either;
* the inventory records what was declared in ``pyproject.toml`` and what was
  resolved and installed in the environment that generated the pack. A
  requirement that is not installed here is recorded as unresolved, not
  guessed or dropped;
* diagnostics name files, counts, and any environment variable whose canary
  value leaked -- never the value itself.
"""

from __future__ import annotations

import argparse
import contextlib
import email
import hashlib
import json
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import zipfile
from importlib import metadata as importlib_metadata
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    # Python 3.10 (the declared floor) has no tomllib; tomli is already pulled in
    # there by the dev extra's `build`, which every documented generator env has.
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]

INVENTORY_FILENAME = "dependency-inventory.json"
PROVENANCE_FILENAME = "release-provenance.json"
SUMS_FILENAME = "SHA256SUMS.txt"

INVENTORY_FORMAT = "seohead.review-inventory.v1"
PROVENANCE_FORMAT = "seohead.review-provenance.v1"
FORMAT_VERSION = 1

# A reviewer or CI run may plant a sentinel value in any variable with this
# prefix. The pack's files and diagnostics must never carry that value: if one
# appears, generation and verification both fail, naming the variable that
# leaked -- never the token. This is how "diagnostics omit credentials and
# client data" stays a tested property rather than a claim.
CANARY_ENV_PREFIX = "SEOHEAD_REVIEW_CANARY_"


class ReviewPackError(ValueError):
    """The pack cannot be generated or fails verification."""


# --- project metadata ---------------------------------------------------------


def _project(root: Path) -> dict[str, Any]:
    path = root / "pyproject.toml"
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8"))["project"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError) as exc:
        raise ReviewPackError(f"{path} is unreadable or has no [project] table: {exc}") from exc
    if not isinstance(project.get("name"), str) or not isinstance(project.get("version"), str):
        raise ReviewPackError(f"{path} must declare project name and version")
    return project


def _declared_profiles(project: dict[str, Any]) -> dict[str, list[str]]:
    """Declared requirement strings per install profile, verbatim from pyproject.

    ``core`` is what every install gets; each optional-dependency group is its
    own profile because a reviewer asks what ``pip install .[mcp]`` pulls, and
    the answer is core plus that group.
    """
    dependencies = project.get("dependencies") or []
    extras = project.get("optional-dependencies") or {}
    profiles: dict[str, list[str]] = {"core": [str(item) for item in dependencies]}
    for name in sorted(extras):
        profiles[name] = [str(item) for item in dependencies] + [str(item) for item in extras[name]]
    return profiles


# --- resolved dependency inventory --------------------------------------------


def _distribution(name: str):
    """The installed distribution for ``name``, or ``None`` when absent."""
    try:
        return importlib_metadata.distribution(name)
    except importlib_metadata.PackageNotFoundError:
        return None


def _distribution_license(dist) -> tuple[str | None, str]:
    """Best license statement for one installed distribution, and where it came from.

    PEP 639 ``License-Expression`` wins, then a ``License ::`` classifier tail,
    then the free-form legacy ``License`` field. ``unavailable`` is honest: the
    record stays, only the license string is missing.
    """
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression, "license-expression"
    for classifier in meta.get_all("Classifier") or []:
        if not classifier.startswith("License ::"):
            continue
        tail = classifier.split("::")[-1].strip()
        if tail and tail != "OSI Approved":
            return tail, "classifier"
    legacy = (meta.get("License") or "").strip()
    if legacy:
        return " ".join(legacy.split())[:200], "license-field"
    return None, "unavailable"


def _canonical(name: str) -> str:
    from packaging.utils import canonicalize_name

    return str(canonicalize_name(name))


def _marker_applies(marker, extras: frozenset[str]) -> bool:
    """Whether a requirement marker can hold in this environment.

    ``extra`` markers are tried against the base environment and each extra the
    parent requirement actually requested, so ``google-auth[requests]`` reaches
    ``requests`` while a bare ``google-auth`` does not.
    """
    if marker is None:
        return True
    if marker.evaluate():
        return True
    return any(marker.evaluate({"extra": extra}) for extra in extras)


def _resolve_profile(declared: list[str]) -> dict[str, Any]:
    """Resolve declared requirements against the installed environment.

    Output keys are deliberately three disjoint lists: ``resolved`` records what
    was measured, ``marker_excluded`` records declared requirements whose
    environment marker does not apply here, and ``unresolved`` records declared
    requirements (or dependencies of them) that are not installed in the
    generating environment. Nothing unresolved is presented as resolved.
    """
    from packaging.requirements import Requirement
    from packaging.version import InvalidVersion

    resolved: dict[str, dict[str, Any]] = {}
    unresolved: list[dict[str, str]] = []
    marker_excluded: list[str] = []
    seen: set[tuple[str, frozenset[str]]] = set()
    queue: list[tuple[str, frozenset[str]]] = [(item, frozenset()) for item in declared]
    while queue:
        raw, inherited_extras = queue.pop()
        try:
            requirement = Requirement(raw)
        except Exception:
            unresolved.append({"requirement": raw, "reason": "requirement could not be parsed"})
            continue
        name = _canonical(requirement.name)
        if not _marker_applies(requirement.marker, inherited_extras):
            if raw in declared:
                marker_excluded.append(raw)
            continue
        requested = frozenset(_canonical(extra) for extra in requirement.extras)
        key = (name, requested)
        if key in seen:
            continue
        seen.add(key)
        dist = _distribution(name)
        if dist is None:
            unresolved.append(
                {"requirement": raw, "reason": "not installed in the generating environment"}
            )
            continue
        try:
            satisfied = requirement.specifier.contains(dist.version, prereleases=True)
        except InvalidVersion:
            satisfied = False
        if not satisfied:
            unresolved.append(
                {
                    "requirement": raw,
                    "reason": f"installed version {dist.version} does not satisfy "
                    f"{requirement.specifier}",
                }
            )
            continue
        license_text, license_source = _distribution_license(dist)
        resolved[name] = {
            "name": dist.metadata.get("Name") or requirement.name,
            "version": dist.version,
            "license": license_text,
            "license_source": license_source,
        }
        # An ``extra`` marker inside dist's own Requires-Dist refers to the
        # extras requested OF dist, never to the extras the parent requested:
        # ``root[feature]`` pulling plain ``child`` must not satisfy
        # ``child-extra; extra == 'feature'`` inside child's metadata.
        for dep_raw in dist.requires or []:
            queue.append((str(dep_raw), requested))
    return {
        "declared_requirements": declared,
        "resolved": [resolved[name] for name in sorted(resolved)],
        "marker_excluded": sorted(set(marker_excluded)),
        "unresolved": sorted(unresolved, key=lambda item: item["requirement"]),
    }


def build_inventory(root: Path) -> dict[str, Any]:
    """The dependency/license inventory for every install profile of ``root``."""
    project = _project(root)
    profiles = {
        name: _resolve_profile(declared) for name, declared in _declared_profiles(project).items()
    }
    return {
        "format": INVENTORY_FORMAT,
        "format_version": FORMAT_VERSION,
        "package": {"name": project["name"], "version": project["version"]},
        "generator": {
            "script": "scripts/generate_review_pack.py",
            "python": platform.python_version(),
            "platform": platform.system(),
        },
        "profiles": profiles,
    }


# --- release provenance --------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_kind(name: str) -> str:
    if name.endswith(".whl"):
        return "wheel"
    if name.endswith((".tar.gz", ".tgz")):
        return "sdist"
    return "other"


def _artifact_identity(filename: str, project: dict[str, Any]) -> dict[str, Any]:
    """The package name/version a wheel or sdist filename declares.

    A stale or mixed distribution directory can carry an artifact built for a
    different package or version. The filename's declared identity is measured
    and compared with ``pyproject.toml`` so verification judges recorded facts
    rather than trusting that every file in ``dist`` belongs to this release.
    """
    from packaging.utils import parse_sdist_filename, parse_wheel_filename
    from packaging.version import Version

    record: dict[str, Any] = {"matches_package": False}
    try:
        if filename.endswith(".whl"):
            name, version, *_ = parse_wheel_filename(filename)
        else:
            name, version = parse_sdist_filename(filename)
    except ValueError:
        record["error"] = "filename does not declare a parseable package name/version"
        return record
    record["filename_name"] = str(name)
    record["filename_version"] = str(version)
    with contextlib.suppress(ValueError):
        record["matches_package"] = str(name) == _canonical(project["name"]) and version == Version(
            project["version"]
        )
    return record


def _safe_member(relative: str) -> bool:
    """Whether an archive member path stays inside its extraction root.

    ``\\`` is a path separator on Windows, so it is normalized before the
    traversal check: ``..\\escape.py`` must be rejected even on a POSIX host,
    because the same archive extracted on Windows would escape. A ``C:``-style
    drive head or a leading ``//`` are absolute references there as well, even
    though POSIX reads them as relative names.
    """
    candidate = PurePosixPath(relative.replace("\\", "/"))
    if not candidate.parts or candidate.is_absolute() or ".." in candidate.parts:
        return False
    return ":" not in candidate.parts[0]


def _extract_archive(artifact: Path, destination: Path) -> Path:
    """Extract ``artifact`` under ``destination``; return the packaged source root.

    Wheels lay ``seohead/`` at the archive root; sdists nest it under one
    ``<name>-<version>/`` directory. Members are read and written by hand --
    never through ``extract``/``extractall`` -- so only regular files under
    safe relative paths land on disk, on every supported Python version.
    """
    members: list[tuple[str, bytes]] = []
    if zipfile.is_zipfile(artifact):
        with zipfile.ZipFile(artifact) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if not _safe_member(info.filename):
                    raise ReviewPackError(f"{artifact.name}: unsafe member path {info.filename!r}")
                members.append((info.filename, archive.read(info)))
    elif tarfile.is_tarfile(artifact):
        with tarfile.open(artifact) as archive:
            for info in archive.getmembers():
                if not info.isfile():
                    continue
                if not _safe_member(info.name):
                    raise ReviewPackError(f"{artifact.name}: unsafe member path {info.name!r}")
                body = archive.extractfile(info)
                members.append((info.name, body.read() if body else b""))
    else:
        raise ReviewPackError(f"{artifact.name} is neither a zip nor a tar archive")
    for relative, body in members:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

    if (destination / "seohead").is_dir():
        return destination
    roots = [
        path for path in destination.iterdir() if path.is_dir() and (path / "seohead").is_dir()
    ]
    if len(roots) == 1:
        return roots[0]
    raise ReviewPackError(f"{artifact.name} does not carry a single packaged seohead source tree")


def _manifest_record(source_root: Path) -> dict[str, Any]:
    """Validate the packaged build manifest inside an extracted artifact."""
    from seohead.build_provenance import MANIFEST_FILENAME, BuildProvenanceError, validate_manifest

    record: dict[str, Any] = {"present": False, "valid": False}
    manifest_path = source_root / "seohead" / MANIFEST_FILENAME
    if not manifest_path.is_file():
        record["error"] = f"no embedded {MANIFEST_FILENAME}"
        return record
    record["present"] = True
    try:
        provenance = validate_manifest(source_root)
        files = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    except (BuildProvenanceError, OSError, ValueError, KeyError) as exc:
        record["error"] = str(exc)
        return record
    record.update(
        valid=True,
        package_version=provenance.version,
        revision=provenance.revision,
        files=len(files),
    )
    return record


def _metadata_record(destination: Path, kind: str, project: dict[str, Any]) -> dict[str, Any]:
    """Measured Name/Version from the artifact's own distribution metadata.

    A wheel carries exactly one ``<name>-<version>.dist-info/METADATA`` at the
    archive root; an sdist carries exactly one ``PKG-INFO`` in its top-level
    directory (or at the archive root in a flat layout). These are the fields
    an installer actually reads, so an artifact whose filename and embedded
    manifest both agree still fails when its ``METADATA`` says otherwise.
    ``matches_package`` is true only for one unambiguous file whose ``Name``
    and ``Version`` equal ``pyproject.toml`` after normalization.
    """
    from packaging.version import InvalidVersion, Version

    label = "*.dist-info/METADATA" if kind == "wheel" else "PKG-INFO"
    record: dict[str, Any] = {"present": False, "matches_package": False}
    if kind == "wheel":
        candidates = sorted(
            path for path in destination.glob("*.dist-info/METADATA") if path.is_file()
        )
    else:
        candidates = sorted(path for path in destination.glob("*/PKG-INFO") if path.is_file())
        flat = destination / "PKG-INFO"
        if flat.is_file():
            candidates.append(flat)
    if len(candidates) != 1:
        record["error"] = (
            f"archive carries {len(candidates)} {label} file(s); exactly one is expected"
        )
        return record
    metadata_file = candidates[0]
    try:
        message = email.message_from_bytes(metadata_file.read_bytes())
    except (OSError, ValueError) as exc:
        record["error"] = f"{metadata_file.name} is unreadable: {exc}"
        return record
    names = [value.strip() for value in message.get_all("Name") or ()]
    versions = [value.strip() for value in message.get_all("Version") or ()]
    if len(names) != 1 or len(versions) != 1:
        record["error"] = f"{metadata_file.name} must declare exactly one Name and one Version"
        return record
    record.update(
        present=True,
        file=str(metadata_file.relative_to(destination)),
        metadata_name=names[0],
        metadata_version=versions[0],
    )
    try:
        record["matches_package"] = _canonical(names[0]) == _canonical(project["name"]) and Version(
            versions[0]
        ) == Version(project["version"])
    except InvalidVersion:
        record["error"] = f"unparseable metadata version {versions[0]!r}"
    return record


def _inspect_artifact(
    artifact: Path, workdir: Path, kind: str, project: dict[str, Any]
) -> dict[str, Any]:
    """Extract one artifact once and measure both in-archive records."""
    try:
        source_root = _extract_archive(artifact, workdir)
    except (ReviewPackError, OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
        error = f"archive could not be inspected: {exc}"
        return {
            "embedded_manifest": {"present": False, "valid": False, "error": error},
            "distribution_metadata": {
                "present": False,
                "matches_package": False,
                "error": error,
            },
        }
    return {
        "embedded_manifest": _manifest_record(source_root),
        "distribution_metadata": _metadata_record(workdir, kind, project),
    }


def build_provenance_document(root: Path, dist_dir: Path, tag: str) -> dict[str, Any]:
    """The release-level provenance record for the distributions in ``dist_dir``.

    This complements, and never replaces, the embedded manifest: it names the
    tag and the artifact digests, and records what the embedded manifest proved
    about each artifact's packaged source -- or that no manifest was present.
    """
    project = _project(root)
    version = project["version"]
    if tag != f"v{version}":
        raise ReviewPackError(
            f"tag {tag!r} does not match package version v{version} in {root / 'pyproject.toml'}"
        )
    files = sorted(path for path in dist_dir.iterdir() if path.is_file())
    if not files:
        raise ReviewPackError(f"{dist_dir} contains no distribution files")
    artifacts: list[dict[str, Any]] = []
    revisions: set[str] = set()
    for artifact in files:
        entry: dict[str, Any] = {
            "file": artifact.name,
            "kind": _artifact_kind(artifact.name),
            "bytes": artifact.stat().st_size,
            "sha256": _sha256_file(artifact),
        }
        if entry["kind"] in {"wheel", "sdist"}:
            entry["artifact_identity"] = _artifact_identity(artifact.name, project)
            with tempfile.TemporaryDirectory() as work:
                inspection = _inspect_artifact(artifact, Path(work), entry["kind"], project)
            record = inspection["embedded_manifest"]
            if record.get("valid"):
                record["version_matches_package"] = record["package_version"] == version
            entry["embedded_manifest"] = record
            entry["distribution_metadata"] = inspection["distribution_metadata"]
            if record.get("valid") and isinstance(record.get("revision"), str):
                revisions.add(record["revision"])
        artifacts.append(entry)
    return {
        "format": PROVENANCE_FORMAT,
        "format_version": FORMAT_VERSION,
        "tag": tag,
        "package": {"name": project["name"], "version": version},
        "tag_matches_package_version": True,
        "source": {
            "revision": sorted(revisions)[0] if len(revisions) == 1 else None,
            "revision_source": "embedded build manifest inside the distribution",
            "note": (
                "release-level record; the per-file source hashes live inside each "
                "distribution's embedded manifest, not here"
            ),
        },
        "artifacts": artifacts,
        "generator": {
            "script": "scripts/generate_review_pack.py",
            "python": platform.python_version(),
            "platform": platform.system(),
        },
    }


# --- pack assembly -------------------------------------------------------------


def write_pack(root: Path, dist_dir: Path, out_dir: Path, tag: str) -> dict[str, Any]:
    """Copy the distributions and write the three pack files; return a summary.

    Layout is flat on purpose: a reviewer who downloads the files into one
    directory can run ``sha256sum -c SHA256SUMS.txt`` without recreating a tree.
    """
    provenance = build_provenance_document(root, dist_dir, tag)
    inventory = build_inventory(root)
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for entry in provenance["artifacts"]:
        target = out_dir / entry["file"]
        shutil.copyfile(dist_dir / entry["file"], target)
        copied.append(entry["file"])
    (out_dir / INVENTORY_FILENAME).write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / PROVENANCE_FILENAME).write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    members = sorted(
        path.name for path in out_dir.iterdir() if path.is_file() and path.name != SUMS_FILENAME
    )
    lines = [f"{_sha256_file(out_dir / name)}  {name}" for name in members]
    (out_dir / SUMS_FILENAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"pack": out_dir, "files": [*members, SUMS_FILENAME], "provenance": provenance}


# --- canary scan and verification ----------------------------------------------


def canary_tokens(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Planted sentinel values, keyed by the variable that supplied each."""
    source = os.environ if environ is None else environ
    return {
        name: value
        for name, value in source.items()
        if name.startswith(CANARY_ENV_PREFIX) and value
    }


def _canary_problems(pack_dir: Path, tokens: dict[str, str]) -> list[str]:
    problems = []
    for path in sorted(pack_dir.iterdir()):
        if not path.is_file():
            continue
        content = path.read_bytes()
        for name, token in tokens.items():
            if token.encode() in content:
                problems.append(f"{path.name} carries a sentinel value supplied via ${name}")
    return problems


def verify_pack(pack_dir: Path, root: Path) -> list[str]:
    """Every problem with the pack, as text. An empty list means it verified.

    Order matters: consistency problems (format, tag, version) are cheap and
    name the disagreement, integrity problems (checksums, embedded manifests)
    prove the bytes, and the canary scan runs last so it sees every file the
    earlier steps already judged.
    """
    problems: list[str] = []
    project = _project(root)
    try:
        provenance = json.loads((pack_dir / PROVENANCE_FILENAME).read_text(encoding="utf-8"))
        inventory = json.loads((pack_dir / INVENTORY_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"pack files are missing or unreadable: {exc}"]

    if not isinstance(provenance, dict) or provenance.get("format") != PROVENANCE_FORMAT:
        problems.append(f"{PROVENANCE_FILENAME} does not carry format {PROVENANCE_FORMAT}")
    else:
        expected_tag = f"v{project['version']}"
        if provenance.get("tag") != expected_tag:
            problems.append(f"provenance tag {provenance.get('tag')!r} is not {expected_tag!r}")
        if provenance.get("tag_matches_package_version") is not True:
            problems.append("provenance does not record tag/version consistency")
        package = provenance.get("package") or {}
        if package.get("name") != project["name"] or package.get("version") != project["version"]:
            problems.append("provenance package identity disagrees with pyproject.toml")
        for artifact in provenance.get("artifacts") or []:
            kind = artifact.get("kind")
            if kind not in {"wheel", "sdist"}:
                continue
            name = artifact.get("file")
            if isinstance(name, str):
                # Re-derived from the filename under review, not the
                # recorded artifact_identity, so a pack cannot bless a
                # stale or foreign distribution by editing the document.
                identity = _artifact_identity(name, project)
                if not identity.get("matches_package"):
                    detail = identity.get("error") or (
                        f"filename declares {identity['filename_name']} "
                        f"{identity['filename_version']}"
                    )
                    problems.append(
                        f"{name}: {detail}; expected {project['name']} {project['version']}"
                    )
            manifest = artifact.get("embedded_manifest") or {}
            if not manifest.get("present") or not manifest.get("valid"):
                problems.append(f"{name}: embedded build manifest absent or invalid")
            elif manifest.get("package_version") != project["version"]:
                problems.append(
                    f"{name}: embedded build manifest records version "
                    f"{manifest.get('package_version')!r}, not {project['version']!r}"
                )
            metadata = artifact.get("distribution_metadata") or {}
            if not metadata.get("present"):
                problems.append(f"{name}: distribution metadata absent, unreadable, or ambiguous")
            elif not metadata.get("matches_package"):
                problems.append(
                    f"{name}: distribution metadata declares "
                    f"{metadata.get('metadata_name')} {metadata.get('metadata_version')}, "
                    f"not {project['name']} {project['version']}"
                )
            recorded_clean = (
                manifest.get("valid")
                and manifest.get("package_version") == project["version"]
                and metadata.get("matches_package")
            )
            if recorded_clean and isinstance(name, str) and (pack_dir / name).is_file():
                # The recorded flags came from generation; verification
                # re-extracts the artifact and re-derives both records rather
                # than trusting the document under review.
                with tempfile.TemporaryDirectory() as work:
                    actual = _inspect_artifact(pack_dir / name, Path(work), kind, project)
                actual_manifest = actual["embedded_manifest"]
                if not actual_manifest.get("valid"):
                    problems.append(
                        f"{name}: embedded build manifest does not re-validate "
                        f"({actual_manifest.get('error', 'invalid')})"
                    )
                elif actual_manifest.get("package_version") != project["version"]:
                    problems.append(
                        f"{name}: embedded build manifest in the artifact reports "
                        f"version {actual_manifest['package_version']!r}, "
                        f"not {project['version']!r}"
                    )
                actual_metadata = actual["distribution_metadata"]
                if not actual_metadata.get("matches_package"):
                    detail = actual_metadata.get("error") or (
                        f"declares {actual_metadata.get('metadata_name')} "
                        f"{actual_metadata.get('metadata_version')}"
                    )
                    problems.append(
                        f"{name}: distribution metadata in the artifact does not "
                        f"match {project['name']} {project['version']} ({detail})"
                    )
            # A missing artifact file is already named by the sums check below.

    if not isinstance(inventory, dict) or inventory.get("format") != INVENTORY_FORMAT:
        problems.append(f"{INVENTORY_FILENAME} does not carry format {INVENTORY_FORMAT}")
    else:
        package = inventory.get("package") or {}
        if package.get("name") != project["name"] or package.get("version") != project["version"]:
            problems.append("inventory package identity disagrees with pyproject.toml")
        profiles = inventory.get("profiles")
        if not isinstance(profiles, dict) or "core" not in profiles:
            problems.append("inventory has no 'core' profile")
        else:
            for name, profile in profiles.items():
                for key in (
                    "declared_requirements",
                    "resolved",
                    "marker_excluded",
                    "unresolved",
                ):
                    if not isinstance(profile.get(key), list):
                        problems.append(f"profile {name!r} lacks a {key} list")
                for entry in profile.get("resolved") or []:
                    if not (entry.get("name") and entry.get("version")):
                        problems.append(f"profile {name!r} has a resolved entry without identity")

    sums_path = pack_dir / SUMS_FILENAME
    if not sums_path.is_file():
        problems.append(f"{SUMS_FILENAME} is missing")
    else:
        recorded: dict[str, str] = {}
        for line in sums_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, _, name = line.partition("  ")
            if len(digest) != 64 or not name:
                problems.append(f"{SUMS_FILENAME} has a malformed line: {line!r}")
                continue
            recorded[name.strip()] = digest
        expected = {
            path.name
            for path in pack_dir.iterdir()
            if path.is_file() and path.name != SUMS_FILENAME
        }
        for missing in sorted(expected - set(recorded)):
            problems.append(f"{missing} is absent from {SUMS_FILENAME}")
        for extra in sorted(set(recorded) - expected):
            problems.append(f"{extra} is listed in {SUMS_FILENAME} but missing on disk")
        for name in sorted(expected & set(recorded)):
            if _sha256_file(pack_dir / name) != recorded[name]:
                problems.append(f"{name}: sha256 does not match {SUMS_FILENAME}")
        if isinstance(provenance, dict):
            for artifact in provenance.get("artifacts") or []:
                name, digest = artifact.get("file"), artifact.get("sha256")
                if name in recorded and recorded[name] != digest:
                    problems.append(f"{name}: provenance digest disagrees with {SUMS_FILENAME}")

    problems.extend(_canary_problems(pack_dir, canary_tokens()))
    return problems


# --- CLI ------------------------------------------------------------------------


def _emit(lines: list[str]) -> int:
    """Print diagnostics only after proving they carry no sentinel value.

    If a line ever does -- say a future change echoes an environment-derived
    value -- the output is withheld and the run fails, naming the variable that
    would have leaked rather than printing its value.
    """
    text = "\n".join(lines)
    leaks = [name for name, token in canary_tokens().items() if token in text]
    if leaks:
        print(
            "diagnostics withheld: output would carry a sentinel from " + ", ".join(sorted(leaks)),
            file=sys.stderr,
        )
        return 2
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="write the pack for built distributions")
    generate.add_argument("--dist", required=True, type=Path, help="directory of built dists")
    generate.add_argument("--tag", required=True, help="release tag, e.g. v3.0.0")
    generate.add_argument("--out", required=True, type=Path, help="output pack directory")
    generate.add_argument("--root", type=Path, default=ROOT, help="source checkout root")
    verify = commands.add_parser("verify", help="check a generated pack")
    verify.add_argument("--pack", required=True, type=Path, help="pack directory")
    verify.add_argument("--root", type=Path, default=ROOT, help="source checkout root")
    args = parser.parse_args(argv)

    try:
        if args.command == "generate":
            summary = write_pack(args.root, args.dist, args.out, args.tag)
            provenance = summary["provenance"]
            lines = [
                f"pack written to {summary['pack']}",
                f"tag {provenance['tag']} matches package version "
                f"{provenance['package']['version']}",
                f"files: {', '.join(summary['files'])}",
            ]
            for artifact in provenance["artifacts"]:
                manifest = artifact.get("embedded_manifest") or {}
                state = "unverified embedded manifest"
                if manifest.get("valid"):
                    state = f"revision {manifest['revision']}"
                    if manifest.get("version_matches_package") is False:
                        state += " (embedded version differs from package version)"
                metadata = artifact.get("distribution_metadata") or {}
                if "distribution_metadata" in artifact and not metadata.get("matches_package"):
                    detail = metadata.get("error") or (
                        f"declares {metadata.get('metadata_name')} "
                        f"{metadata.get('metadata_version')}"
                    )
                    state += f"; distribution metadata: {detail}"
                lines.append(f"{artifact['file']}: {artifact['kind']}, {state}")
            return _emit(lines)
        problems = verify_pack(args.pack, args.root)
        if problems:
            lines = [f"verification failed: {len(problems)} problem(s)"]
            lines += [f"- {problem}" for problem in problems]
            return _emit(lines) or 1
        covered = sum(
            1 for path in args.pack.iterdir() if path.is_file() and path.name != SUMS_FILENAME
        )
        return _emit(
            [
                f"pack verified: {args.pack}",
                f"tag v{_project(args.root)['version']} matches, "
                f"{covered} file(s) covered by {SUMS_FILENAME}",
            ]
        )
    except ReviewPackError as exc:
        return _emit([f"error: {exc}"]) or 2


if __name__ == "__main__":
    raise SystemExit(main())
