"""Code-owned input contracts for public SEOHEAD commands.

The catalogue deliberately describes accepted inputs, not execution.  In
particular, a ``scan_artifact`` is a retained local file: reading it never
replays a crawl or promises that a body was retained.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass


@dataclass(frozen=True)
class InputForm:
    """One supported input form, named by handler keyword arguments."""

    kind: str
    arguments: tuple[str, ...]
    note: str = ""
    required_with: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandContract:
    """One public command and its alternative supported input forms."""

    command: str
    handler: str | None
    forms: tuple[InputForm, ...]
    note: str = ""


def _form(
    kind: str, *arguments: str, note: str = "", required_with: tuple[str, ...] = ()
) -> InputForm:
    return InputForm(kind, arguments, note, required_with)


def _command(
    command: str, handler: str | None, *forms: InputForm, note: str = ""
) -> CommandContract:
    return CommandContract(command, handler, forms, note)


# Direct CLI commands.  This module is intentionally data-only: tests at
# the CLI/handler boundary prove the entries stay synchronized without making
# package runtime import either interface layer.
COMMAND_CONTRACTS: tuple[CommandContract, ...] = (
    _command("parse", "parse", _form("live_url", "url"), _form("url_list", "urls")),
    _command(
        "crawl-site",
        "crawl_site",
        _form("live_url", "url"),
        _form("url_list", "urls"),
        _form("local_file", "urls_file", note="TXT, CSV, XLSX, or XML URL input"),
        _form(
            "scan_artifact",
            "resume",
            note="Resumes retained crawl evidence and continues network collection.",
        ),
        _form("local_config", "config"),
        _form(
            "project_directory",
            "project",
            note="Defaults the target and scans/ path; explicit paths, legacy output and resume keep their route.",
        ),
    ),
    _command("crawl-describe-settings", "crawl_describe_settings", _form("no_input")),
    _command("scan-reanalyze", "scan_reanalyze", _form("scan_artifact", "input_path")),
    _command("log-scan", "log_scan", _form("legacy_directory", "run")),
    _command(
        "compare-crawls",
        "compare_crawls",
        _form("audit_document", "before", "after", note="Each path may be audit JSON or scan.v1."),
    ),
    _command(
        "crawl-enrich",
        "crawl_enrich",
        _form("audit_document", "audit", required_with=("external_csv",)),
        _form("local_file", "external_csv", required_with=("audit",)),
    ),
    _command("segment-diff", "segment_diff", _form("audit_document", "audit")),
    _command("redirects-generate", "redirects_generate", _form("inline_json", "redirects")),
    _command("redirects-check", "redirects_check", _form("live_url", "url")),
    _command("sitemap-crawl", "sitemap_crawl", _form("live_url", "url")),
    _command("images-download", "images_download", _form("url_list", "urls")),
    _command("images-optimize", "images_optimize", _form("local_file", "files")),
    _command("keywords-cluster", "keywords_cluster", _form("inline_json", "params")),
    _command("robots-check", "robots_check", _form("live_url", "url")),
    _command("headers-check", "headers_check", _form("live_url", "url")),
    _command("asset-weight-check", "asset_weight_check", _form("live_url", "url")),
    _command("links-check", "links_check", _form("live_url", "url")),
    _command("hreflang-check", "hreflang_check", _form("live_url", "url")),
    _command("domain-profile", "domain_profile", _form("domain", "domain")),
    _command("cdn-check", "cdn_check", _form("live_url", "url")),
    _command("tech-detect", "tech_detect", _form("live_url", "url")),
    _command("security-check", "security_check", _form("live_url", "url")),
    _command(
        "backlinks-check",
        "backlinks_check",
        _form("domain", "target", required_with=("donors",)),
        _form("url_list", "donors", required_with=("target",)),
        _form("local_file", "donors_file", required_with=("target",)),
    ),
    _command(
        "schema-check",
        "schema_check",
        _form("live_url", "url"),
        _form("inline_html", "html"),
    ),
    _command(
        "schema-build",
        "schema_build",
        _form("live_url", "url"),
        _form("inline_html", "html"),
    ),
    _command("duplicate-check", "duplicate_check", _form("inline_corpus", "items")),
    _command(
        "ai-bots-check",
        "ai_bots_check",
        _form("live_url", "url"),
        _form("inline_text", "robots_text"),
    ),
    _command("mirror-check", "mirror_check", _form("live_url", "url")),
    _command("llms-txt-check", "llms_txt_check", _form("live_url", "url")),
    _command(
        "citability-check",
        "citability_check",
        _form("live_url", "url"),
        _form("inline_text", "text"),
    ),
    _command(
        "markdown-extract",
        "markdown_extract",
        _form("live_url", "url"),
        _form("inline_html", "html"),
    ),
    _command("boilerplate-report", "boilerplate_report", _form("inline_corpus", "pages")),
    _command(
        "social-meta-check",
        "social_meta_check",
        _form("live_url", "url"),
        _form("inline_json", "og", "twitter"),
    ),
    _command("soft404-check", "soft404_check", _form("live_url", "url")),
    _command("log-analyze", "log_analyze", _form("local_log", "path")),
    _command("regions-check", "regions_check", _form("live_url", "url")),
    _command("render-check", "render_check", _form("live_url", "url")),
    _command("site-audit", "site_audit", _form("live_url", "url"), _form("url_list", "urls")),
    _command(
        "report-build",
        "report_build",
        _form("audit_document", "audit", note="Audit JSON or a retained scan.v1 artifact."),
    ),
    _command("facts-export", "facts_export", _form("inline_json", "sites")),
    _command("keywords-expand", "keywords_expand", _form("provider_query", "phrase")),
    _command("keywords-seasonality", "keywords_seasonality", _form("provider_query", "phrase")),
    _command("keywords-exact", "keywords_exact", _form("provider_query", "keywords")),
    _command(
        "serp-fetch",
        "serp_fetch",
        _form("provider_query", "query"),
        _form("provider_query", "queries"),
    ),
    _command(
        "spend-report", "spend_report", _form("local_log", note="Configured local spend log.")
    ),
    _command("sources-doctor", "sources_doctor", _form("local_config")),
    _command("regions-tree", "regions_tree", _form("local_config")),
    _command("metrika-counters", "metrika_counters", _form("local_config")),
    _command("metrika-setup", "metrika_setup", _form("provider_query", "counter_id")),
    _command("metrika-report", "metrika_report", _form("provider_query", "counter_id")),
    _command(
        "google-keywords",
        "google_keywords",
        _form("provider_query", "keywords"),
        _form("provider_query", "seed"),
    ),
    _command("google-serp", "google_serp", _form("provider_query", "query")),
    _command("wayback-history", "wayback_history", _form("live_url", "url")),
    _command("crtsh-subdomains", "crtsh_subdomains", _form("domain", "domain")),
    _command("gsc-query", "gsc_query", _form("provider_query", "site_url")),
    _command(
        "crux-report",
        "crux_report",
        _form("provider_query", "url"),
        _form("provider_query", "origin"),
    ),
    _command("indexnow-submit", "indexnow_submit", _form("url_list", "urls")),
    _command(
        "scan-list",
        "scan_list",
        _form("legacy_directory", "directory"),
        _form("project_directory", "project"),
    ),
    _command(
        "project-new",
        "project_new",
        _form("project_directory", "directory"),
        _form("live_url", "target"),
    ),
    _command("project-open", "project_open", _form("project_directory", "directory")),
    _command("project-status", "project_status", _form("project_directory", "directory")),
    _command("scan-inspect", "scan_inspect", _form("scan_artifact", "input_path")),
    _command("scan-snapshot", "scan_snapshot", _form("scan_artifact", "input_path")),
    _command("scan-pin", "scan_pin", _form("scan_artifact", "input_path")),
    _command(
        "scan-prune",
        "scan_prune",
        _form("legacy_directory", "directory"),
        _form("local_file", "plan"),
        _form(
            "project_directory",
            "project",
            note="Defaults the directory to project scans/; apply remains explicit.",
        ),
    ),
    _command(
        "scan-body-diff",
        "scan_body_diff",
        _form("scan_artifact", "left", "right"),
        _form("selector", "url", note="Selects the logical URL within both scans."),
    ),
)


SF_CONTRACTS: tuple[CommandContract, ...] = (
    _command(
        "sf run",
        None,
        _form("live_url", "crawl"),
        _form("local_file", "load_crawl", note="Saved .seospider crawl; requires licensed SF CLI"),
        _form("local_file", "crawl_list", note="URL-list file for licensed SF live traversal"),
        _form("legacy_directory", "exports_dir"),
        _form("local_config", "config"),
    ),
    _command(
        "sf tasks", None, _form("audit_document", "audit_json"), _form("local_config", "config")
    ),
    _command("sf doctor", None, _form("local_config", "config")),
    _command("sf save-config", None, _form("no_input")),
    _command("mcp", None, _form("no_input"), note="Starts the local stdio server."),
)


CONTRACTS = COMMAND_CONTRACTS + SF_CONTRACTS

_KIND_LABELS = {
    "live_url": "Live URL",
    "url_list": "URL list",
    "domain": "Domain",
    "scan_artifact": "Scan artifact",
    "audit_document": "Audit document",
    "legacy_directory": "Local directory",
    "local_file": "Local file",
    "local_log": "Local log",
    "inline_corpus": "Inline corpus",
    "inline_json": "Inline JSON",
    "inline_html": "Inline HTML",
    "inline_text": "Inline text",
    "selector": "Selector",
    "provider_query": "Provider query",
    "local_config": "Local configuration",
    "project_directory": "Project directory",
    "no_input": "No direct input",
}


def coverage_gaps(
    commands: Collection[str], sf_subcommands: Collection[str]
) -> tuple[set[str], set[str]]:
    """Return public commands missing from, or stale in, this catalogue.

    Callers provide interface registries so package runtime never imports CLI or
    server modules merely to expose its input metadata.
    """
    expected = set(commands) | {f"sf {name}" for name in sf_subcommands} | {"mcp"}
    catalogued = {contract.command for contract in CONTRACTS}
    return expected - catalogued, catalogued - expected


def render_markdown() -> str:
    """Render the checked-in public input reference."""
    lines = [
        "# Command inputs",
        "",
        "This reference is generated from `seohead.input_contracts`. Each row inventories consumed",
        "source inputs rather than inferring them from a command's output. Forms on one row can be",
        "required together; the notes name those relationships.",
        "",
        "A **scan artifact** is a retained local `scan.v1` SQLite file. Read-only analysis and",
        "history operations do not replay a crawl or promise retained page bodies. `crawl-site --resume`",
        "is the explicit exception: it continues network collection. `duplicate-check` and",
        "`boilerplate-report` currently accept inline corpora only; they do not accept `--scan`.",
        "",
        "## Operational-store decision",
        "",
        "`scan.v1` is the authoritative retained evidence store for an artifact-mode crawl.",
        "The HTTP cache remains sharded `http_cache.v3` files because concurrent crawl workers",
        "can independently replace one cache entry. In live cache modes a cache miss is a safe",
        "network fallback; a replay-mode miss remains offline and is reported. The run",
        "journal remains an append-only local `runs.jsonl` record. Neither store is a second scan corpus,",
        "and this decision makes no backend migration.",
        "",
        "## Catalogue",
        "",
        "| Command | Accepted input forms | Notes |",
        "| --- | --- | --- |",
    ]
    for contract in CONTRACTS:
        forms = "<br>".join(
            f"{_KIND_LABELS[form.kind]} (`{', '.join(form.arguments)}`)"
            + (f"; requires `{', '.join(form.required_with)}`" if form.required_with else "")
            if form.arguments
            else _KIND_LABELS[form.kind]
            for form in contract.forms
        )
        notes = "; ".join(filter(None, [contract.note, *(form.note for form in contract.forms)]))
        lines.append(f"| `{contract.command}` | {forms} | {notes or '—'} |")
    lines.append("")
    return "\n".join(lines)
