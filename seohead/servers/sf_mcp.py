"""MCP server (stdio) exposing the audit core through five agent-callable tools.

Requires the MCP Python SDK::

    pip install mcp

Run it directly (``python -m seohead.servers.sf_mcp``) or register it as a local
stdio connector. Large payloads are returned as file paths, never dumped inline.
Network side effects, such as sitemap
live-rechecks, remain opt-in.
"""

from __future__ import annotations

import json
import os
from typing import Any

from seohead.sf.core.audit import INPUT_MODES, run_audit
from seohead.sf.core.loader import EXPORT_MATCHERS, discover_exports
from seohead.sf.reporters import write_json, write_markdown

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - SDK optional at import time
    FastMCP = None  # type: ignore


# Imported from the audit core so this boundary cannot drift from it: adding
# a live mode there makes it valid here without a second, easily-forgotten edit.
VALID_MODES = INPUT_MODES
VALID_PROFILES = {"lite", "full", "custom"}


def _do_run(
    mode: str,
    source: str,
    profile: str = "full",
    out: str = "report",
    config: str | None = None,
    sitemap: str | None = None,
) -> dict[str, Any]:
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}")
    if profile not in VALID_PROFILES:
        raise ValueError(f"profile must be one of {sorted(VALID_PROFILES)}, got {profile!r}")
    if not source:
        raise ValueError("`input` (exports dir / .seospider / url / list) is required")
    if mode == "parse-exports":
        result = run_audit(
            input_mode=mode,
            exports_dir=source,
            profile=profile,
            config_path=config or "config.json",
            sitemap_url=sitemap,
            log=lambda m: None,
        )
    else:
        result = run_audit(
            input_mode=mode,
            source=source,
            profile=profile,
            config_path=config or "config.json",
            sitemap_url=sitemap,
            output_dir=os.path.join(out, "exports"),
            log=lambda m: None,
        )
    os.makedirs(out, exist_ok=True)
    json_path = write_json(result, os.path.join(out, "audit.json"))
    md_path = write_markdown(result, os.path.join(out, "audit.md"))
    return {
        "summary": result.summary,
        "json_path": os.path.abspath(json_path),
        "md_path": os.path.abspath(md_path),
    }


def _load(json_path: str) -> dict[str, Any]:
    if not os.path.isfile(json_path):
        raise FileNotFoundError(f"audit json not found: {json_path}")
    with open(json_path, encoding="utf-8") as fh:
        return json.load(fh)


def register(mcp):  # pragma: no cover - needs the SDK
    """Register the crawl-audit tools on an existing FastMCP server.

    Registration is separate from :func:`build_server` so the unified
    ``seohead`` server can expose both live URL tools and crawl-derived audit
    tools through one local connector. The standalone server remains available
    only for focused debugging.
    """

    from mcp.types import ToolAnnotations

    create_files = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
    create_local_files = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
    read_files = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )

    @mcp.tool(annotations=create_files, structured_output=True)
    def sf_audit_run(
        mode: str,
        input: str,
        profile: str = "full",
        out: str = "report",
        config: str | None = None,
        sitemap: str | None = None,
    ) -> dict[str, Any]:
        """Run an SF audit and write audit.json plus audit.md.

        Use ``parse-exports`` with a directory of existing CSV/XLSX exports;
        this mode does not require Screaming Frog to be installed. The
        ``load-crawl``, ``crawl``, and ``crawl-list`` modes invoke the separately
        installed, licensed SF CLI. ``input`` is respectively an exports
        directory, .seospider path, start URL, or URL-list file. Returns a compact
        summary and absolute file paths instead of embedding the large reports.
        """
        return _do_run(mode, input, profile=profile, out=out, config=config, sitemap=sitemap)

    @mcp.tool(annotations=read_files, structured_output=True)
    def sf_audit_summary(json_path: str) -> dict[str, Any]:
        """Read a compact health summary from an existing audit.json.

        Returns the project, health score, severity counts, the first 15 ranked
        checks, and sitemap statistics without loading the complete issue list
        into the agent context.
        """
        data = _load(json_path)
        s = data["summary"]
        return {
            "project": data["run"].get("project"),
            "health_score": s.get("health_score"),
            "by_severity": s.get("by_severity"),
            "top_checks": dict(list(s.get("by_check", {}).items())[:15]),
            "sitemap": s.get("sitemap"),
        }

    @mcp.tool(annotations=read_files, structured_output=True)
    def sf_audit_issues(
        json_path: str, check: str | None = None, severity: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return issues filtered by check ID and/or severity from audit.json.

        The requested limit is clamped to 1..1000 to keep MCP payloads bounded.
        Omit both filters to inspect the first issues in report order.
        """
        data = _load(json_path)
        limit = max(1, min(int(limit), 1000))  # clamp to a sane range
        out: list[dict[str, Any]] = []
        for issue in data["issues"]:
            if check and issue["check"] != check:
                continue
            if severity and issue["severity"] != severity:
                continue
            out.append(issue)
            if len(out) >= limit:
                break
        return out

    @mcp.tool(annotations=read_files, structured_output=True)
    def sf_list_exports(exports_dir: str) -> dict[str, Any]:
        """Discover recognized SF exports in a directory.

        Returns the matched files by logical export name and the logical exports
        that are absent, allowing an agent to explain which checks may be skipped
        before running the audit.
        """
        found = discover_exports(exports_dir)
        missing = [k for k in EXPORT_MATCHERS if k not in found]
        return {"found": found, "missing": missing}

    @mcp.tool(annotations=create_local_files, structured_output=True)
    def sf_audit_tasks(
        json_path: str, out: str = "report", config: str | None = None
    ) -> dict[str, Any]:
        """Build tasks.json and tasks.md from an existing audit.json.

        Priority, severity inclusion, grouping, effort estimates, and URL caps
        come from the configured ``tasks_pipeline``. Returns a compact summary
        and absolute paths to both backlog files.
        """
        from seohead.sf.config import load_config
        from seohead.sf.tasks import build_tasks, write_tasks

        backlog = build_tasks(_load(json_path), load_config(config or "config.json"))
        os.makedirs(out, exist_ok=True)
        jp, mp = write_tasks(
            backlog, os.path.join(out, "tasks.json"), os.path.join(out, "tasks.md")
        )
        return {
            "summary": backlog["summary"],
            "tasks_json": os.path.abspath(jp),
            "tasks_md": os.path.abspath(mp),
        }

    return mcp


def build_server():  # pragma: no cover - needs the SDK
    """Build the audit-only FastMCP server used for focused local debugging."""
    if FastMCP is None:
        raise RuntimeError("MCP SDK not installed. Run: pip install mcp")
    return register(FastMCP("sf-analyzer"))


def main() -> None:  # pragma: no cover
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
