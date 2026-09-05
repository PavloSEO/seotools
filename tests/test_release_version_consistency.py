"""Release metadata must not drift: pyproject, runtime, and citation versions agree.

The release workflow (.github/workflows/release.yml) compares the git tag only
against ``pyproject.toml``. ``seohead.__version__`` and ``CITATION.cff`` each
carry a separate, hand-written copy of the same version string, and neither is
checked against the tag or against each other. A release can therefore pass
the tag check while publishing a stale CLI/report version or stale citation
metadata.

This is a narrow, offline, source-derived gate: it reads the three committed
declarations directly and asserts they agree, then proves the comparison
itself would catch a real mismatch rather than vacuously passing today because
the three numbers happen to already match.
"""

from __future__ import annotations

import re
from pathlib import Path

import seohead

ROOT = Path(__file__).resolve().parent.parent

# Anchored to the start of a line so it cannot match `python_version = "3.10"`
# ([tool.mypy]) or similarly-suffixed keys elsewhere in the file.
_PYPROJECT_VERSION_RE = re.compile(r'(?m)^version\s*=\s*"([^"]+)"\s*$')
_CITATION_VERSION_RE = re.compile(r"(?m)^version:\s*(\S+)\s*$")


def _pyproject_version(text: str) -> str:
    match = _PYPROJECT_VERSION_RE.search(text)
    assert match, 'pyproject.toml has no top-level `version = "..."` line'
    return match.group(1)


def _citation_version(text: str) -> str:
    match = _CITATION_VERSION_RE.search(text)
    assert match, "CITATION.cff has no `version: ...` line"
    return match.group(1)


def test_pyproject_runtime_and_citation_versions_agree():
    pyproject_version = _pyproject_version((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    citation_version = _citation_version((ROOT / "CITATION.cff").read_text(encoding="utf-8"))
    runtime_version = seohead.__version__
    assert pyproject_version == runtime_version == citation_version, (
        f"release metadata drift: pyproject.toml={pyproject_version!r}, "
        f"seohead.__version__={runtime_version!r}, CITATION.cff={citation_version!r}"
    )


def test_mismatched_fixture_is_caught_by_the_same_comparison():
    """Negative control: a deliberately mismatched trio must fail the check above.

    Without this, an equality assertion that happens to pass today (all three
    already say 3.0.0) would prove nothing about whether the gate can ever
    fire -- it could be comparing a value against itself by accident.
    """
    pyproject_text = 'version = "3.0.0"\n'
    citation_text = "version: 3.0.1\n"
    stale_runtime_version = "3.0.0"

    pyproject_version = _pyproject_version(pyproject_text)
    citation_version = _citation_version(citation_text)

    assert not (pyproject_version == stale_runtime_version == citation_version), (
        "the mismatched fixture (pyproject 3.0.0, citation 3.0.1) must not read as consistent"
    )
    # And a genuinely matched fixture must still read as consistent, so the
    # negative control above is failing for the right reason (the citation
    # value), not because the extraction helpers are broken.
    matching_citation_text = "version: 3.0.0\n"
    assert pyproject_version == stale_runtime_version == _citation_version(matching_citation_text)
