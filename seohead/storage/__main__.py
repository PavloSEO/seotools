"""Opt-in storage CLI, separate from existing crawl and report handlers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import ScanError, _loads, _text, import_run, open_scan, read_audit


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import and inspect scan.v1 legacy artifacts (no bodies)"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser(
        "import-run", help="import existing pages, links and audit without crawling"
    )
    importer.add_argument("source", type=Path)
    importer.add_argument("--out", type=Path, required=True)
    importer.add_argument(
        "--producer-build", required=True, help="original crawl's full Git commit SHA"
    )
    importer.add_argument(
        "--config", type=Path, help="original effective JSON manifest, never today's defaults"
    )
    inspector = commands.add_parser("inspect", help="validate and print metadata, without bodies")
    inspector.add_argument("source", type=Path)
    reporter = commands.add_parser(
        "report", help="render the saved audit from a directory or artifact"
    )
    reporter.add_argument("source", type=Path)
    reporter.add_argument("--format", choices=("json", "md", "csv", "xlsx", "docx"), default="md")
    reporter.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "import-run":
            config = _loads(_text(args.config), "--config") if args.config else None
            if args.config and not isinstance(config, dict):
                raise ScanError("--config must contain an effective-configuration JSON object")
            out = import_run(
                args.source, args.out, producer_build=args.producer_build, effective_config=config
            )
            result = {
                "ok": True,
                "path": str(out),
                "format": "scan.v1",
                "body_retention": "unavailable",
            }
        elif args.command == "inspect":
            con = open_scan(args.source)
            try:
                result = {
                    "ok": True,
                    "scan": dict(con.execute("SELECT * FROM scan").fetchone()),
                    "pages": con.execute("SELECT COUNT(*) FROM pages").fetchone()[0],
                    "links": con.execute("SELECT COUNT(*) FROM links").fetchone()[0],
                }
            finally:
                con.close()
        else:
            from seohead.reports import build_report

            inputs = (
                [args.source / name for name in ("pages.jsonl", "links.jsonl", "audit.json")]
                if args.source.is_dir()
                else [args.source]
            )
            outputs = [Path(args.out)]
            if args.format == "csv":
                outputs.append(Path(args.out).with_suffix(".pages.csv"))
            if any(
                target.exists() and source.exists() and os.path.samefile(target, source)
                for source in inputs
                for target in outputs
            ):
                raise ScanError("report output must not overwrite a source artifact or crawl input")
            document = (
                str(args.source / "audit.json") if args.source.is_dir() else read_audit(args.source)
            )
            result = build_report(document, fmt=args.format, path=args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    except (ScanError, OSError, UnicodeError) as exc:
        print(f"scan storage: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
