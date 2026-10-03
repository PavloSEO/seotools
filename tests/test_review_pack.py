"""The software-review pack is generated and verified without a network.

The pack -- a dependency/license inventory, a release-provenance record, and a
SHA-256 manifest over those and the built distributions -- is the evidence an
internal security/legal/procurement review asks for. These tests run the whole
generate/verify loop on a synthetic checkout and synthetic distributions, so
nothing here needs a real tag, a real release, or installed credentials.
"""

from __future__ import annotations

import email.message
import io
import json
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.generate_review_pack as gp
from seohead import build_provenance

REVISION = "a" * 40
VERSION = "9.9.9"
TAG = f"v{VERSION}"
PKG_FILES = {
    "__init__.py": b"__version__ = '9.9.9'\n",
    "core.py": b"def core():\n    return True\n",
}

PYPROJECT = """\
[project]
name = "synthetic-tools"
version = "9.9.9"
dependencies = [
    "httpx>=0.27,<1",
    "synthetic-direct>=1",
    "marker-gated-pkg; python_version < '3.0'",
]

[project.optional-dependencies]
extras-demo = ["google-auth[requests]>=2", "not-installed-pkg-xyz"]
"""

DIST_METADATA = (
    b"Metadata-Version: 2.1\n"
    b"Name: synthetic-tools\n"
    b"Version: 9.9.9\n"
    b"\n"
    b"Synthetic distribution for review-pack tests.\n"
)


def _checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    return root


def _fake_distribution(
    name: str,
    version: str,
    requires: tuple[str, ...] = (),
    *,
    license_expression: str | None = None,
    license_field: str | None = None,
    classifiers: tuple[str, ...] = (),
) -> SimpleNamespace:
    metadata = email.message.Message()
    metadata["Name"] = name
    metadata["Version"] = version
    if license_expression:
        metadata["License-Expression"] = license_expression
    if license_field:
        metadata["License"] = license_field
    for classifier in classifiers:
        metadata["Classifier"] = classifier
    return SimpleNamespace(version=version, requires=list(requires), metadata=metadata)


def _stub_environment(monkeypatch, packages: dict[str, SimpleNamespace]) -> None:
    """Resolve only the named fake distributions; everything else is uninstalled."""
    registry = {gp._canonical(name): dist for name, dist in packages.items()}
    monkeypatch.setattr(gp, "_distribution", registry.get)


def _manifest_tree(root: Path, *, version: str = VERSION) -> bytes:
    """Stage ``PKG_FILES`` under ``root/seohead`` with a validating manifest."""
    for relative, body in PKG_FILES.items():
        target = root / "seohead" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    manifest = build_provenance.build_manifest(root, version=version, revision=REVISION)
    build_provenance.write_manifest(root, manifest)
    return (root / "seohead" / build_provenance.MANIFEST_FILENAME).read_bytes()


def _write_wheel(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for relative, body in members.items():
            archive.writestr(relative, body)


def _write_sdist(path: Path, top: str, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for relative, body in members.items():
            info = tarfile.TarInfo(relative)
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))


def _dist_dir(tmp_path: Path) -> Path:
    """A wheel and an sdist that both carry a validating embedded manifest."""
    dist = tmp_path / "dist"
    dist.mkdir(parents=True)

    wheel_manifest = _manifest_tree(tmp_path / "wheel-stage")
    _write_wheel(
        dist / "synthetic_tools-9.9.9-py3-none-any.whl",
        {
            **{f"seohead/{name}": body for name, body in PKG_FILES.items()},
            f"seohead/{build_provenance.MANIFEST_FILENAME}": wheel_manifest,
            "synthetic_tools-9.9.9.dist-info/METADATA": DIST_METADATA,
        },
    )

    sdist_manifest = _manifest_tree(tmp_path / "sdist-stage" / "synthetic_tools-9.9.9")
    _write_sdist(
        dist / "synthetic_tools-9.9.9.tar.gz",
        "synthetic_tools-9.9.9",
        {f"synthetic_tools-9.9.9/seohead/{name}": body for name, body in PKG_FILES.items()}
        | {f"synthetic_tools-9.9.9/seohead/{build_provenance.MANIFEST_FILENAME}": sdist_manifest}
        | {"synthetic_tools-9.9.9/PKG-INFO": DIST_METADATA},
    )
    return dist


def _generate(tmp_path: Path, monkeypatch, packages: dict | None = None) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = _checkout(tmp_path)
    dist = _dist_dir(tmp_path)
    _stub_environment(monkeypatch, packages or {})
    pack = tmp_path / "pack"
    assert (
        gp.main(
            ["generate", "--root", str(root), "--dist", str(dist), "--tag", TAG, "--out", str(pack)]
        )
        == 0
    )
    return root, pack


# --- generation ---------------------------------------------------------------


def test_generate_writes_the_three_pack_files_and_copies_dists(tmp_path, monkeypatch):
    _root, pack = _generate(tmp_path, monkeypatch)

    names = {path.name for path in pack.iterdir()}
    assert {
        gp.INVENTORY_FILENAME,
        gp.PROVENANCE_FILENAME,
        gp.SUMS_FILENAME,
        "synthetic_tools-9.9.9.tar.gz",
        "synthetic_tools-9.9.9-py3-none-any.whl",
    } == names

    provenance = json.loads((pack / gp.PROVENANCE_FILENAME).read_text())
    assert provenance["format"] == gp.PROVENANCE_FORMAT
    assert provenance["tag"] == TAG
    assert provenance["tag_matches_package_version"] is True
    assert provenance["source"]["revision"] == REVISION
    for artifact in provenance["artifacts"]:
        assert artifact["embedded_manifest"]["valid"] is True
        assert artifact["embedded_manifest"]["files"] == len(PKG_FILES)
        assert artifact["distribution_metadata"]["matches_package"] is True


def test_generate_is_deterministic_for_the_same_inputs(tmp_path, monkeypatch):
    _root_a, first = _generate(tmp_path / "a", monkeypatch)
    _root_b, second = _generate(tmp_path / "b", monkeypatch)
    for path in sorted(first.iterdir()):
        assert path.read_bytes() == (second / path.name).read_bytes(), path.name


def test_generate_refuses_a_tag_that_disagrees_with_the_version(tmp_path, monkeypatch, capsys):
    root = _checkout(tmp_path)
    dist = _dist_dir(tmp_path)
    _stub_environment(monkeypatch, {})

    assert (
        gp.main(
            [
                "generate",
                "--root",
                str(root),
                "--dist",
                str(dist),
                "--tag",
                "v8.0.0",
                "--out",
                str(tmp_path / "pack"),
            ]
        )
        == 2
    )
    assert "v8.0.0" in capsys.readouterr().out


def test_generate_records_an_artifact_without_a_manifest_honestly(tmp_path, monkeypatch):
    root = _checkout(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_wheel(
        dist / "synthetic_tools-9.9.9-py3-none-any.whl",
        {
            "seohead/__init__.py": b"x = 1\n",
            "synthetic_tools-9.9.9.dist-info/METADATA": DIST_METADATA,
        },
    )
    _stub_environment(monkeypatch, {})
    pack = tmp_path / "pack"
    assert (
        gp.main(
            ["generate", "--root", str(root), "--dist", str(dist), "--tag", TAG, "--out", str(pack)]
        )
        == 0
    )
    provenance = json.loads((pack / gp.PROVENANCE_FILENAME).read_text())
    manifest = provenance["artifacts"][0]["embedded_manifest"]
    assert manifest["present"] is False and manifest["valid"] is False
    # ... and verification refuses to bless it.
    assert gp.main(["verify", "--pack", str(pack), "--root", str(root)]) == 1


# --- resolution ----------------------------------------------------------------


def test_requested_extras_pull_their_dependencies_bare_ones_do_not(monkeypatch):
    _stub_environment(
        monkeypatch,
        {
            "google-auth": _fake_distribution(
                "google-auth",
                "2.0",
                ("cachetools>=5", "requests>=2.20; extra == 'requests'"),
            ),
            "cachetools": _fake_distribution("cachetools", "5.0"),
            "requests": _fake_distribution("requests", "2.32"),
        },
    )
    bare = {entry["name"] for entry in gp._resolve_profile(["google-auth"])["resolved"]}
    assert bare == {"google-auth", "cachetools"}
    with_extra = {
        entry["name"] for entry in gp._resolve_profile(["google-auth[requests]"])["resolved"]
    }
    assert with_extra == {"google-auth", "cachetools", "requests"}


def test_an_installed_version_that_misses_the_specifier_is_unresolved_not_resolved(monkeypatch):
    _stub_environment(monkeypatch, {"versioned": _fake_distribution("versioned", "1.0")})
    profile = gp._resolve_profile(["versioned>=2"])
    assert profile["resolved"] == []
    assert profile["unresolved"] == [
        {
            "requirement": "versioned>=2",
            "reason": "installed version 1.0 does not satisfy >=2",
        }
    ]


def test_a_childs_extra_markers_do_not_inherit_the_parents_extras(monkeypatch):
    """``root[feature] -> child`` must not satisfy ``x; extra == 'feature'`` in child's metadata."""
    _stub_environment(
        monkeypatch,
        {
            "rootpkg": _fake_distribution("rootpkg", "1.0", ("childpkg; extra == 'feature'",)),
            "childpkg": _fake_distribution("childpkg", "1.0", ("child-extra; extra == 'feature'",)),
            "child-extra": _fake_distribution("child-extra", "1.0"),
        },
    )
    resolved = {entry["name"] for entry in gp._resolve_profile(["rootpkg[feature]"])["resolved"]}
    assert resolved == {"rootpkg", "childpkg"}


def test_a_child_requirements_own_extras_still_propagate(monkeypatch):
    _stub_environment(
        monkeypatch,
        {
            "rootpkg": _fake_distribution(
                "rootpkg", "1.0", ("childpkg[feature]; extra == 'feature'",)
            ),
            "childpkg": _fake_distribution("childpkg", "1.0", ("child-extra; extra == 'feature'",)),
            "child-extra": _fake_distribution("child-extra", "1.0"),
        },
    )
    resolved = {entry["name"] for entry in gp._resolve_profile(["rootpkg[feature]"])["resolved"]}
    assert resolved == {"rootpkg", "childpkg", "child-extra"}


def test_profiles_keep_every_declared_requirement_accounted_for(tmp_path, monkeypatch):
    _stub_environment(
        monkeypatch,
        {
            "httpx": _fake_distribution("httpx", "0.28", license_expression="BSD-3-Clause"),
            "synthetic-direct": _fake_distribution("synthetic-direct", "1.2"),
            "google-auth": _fake_distribution("google-auth", "2.0"),
        },
    )
    profile = gp.build_inventory(_checkout(tmp_path))["profiles"]["extras-demo"]
    names = {entry["name"] for entry in profile["resolved"]}
    assert {"httpx", "synthetic-direct", "google-auth"} <= names
    assert {item["requirement"] for item in profile["unresolved"]} == {"not-installed-pkg-xyz"}
    assert "marker-gated-pkg; python_version < '3.0'" in profile["marker_excluded"]
    httpx = next(entry for entry in profile["resolved"] if entry["name"] == "httpx")
    assert httpx["license"] == "BSD-3-Clause" and httpx["license_source"] == "license-expression"


def test_uninstalled_unparseable_and_gated_requirements_are_named_not_dropped(monkeypatch):
    _stub_environment(monkeypatch, {})
    profile = gp._resolve_profile(
        [
            "definitely-not-installed-pkg-xyz>=1",
            "gated; python_version < '3.0'",
            "not a requirement",
        ]
    )
    assert {item["requirement"] for item in profile["unresolved"]} == {
        "definitely-not-installed-pkg-xyz>=1",
        "not a requirement",
    }
    assert profile["marker_excluded"] == ["gated; python_version < '3.0'"]
    assert profile["resolved"] == []


def test_license_precedence_is_expression_then_classifier_then_field():
    assert gp._distribution_license(
        _fake_distribution("a", "1", license_expression="MIT", license_field="long text")
    ) == ("MIT", "license-expression")
    assert gp._distribution_license(
        _fake_distribution(
            "b", "1", classifiers=("License :: OSI Approved :: Apache Software License",)
        )
    ) == ("Apache Software License", "classifier")
    assert gp._distribution_license(_fake_distribution("c", "1", license_field="BSD\nmulti")) == (
        "BSD multi",
        "license-field",
    )
    assert gp._distribution_license(_fake_distribution("d", "1")) == (None, "unavailable")


def test_the_real_checkout_produces_a_profile_per_extra():
    inventory = gp.build_inventory(Path(__file__).resolve().parents[1])
    profiles = inventory["profiles"]
    assert {"core", "mcp", "dev", "all"} <= set(profiles)
    assert "httpx>=0.27,<1" in profiles["core"]["declared_requirements"]
    assert "mcp>=1.29,<2" in profiles["mcp"]["declared_requirements"]
    core_resolved = {entry["name"].lower() for entry in profiles["core"]["resolved"]}
    core_unresolved = {item["requirement"] for item in profiles["core"]["unresolved"]}
    assert {"httpx", "pandas"} <= core_resolved or core_unresolved, (
        "the real core profile resolved nothing and recorded nothing unresolved"
    )


# --- verification ---------------------------------------------------------------


def test_verify_accepts_a_generated_pack(tmp_path, monkeypatch, capsys):
    root, pack = _generate(tmp_path, monkeypatch)
    assert gp.main(["verify", "--pack", str(pack), "--root", str(root)]) == 0
    assert "pack verified" in capsys.readouterr().out


def test_verify_detects_a_tampered_distribution(tmp_path, monkeypatch):
    root, pack = _generate(tmp_path, monkeypatch)
    wheel = pack / "synthetic_tools-9.9.9-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    problems = gp.verify_pack(pack, root)
    assert any("sha256 does not match" in problem for problem in problems)


def test_verify_detects_a_tampered_inventory_and_an_extra_file(tmp_path, monkeypatch):
    root, pack = _generate(tmp_path, monkeypatch)
    inventory = json.loads((pack / gp.INVENTORY_FILENAME).read_text())
    inventory["profiles"]["core"]["resolved"] = []
    (pack / gp.INVENTORY_FILENAME).write_text(json.dumps(inventory))
    (pack / "extra.txt").write_text("unlisted", encoding="utf-8")
    problems = gp.verify_pack(pack, root)
    assert any("sha256 does not match" in problem for problem in problems)
    assert any("extra.txt" in problem and "absent" in problem for problem in problems)


def test_verify_detects_a_tag_or_version_disagreement(tmp_path, monkeypatch):
    root, pack = _generate(tmp_path, monkeypatch)
    (root / "pyproject.toml").write_text(PYPROJECT.replace("9.9.9", "9.9.8"), encoding="utf-8")
    problems = gp.verify_pack(pack, root)
    assert any("tag" in problem for problem in problems)
    assert any("package identity" in problem for problem in problems)


def test_verify_detects_an_invalid_embedded_manifest(tmp_path, monkeypatch):
    root = _checkout(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_wheel(
        dist / "synthetic_tools-9.9.9-py3-none-any.whl",
        {
            "seohead/__init__.py": b"x = 1\n",
            f"seohead/{build_provenance.MANIFEST_FILENAME}": b"{}",
            "synthetic_tools-9.9.9.dist-info/METADATA": DIST_METADATA,
        },
    )
    _stub_environment(monkeypatch, {})
    pack = tmp_path / "pack"
    gp.main(
        ["generate", "--root", str(root), "--dist", str(dist), "--tag", TAG, "--out", str(pack)]
    )
    assert any("embedded build manifest" in problem for problem in gp.verify_pack(pack, root))


def test_verify_revalidates_the_manifest_instead_of_trusting_the_record(tmp_path, monkeypatch):
    """A wheel re-sealed after generation -- provenance digests and SHA256SUMS
    updated to match the tampered bytes -- must still fail on the broken
    embedded manifest, because verification re-extracts the artifact."""
    root, pack = _generate(tmp_path, monkeypatch)
    wheel = pack / "synthetic_tools-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    members[f"seohead/{build_provenance.MANIFEST_FILENAME}"] = b"{}"
    _write_wheel(wheel, members)

    provenance_path = pack / gp.PROVENANCE_FILENAME
    provenance = json.loads(provenance_path.read_text())
    for artifact in provenance["artifacts"]:
        if artifact["file"] == wheel.name:
            artifact["sha256"] = gp._sha256_file(wheel)
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")

    rewritten = {wheel.name, provenance_path.name}
    lines = []
    for line in (pack / gp.SUMS_FILENAME).read_text().splitlines():
        _, _, name = line.partition("  ")
        name = name.strip()
        lines.append(f"{gp._sha256_file(pack / name)}  {name}" if name in rewritten else line)
    (pack / gp.SUMS_FILENAME).write_text("\n".join(lines) + "\n")

    problems = gp.verify_pack(pack, root)
    assert any("embedded build manifest" in problem for problem in problems)


def test_verify_detects_a_distribution_built_for_another_version(tmp_path, monkeypatch):
    """A stale wheel -- a valid manifest recording a different package_version
    -- is measured honestly at generation and refused at verification."""
    root = _checkout(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    stale_manifest = _manifest_tree(tmp_path / "stale-stage", version="9.9.8")
    _write_wheel(
        dist / "synthetic_tools-9.9.9-py3-none-any.whl",
        {
            **{f"seohead/{name}": body for name, body in PKG_FILES.items()},
            f"seohead/{build_provenance.MANIFEST_FILENAME}": stale_manifest,
            "synthetic_tools-9.9.9.dist-info/METADATA": DIST_METADATA,
        },
    )
    _stub_environment(monkeypatch, {})
    pack = tmp_path / "pack"
    assert (
        gp.main(
            ["generate", "--root", str(root), "--dist", str(dist), "--tag", TAG, "--out", str(pack)]
        )
        == 0
    )
    provenance = json.loads((pack / gp.PROVENANCE_FILENAME).read_text())
    manifest = provenance["artifacts"][0]["embedded_manifest"]
    assert manifest["valid"] is True
    assert manifest["package_version"] == "9.9.8"
    assert manifest["version_matches_package"] is False

    problems = gp.verify_pack(pack, root)
    assert any("'9.9.8'" in problem for problem in problems)


def test_verify_detects_an_artifact_named_for_another_version(tmp_path, monkeypatch):
    """A wheel whose filename declares a different version must not verify
    even when its embedded manifest matches the project."""
    root = _checkout(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    manifest = _manifest_tree(tmp_path / "stage")
    _write_wheel(
        dist / "synthetic_tools-9.9.8-py3-none-any.whl",
        {
            **{f"seohead/{name}": body for name, body in PKG_FILES.items()},
            f"seohead/{build_provenance.MANIFEST_FILENAME}": manifest,
            "synthetic_tools-9.9.8.dist-info/METADATA": (
                b"Metadata-Version: 2.1\nName: synthetic-tools\nVersion: 9.9.8\n"
            ),
        },
    )
    _stub_environment(monkeypatch, {})
    pack = tmp_path / "pack"
    assert (
        gp.main(
            ["generate", "--root", str(root), "--dist", str(dist), "--tag", TAG, "--out", str(pack)]
        )
        == 0
    )
    provenance = json.loads((pack / gp.PROVENANCE_FILENAME).read_text())
    identity = provenance["artifacts"][0]["artifact_identity"]
    assert identity["filename_version"] == "9.9.8"
    assert identity["matches_package"] is False

    problems = gp.verify_pack(pack, root)
    assert any("filename declares" in problem for problem in problems)


def test_verify_detects_metadata_that_disagrees_with_filename_and_manifest(tmp_path, monkeypatch):
    """Filename and embedded manifest both say 9.9.9, but the dist-info
    METADATA an installer would read says 9.9.8: measured honestly at
    generation, refused at verification."""
    root = _checkout(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    manifest = _manifest_tree(tmp_path / "stage")
    _write_wheel(
        dist / "synthetic_tools-9.9.9-py3-none-any.whl",
        {
            **{f"seohead/{name}": body for name, body in PKG_FILES.items()},
            f"seohead/{build_provenance.MANIFEST_FILENAME}": manifest,
            "synthetic_tools-9.9.9.dist-info/METADATA": (
                b"Metadata-Version: 2.1\nName: synthetic-tools\nVersion: 9.9.8\n"
            ),
        },
    )
    _stub_environment(monkeypatch, {})
    pack = tmp_path / "pack"
    assert (
        gp.main(
            ["generate", "--root", str(root), "--dist", str(dist), "--tag", TAG, "--out", str(pack)]
        )
        == 0
    )
    provenance = json.loads((pack / gp.PROVENANCE_FILENAME).read_text())
    metadata = provenance["artifacts"][0]["distribution_metadata"]
    assert metadata["present"] is True
    assert metadata["metadata_version"] == "9.9.8"
    assert metadata["matches_package"] is False

    problems = gp.verify_pack(pack, root)
    assert any("distribution metadata" in problem for problem in problems)


def test_verify_rechecks_metadata_instead_of_trusting_the_record(tmp_path, monkeypatch):
    """A wheel re-sealed after generation -- provenance digests and SHA256SUMS
    updated to match the tampered bytes -- must still fail on the METADATA
    version, because verification re-reads the file an installer would read."""
    root, pack = _generate(tmp_path, monkeypatch)
    wheel = pack / "synthetic_tools-9.9.9-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    members["synthetic_tools-9.9.9.dist-info/METADATA"] = (
        b"Metadata-Version: 2.1\nName: synthetic-tools\nVersion: 9.9.8\n"
    )
    _write_wheel(wheel, members)

    provenance_path = pack / gp.PROVENANCE_FILENAME
    provenance = json.loads(provenance_path.read_text())
    for artifact in provenance["artifacts"]:
        if artifact["file"] == wheel.name:
            artifact["sha256"] = gp._sha256_file(wheel)
    provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")

    rewritten = {wheel.name, provenance_path.name}
    lines = []
    for line in (pack / gp.SUMS_FILENAME).read_text().splitlines():
        _, _, name = line.partition("  ")
        name = name.strip()
        lines.append(f"{gp._sha256_file(pack / name)}  {name}" if name in rewritten else line)
    (pack / gp.SUMS_FILENAME).write_text("\n".join(lines) + "\n")

    problems = gp.verify_pack(pack, root)
    assert any("distribution metadata" in problem for problem in problems)


@pytest.mark.parametrize(
    "metadata_members",
    [
        {},
        {
            "synthetic_tools-9.9.9.dist-info/METADATA": DIST_METADATA,
            "other_tools-1.0.dist-info/METADATA": DIST_METADATA,
        },
        {
            "synthetic_tools-9.9.9.dist-info/METADATA": (
                b"Metadata-Version: 2.1\nName: synthetic-tools\n"
                b"Name: synthetic-tools\nVersion: 9.9.9\n"
            )
        },
    ],
    ids=["absent", "two-dist-info-dirs", "duplicate-name-header"],
)
def test_verify_detects_absent_or_ambiguous_wheel_metadata(tmp_path, monkeypatch, metadata_members):
    """A wheel without exactly one unambiguous METADATA file must not verify:
    none at all, two dist-info directories, or duplicated header fields."""
    root = _checkout(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    manifest = _manifest_tree(tmp_path / "stage")
    _write_wheel(
        dist / "synthetic_tools-9.9.9-py3-none-any.whl",
        {
            **{f"seohead/{name}": body for name, body in PKG_FILES.items()},
            f"seohead/{build_provenance.MANIFEST_FILENAME}": manifest,
            **metadata_members,
        },
    )
    _stub_environment(monkeypatch, {})
    pack = tmp_path / "pack"
    assert (
        gp.main(
            ["generate", "--root", str(root), "--dist", str(dist), "--tag", TAG, "--out", str(pack)]
        )
        == 0
    )
    assert any("distribution metadata" in problem for problem in gp.verify_pack(pack, root))


def test_verify_detects_sdist_pkg_info_that_disagrees(tmp_path, monkeypatch):
    """PKG-INFO is the sdist's Name/Version declaration; a disagreement is
    refused even when the filename and embedded manifest match."""
    root = _checkout(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    manifest = _manifest_tree(tmp_path / "stage" / "synthetic_tools-9.9.9")
    _write_sdist(
        dist / "synthetic_tools-9.9.9.tar.gz",
        "synthetic_tools-9.9.9",
        {f"synthetic_tools-9.9.9/seohead/{name}": body for name, body in PKG_FILES.items()}
        | {f"synthetic_tools-9.9.9/seohead/{build_provenance.MANIFEST_FILENAME}": manifest}
        | {
            "synthetic_tools-9.9.9/PKG-INFO": (
                b"Metadata-Version: 2.1\nName: other-tools\nVersion: 9.9.9\n"
            )
        },
    )
    _stub_environment(monkeypatch, {})
    pack = tmp_path / "pack"
    assert (
        gp.main(
            ["generate", "--root", str(root), "--dist", str(dist), "--tag", TAG, "--out", str(pack)]
        )
        == 0
    )
    provenance = json.loads((pack / gp.PROVENANCE_FILENAME).read_text())
    metadata = provenance["artifacts"][0]["distribution_metadata"]
    assert metadata["metadata_name"] == "other-tools"
    assert metadata["matches_package"] is False

    problems = gp.verify_pack(pack, root)
    assert any("distribution metadata" in problem for problem in problems)


@pytest.mark.parametrize(
    "member",
    [
        "../escape.py",
        # Windows separators and drive/UNC heads must be refused on every host:
        # the same archive extracted on Windows would escape the target root.
        "..\\escape.py",
        "sub\\..\\evil.py",
        "C:\\outside\\victim.txt",
        "C:evil.py",
        "//server/share/x.py",
        "\\\\server\\share\\x.py",
    ],
)
def test_extract_archive_refuses_members_that_escape(tmp_path, member):
    wheel = tmp_path / "evil.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(member, b"x")
    with pytest.raises(gp.ReviewPackError, match="unsafe member path"):
        gp._extract_archive(wheel, tmp_path / "out")


# --- canary discipline ----------------------------------------------------------


def test_pack_files_and_diagnostics_carry_no_canary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SEOHEAD_REVIEW_CANARY_CREDENTIAL", "CANARY-SECRET-7f3c2a1e")
    monkeypatch.setenv("SEOHEAD_REVIEW_CANARY_CLIENT", "https://canary-client.example.invalid")
    root, pack = _generate(tmp_path, monkeypatch)
    assert gp.main(["verify", "--pack", str(pack), "--root", str(root)]) == 0
    output = capsys.readouterr().out
    for token in ("CANARY-SECRET-7f3c2a1e", "https://canary-client.example.invalid"):
        assert token not in output
        assert not any(token.encode() in path.read_bytes() for path in pack.iterdir())


def test_a_leaked_canary_fails_verification_and_names_only_the_variable(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("SEOHEAD_REVIEW_CANARY_CREDENTIAL", "CANARY-SECRET-7f3c2a1e")
    root, pack = _generate(tmp_path, monkeypatch)
    (pack / "notes.txt").write_bytes(b"leak: CANARY-SECRET-7f3c2a1e")
    assert gp.main(["verify", "--pack", str(pack), "--root", str(root)]) == 1
    output = capsys.readouterr().out
    assert "SEOHEAD_REVIEW_CANARY_CREDENTIAL" in output
    assert "CANARY-SECRET-7f3c2a1e" not in output


def test_diagnostics_are_withheld_rather_than_printing_a_canary(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SEOHEAD_REVIEW_CANARY_PATH", "CANARY-PATH-9e8d")
    root, pack = _generate(tmp_path, monkeypatch)
    named = pack.parent / "CANARY-PATH-9e8d"
    pack.rename(named)
    assert gp.main(["verify", "--pack", str(named), "--root", str(root)]) == 2
    output = capsys.readouterr()
    assert "CANARY-PATH-9e8d" not in output.out
    assert "SEOHEAD_REVIEW_CANARY_PATH" in output.err
