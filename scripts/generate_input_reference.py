#!/usr/bin/env python3
"""Write or verify the generated command-input reference."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seohead.input_contracts import render_markdown  # noqa: E402

TARGET = ROOT / "docs" / "INPUTS.md"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="fail when the checked-in document drifts"
    )
    args = parser.parse_args()
    rendered = render_markdown()
    current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
    if args.check:
        if current != rendered:
            raise SystemExit("docs/INPUTS.md is stale: run scripts/generate_input_reference.py")
        return 0
    TARGET.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
