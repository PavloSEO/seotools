"""Generate the packaged project coverage catalogue from repository sources."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "seohead" / "data" / "project_catalogue.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seohead.projects.catalogue import build_catalogue, serialise_catalogue  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail when generated content differs")
    args = parser.parse_args(argv)
    rendered = serialise_catalogue(build_catalogue(ROOT))
    output = args.output.resolve()
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != rendered:
            parser.error(f"{output} is stale; run scripts/generate_project_catalogue.py")
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
