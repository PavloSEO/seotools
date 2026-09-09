"""The shipped example project must answer the coverage question, not defer it.

Epic #648 asks that `project status` on the `examples/` skeleton print a coverage figure
matching a hand count of the catalogue. Checklist initialization is deliberately separate
from project creation (docs/PROJECTS.md), so the skeleton was shipped without a checklist
and reported `not_initialized` -- the one project in this repository a reader can run the
documented command against answered with a state instead of a number, and no test or
document walked it.

The skeleton now ships already initialized. This gate holds that shipped file to the live
catalogue: it fails if the file disappears, if its item IDs drift from the catalogue, if
any shipped item claims a result rather than `not_run`, or if `project-status` stops
reporting counts for it.
"""

from __future__ import annotations

import json
import pathlib

from scripts.generate_project_skeleton_coverage import render
from seohead.projects.catalogue import load_catalogue
from seohead.servers.handlers import project_status

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKELETON = ROOT / "examples" / "project-skeleton"
COVERAGE = SKELETON / "coverage.json"
REGENERATE = "python scripts/generate_project_skeleton_coverage.py"


def _document() -> dict:
    assert COVERAGE.is_file(), (
        f"examples/project-skeleton/coverage.json is missing: regenerate it with `{REGENERATE}`"
    )
    return json.loads(COVERAGE.read_text(encoding="utf-8"))


def test_the_shipped_skeleton_carries_an_initialized_checklist():
    document = _document()
    assert document["format"] == "seohead.coverage.v1"
    assert document["revision"] >= 1
    assert (
        document["project_uuid"]
        == json.loads((SKELETON / "project.json").read_text(encoding="utf-8"))["project_uuid"]
    )


def test_shipped_item_ids_match_the_live_catalogue():
    assert set(_document()["items"]) == set(load_catalogue()), (
        "examples/project-skeleton/coverage.json has drifted from the packaged catalogue: "
        f"regenerate it with `{REGENERATE}`"
    )


def test_no_shipped_item_claims_a_result():
    """Nothing has been run against the synthetic site, so nothing may look measured."""
    for item_id, item in _document()["items"].items():
        assert item["records"] == [], f"{item_id} ships a result it never earned"


def test_the_shipped_file_states_no_time_but_the_projects_own_creation_time():
    """A committed example must not carry the clock of whichever machine generated it."""
    created_at = json.loads((SKELETON / "project.json").read_text(encoding="utf-8"))["created_at"]
    observed = {
        version["observed_at"]
        for item in _document()["items"].values()
        for version in item["definitions"]
    }
    assert observed == {created_at}


def test_the_shipped_file_matches_a_fresh_generation():
    assert COVERAGE.read_text(encoding="utf-8") == render(), (
        f"examples/project-skeleton/coverage.json is stale: regenerate it with `{REGENERATE}`"
    )


def test_project_status_reports_a_coverage_figure_matching_the_catalogue():
    checklist = project_status(str(SKELETON))["checklist"]
    assert checklist["state"] == "initialized", checklist.get("reason")
    catalogue = load_catalogue()
    kinds = {"check", "skill", "scenario"}
    assert checklist["counts"]["total"] == len(catalogue)
    assert checklist["counts"]["not_run"] == len(catalogue)
    assert checklist["counts"]["run"] == 0
    for kind in kinds:
        expected = sum(entry["kind"] == kind for entry in catalogue.values())
        assert checklist["by_kind"][kind]["total"] == expected
    # The hand count the epic asks for: the printed figure is the catalogue's own kinds summed.
    assert (
        sum(checklist["by_kind"][kind]["total"] for kind in kinds) == checklist["counts"]["total"]
    )
