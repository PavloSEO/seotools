"""Generate the shipped example project checklist from the packaged catalogue."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKELETON = ROOT / "examples" / "project-skeleton"
OUTPUT = SKELETON / "coverage.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seohead.projects.coverage import initialize_coverage  # noqa: E402


def render(skeleton: Path = SKELETON) -> str:
    """Initialize a throwaway copy of the skeleton and return its normalized coverage document.

    Initialization runs on a copy so a ``--check`` run cannot write into the shipped project,
    and so a stale committed file is compared against a first initialization rather than
    against a reconciliation of itself.
    """
    project = json.loads((skeleton / "project.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as workspace:
        staged = Path(workspace) / "project-skeleton"
        staged.mkdir()
        for name in ("project.json", "log.md"):
            shutil.copyfile(skeleton / name, staged / name)
        for name in ("scans", "reports"):
            (staged / name).mkdir()
        initialize_coverage(staged)
        document = json.loads((staged / "coverage.json").read_text(encoding="utf-8"))
    # Initialization stamps each definition with the wall clock, which would make the committed
    # file differ on every regeneration and carry the generating machine's time. The project's
    # own synthetic creation time is the only observation time this example can honestly state.
    for item in document["items"].values():
        for version in item["definitions"]:
            version["observed_at"] = project["created_at"]
    return json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail when generated content differs")
    args = parser.parse_args(argv)
    rendered = render()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text(encoding="utf-8") != rendered:
            parser.error(f"{OUTPUT} is stale; run scripts/generate_project_skeleton_coverage.py")
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
