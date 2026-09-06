"""Unified ``seohead`` CLI over the toolkit's shared handler layer.

Usage:
    seohead <command> [--input '<json>'] [convenience flags]
    seohead sf <run|tasks|doctor> ...   # audit Screaming Frog crawl data
    seohead mcp            # run the MCP server (stdio)

Primary input is --input '<json>' (an object mapped onto the handler kwargs) or
piped stdin JSON; a few convenience flags are also accepted. Output is pretty JSON
to stdout. Exit codes are documented in one place: docs/USAGE.md's "Input conventions".
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from seohead import __version__, runlog
from seohead.servers import handlers

# command -> handler kwarg builder. Each maps CLI namespace + --input dict -> kwargs.
COMMANDS = (
    "parse",
    "crawl-site",
    "crawl-describe-settings",
    "scan-reanalyze",
    "log-scan",
    "compare-crawls",
    "segment-diff",
    "redirects-generate",
    "redirects-check",
    "sitemap-crawl",
    "images-download",
    "images-optimize",
    "keywords-cluster",
    "robots-check",
    "headers-check",
    "asset-weight-check",
    "links-check",
    "hreflang-check",
    "domain-profile",
    "cdn-check",
    "tech-detect",
    "security-check",
    "backlinks-check",
    "schema-check",
    "schema-build",
    "duplicate-check",
    "ai-bots-check",
    "mirror-check",
    "llms-txt-check",
    "citability-check",
    "markdown-extract",
    "boilerplate-report",
    "social-meta-check",
    "soft404-check",
    "log-analyze",
    "regions-check",
    "render-check",
    "site-audit",
    "report-build",
    "facts-export",
    "keywords-expand",
    "keywords-seasonality",
    "keywords-exact",
    "serp-fetch",
    "spend-report",
    "sources-doctor",
    "regions-tree",
    "metrika-counters",
    "metrika-setup",
    "metrika-report",
    "google-keywords",
    "google-serp",
    "wayback-history",
    "crtsh-subdomains",
    "gsc-query",
    "crux-report",
    "indexnow-submit",
)

# Tools whose complete direct CLI input can be supplied by one --url flag.
URL_COMMANDS = (
    "crawl-site",
    "parse",
    "redirects-check",
    "sitemap-crawl",
    "robots-check",
    "headers-check",
    "asset-weight-check",
    "links-check",
    "hreflang-check",
    "cdn-check",
    "tech-detect",
    "security-check",
    "schema-check",
    "mirror-check",
    "schema-build",
    "ai-bots-check",
    "llms-txt-check",
    "citability-check",
    "markdown-extract",
    "social-meta-check",
    "soft404-check",
    "render-check",
)


# Wait briefly for piped stdin before deciding that no JSON input is coming.
# A zero wait races with `echo '{...}' | seohead parse`; an unlimited wait hangs a command launched
# by a script or CI job whose pipe is open but empty. The latter occurred in a real site-audit run.
STDIN_WAIT_SECONDS = 0.2


def _stdin_has_data() -> bool:
    if sys.stdin is None or sys.stdin.closed or sys.stdin.isatty():
        return False
    try:
        import select

        ready, _, _ = select.select([sys.stdin], [], [], STDIN_WAIT_SECONDS)
        return bool(ready)
    except (ImportError, OSError, ValueError):
        # Some file-like objects do not support select (notably replaced stdin in tests). Fall back
        # to reading them directly; those controlled streams deliver EOF immediately.
        return True


# These flags already identify the input source. When any is present, never inspect stdin. Otherwise
# `while read u; do seohead parse --url "$u"; done < urls.txt` loses the remainder of the file on
# its first iteration: regular-file stdin is always reported as ready, so the command consumes every
# unread URL and the loop processes only one.
#
# Populated by _source_flag() below rather than hand-listed here: a hand-kept copy of "which
# flags count" drifted out of sync with the parser once already (#156 — --phrase, --keywords,
# --query/--queries, --seed, --counter, and --before/--after were all missing), silently
# truncating any per-line loop over one of them to a single iteration.
SOURCE_FLAGS: set[str] = set()


def _source_flag(sub: argparse.ArgumentParser, *args: str, **kwargs: Any) -> argparse.Action:
    """Add a flag whose value alone supplies a command's complete input (a URL, a file path, a
    search phrase, a counter ID, ...) and register it in SOURCE_FLAGS in the same place it is
    defined, so the set cannot drift from the parser the way the old hand-maintained tuple did.
    """
    action = sub.add_argument(*args, **kwargs)
    SOURCE_FLAGS.add(action.dest)
    return action


def _has_source_flag(args: argparse.Namespace) -> bool:
    return any(getattr(args, name, None) for name in SOURCE_FLAGS)


def _load_input(raw: str | None, allow_stdin: bool = True) -> dict[str, Any]:
    if raw:
        return json.loads(raw)
    if allow_stdin and _stdin_has_data():
        data = sys.stdin.read().strip()
        if data:
            return json.loads(data)
    return {}


def _split_list(val: str | None) -> list[str] | None:
    """Parse a comma-separated list while preserving quoted commas inside an item.

        --queries "technical SEO,site audit"       -> two items
        --queries "'SEO audit, enterprise sites'"  -> one item with its comma preserved

    Search queries and page titles commonly contain commas. Silently splitting such a value would
    change the requested input and, for paid providers, could issue two billable calls instead of one.
    """
    if not val:
        return None
    import csv
    from io import StringIO

    # The CSV parser handles quoting while retaining commas inside fields.
    for quote in ("'", '"'):
        if quote in val:
            rows = list(csv.reader(StringIO(val), quotechar=quote, skipinitialspace=True))
            if rows:
                return [s.strip() for s in rows[0] if s.strip()]
    return [s.strip() for s in val.split(",") if s.strip()]


def _build_kwargs(cmd: str, args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Return (handler_name, kwargs) for a command from flags + --input JSON."""
    data = _load_input(getattr(args, "input", None), allow_stdin=not _has_source_flag(args))
    handler_name = cmd.replace("-", "_")
    kw: dict[str, Any] = dict(data)  # --input is the base; flags override/augment

    if cmd == "scan-reanalyze":
        for flag in ("input_path", "out", "producer_build"):
            value = getattr(args, flag, None)
            if value is not None:
                kw[flag] = value
    elif cmd == "parse":
        if args.url:
            kw["url"] = args.url
        if args.urls:
            kw["urls"] = _split_list(args.urls)
    elif cmd == "redirects-generate":
        kw.setdefault("redirects", data.get("redirects", []))
        if args.format:
            kw["fmt"] = args.format
    elif cmd == "redirects-check":
        if args.url:
            kw["url"] = args.url
    elif cmd == "crawl-site":
        if args.url:
            kw["url"] = args.url
        if getattr(args, "urls", None):
            kw["urls"] = _split_list(args.urls)
        for flag in (
            "config",
            "max_urls",
            "max_depth",
            "min_delay",
            "out_dir",
            "robots",
            "sitemap",
            "scan_out",
            "producer_build",
        ):
            value = getattr(args, flag, None)
            if value is not None:
                kw[flag] = value
        from seohead.crawl import settings as crawl_config

        overrides: dict[str, Any] = {}
        rate = getattr(args, "max_urls_per_second", None)
        if rate is not None:
            overrides["speed.min_delay_seconds"] = crawl_config.delay_for_request_rate(rate)
        for assignment in getattr(args, "set_settings", None) or ():
            path, value = crawl_config.parse_setting_assignment(assignment)
            overrides[path] = value
        if overrides:
            kw["overrides"] = overrides
    elif cmd == "sitemap-crawl":
        if args.url:
            kw["url"] = args.url
        if args.concurrency is not None:
            kw["concurrency"] = args.concurrency
    elif cmd == "images-download":
        if args.urls:
            kw["urls"] = _split_list(args.urls)
        if args.output_dir:
            kw["output_dir"] = args.output_dir
    elif cmd == "images-optimize":
        if args.files:
            kw["files"] = _split_list(args.files)
        settings = dict(kw.get("settings") or {})
        if getattr(args, "output_dir", None):
            settings["out_dir"] = args.output_dir
        for key in ("format", "quality", "max_width", "max_height", "max_pixels"):
            value = getattr(args, key, None)
            if value is not None:
                settings[key] = value
        if getattr(args, "in_place", False):
            settings["in_place"] = True
        if getattr(args, "overwrite", False):
            settings["overwrite"] = True
        kw["settings"] = settings
    elif cmd == "keywords-cluster":
        pass  # keywords/algorithm come from --input JSON
    elif cmd == "duplicate-check":
        if getattr(args, "threshold", None) is not None:
            kw["threshold"] = args.threshold
        if getattr(args, "fingerprints", False):
            kw["with_fingerprints"] = True
        if getattr(args, "all_pages", False):
            kw["only_indexable"] = False
        # items[] is intentionally accepted through --input JSON.
    elif cmd == "log-analyze":
        if args.path:
            kw["path"] = args.path
        if getattr(args, "verify_bots", False):
            kw["verify_bots"] = True
    elif cmd == "site-audit":
        if args.url:
            kw["url"] = args.url
        if getattr(args, "urls", None):
            kw["urls"] = _split_list(args.urls)
        if getattr(args, "limit", None) is not None:
            kw["limit"] = args.limit
        if getattr(args, "concurrency", None) is not None:
            kw["concurrency"] = args.concurrency
        if getattr(args, "render", False):
            kw["render"] = True
        if getattr(args, "skip", None):
            kw["skip"] = _split_list(args.skip)
        if getattr(args, "report", None):
            kw["_report"] = args.report
            kw["_out"] = getattr(args, "out", None)
    elif cmd == "report-build":
        if getattr(args, "audit", None):
            kw["audit"] = args.audit
        if getattr(args, "format", None):
            kw["fmt"] = args.format
        if getattr(args, "out", None):
            kw["out"] = args.out
    elif cmd == "log-scan":
        if getattr(args, "run", None):
            kw["run"] = args.run
        if getattr(args, "images_dir", None):
            kw["images_dir"] = args.images_dir
    elif cmd == "compare-crawls":
        if getattr(args, "before", None):
            kw["before"] = args.before
        if getattr(args, "after", None):
            kw["after"] = args.after
    elif cmd == "segment-diff":
        if getattr(args, "audit", None):
            kw["audit"] = args.audit
        if getattr(args, "source", None):
            kw["source"] = args.source
        if getattr(args, "target", None):
            kw["target"] = args.target
    elif cmd == "regions-check":
        if args.url:
            kw["url"] = args.url
        if getattr(args, "extra", None):
            kw["extra"] = _split_list(args.extra)
        if getattr(args, "limit", None) is not None:
            kw["limit"] = args.limit
        if getattr(args, "render", False):
            kw["render"] = True
    elif cmd == "domain-profile":
        if args.domain:
            kw["domain"] = args.domain
        if getattr(args, "no_tls", False):
            kw["with_tls"] = False
    elif cmd == "backlinks-check":
        if args.target:
            kw["target"] = args.target
        donors = _split_list(args.donors) or []
        if args.donors_file:
            donors += _read_donors(args.donors_file)
        if donors:
            kw["donors"] = donors
        if args.concurrency is not None:
            kw["concurrency"] = args.concurrency
    if cmd == "keywords-expand":
        if args.phrase:
            kw["phrase"] = args.phrase
        if args.limit is not None:
            kw["limit"] = args.limit
        if getattr(args, "regions", None):
            kw["regions"] = _split_list(args.regions)
    if cmd == "keywords-seasonality":
        for name in ("phrase", "from_date", "to_date", "period"):
            value = getattr(args, name, None)
            if value:
                kw[name] = value
        if getattr(args, "regions", None):
            kw["regions"] = _split_list(args.regions)
    if cmd == "keywords-exact":
        if getattr(args, "keywords", None):
            kw["keywords"] = _split_list(args.keywords)
        if getattr(args, "region", None) is not None:
            kw["region"] = args.region
        if getattr(args, "no_wait", False):
            kw["wait"] = False
    if cmd == "serp-fetch":
        if getattr(args, "query", None):
            kw["query"] = args.query
        if getattr(args, "queries", None):
            kw["queries"] = _split_list(args.queries)
        if getattr(args, "region", None):
            kw["region"] = str(args.region)
        if getattr(args, "top", None) is not None:
            kw["top"] = args.top
    if cmd == "google-keywords":
        if getattr(args, "keywords", None):
            kw["keywords"] = _split_list(args.keywords)
        for name in ("seed", "language", "country"):
            value = getattr(args, name, None)
            if value:
                kw[name] = value
        if getattr(args, "location_code", None) is not None:
            kw["location_code"] = args.location_code
        if getattr(args, "limit", None) is not None:
            kw["limit"] = args.limit
        if getattr(args, "difficulty", False):
            kw["difficulty"] = True
    if cmd == "google-serp":
        for name in ("query", "language", "country"):
            value = getattr(args, name, None)
            if value:
                kw[name] = value
        if getattr(args, "location_code", None) is not None:
            kw["location_code"] = args.location_code
        if getattr(args, "depth", None) is not None:
            kw["depth"] = args.depth
    if cmd == "wayback-history":
        if args.url:
            kw["url"] = args.url
        for name in ("limit", "from_date", "to_date"):
            value = getattr(args, name, None)
            if value:
                kw[name] = value
    if cmd == "crtsh-subdomains" and args.domain:
        kw["domain"] = args.domain
    if cmd == "gsc-query":
        if args.site_url:
            kw["site_url"] = args.site_url
        for name in ("mode", "start_date", "end_date", "inspection_url"):
            value = getattr(args, name, None)
            if value:
                kw[name] = value
        if getattr(args, "dimensions", None):
            kw["dimensions"] = _split_list(args.dimensions)
        if getattr(args, "row_limit", None):
            kw["row_limit"] = args.row_limit
    if cmd == "crux-report":
        if getattr(args, "url", None):
            kw["url"] = args.url
        if getattr(args, "origin", None):
            kw["origin"] = args.origin
        if getattr(args, "form_factor", None):
            kw["form_factor"] = args.form_factor
        if getattr(args, "metrics", None):
            kw["metrics"] = _split_list(args.metrics)
    if cmd == "indexnow-submit":
        if getattr(args, "urls", None):
            kw["urls"] = _split_list(args.urls)
        if getattr(args, "host", None):
            kw["host"] = args.host
        if getattr(args, "key_location", None):
            kw["key_location"] = args.key_location
    if cmd == "metrika-setup" and getattr(args, "counter", None):
        kw["counter_id"] = args.counter
    if cmd == "metrika-report":
        if getattr(args, "counter", None):
            kw["counter_id"] = args.counter
        for name in ("metrics", "dimensions", "date1", "date2", "filters", "sort"):
            value = getattr(args, name, None)
            if value:
                kw[name] = value
        if getattr(args, "limit", None) is not None:
            kw["limit"] = args.limit
        if getattr(args, "paginate", False):
            kw["paginate"] = True
    if cmd == "regions-tree" and getattr(args, "save_to", None):
        kw["save_to"] = args.save_to
    if cmd == "spend-report" and getattr(args, "since", None):
        kw["since"] = args.since
    if cmd in URL_COMMANDS:
        if args.url:
            kw["url"] = args.url
        if cmd == "links-check" and getattr(args, "internal_only", False):
            kw["internal_only"] = True
        if cmd == "security-check" and getattr(args, "probe_paths", False):
            kw["probe_paths"] = True
        if cmd == "schema-build" and getattr(args, "type", None):
            kw["override_type"] = args.type
        if cmd == "render-check":
            if getattr(args, "viewport", None):
                kw["viewport"] = args.viewport
            if getattr(args, "wait", None):
                kw["wait"] = args.wait
        if cmd == "llms-txt-check" and getattr(args, "brand", None):
            kw["brand"] = args.brand
    return handler_name, kw


def _print_config_help() -> None:
    """List every crawl-site config setting, generated from seohead.crawl.settings.

    One source of truth: this reads the same DEFAULTS/DESCRIPTIONS that the
    config file loader validates against, so a setting cannot be added to the
    module without becoming visible here.
    """
    from seohead.crawl import settings as crawl_settings

    print(
        "Crawler config settings (seohead/crawl/settings.py). Set these in a JSON file passed to "
        '--config, e.g. {"limits": {"max_urls": 50}}.'
    )
    print()
    for setting in crawl_settings.describe_settings():
        marker = "*" if setting["results_affecting"] else " "
        print(f"{marker} {setting['path']} ({setting['type']}, default {setting['default']!r})")
        print(f"      {setting['description']}")
    print()
    print("* changes what the audit finds; recorded in the run manifest.")


def _print_effective_rate(kwargs: dict[str, Any]) -> None:
    """Print the worst-case requests/second a crawl-site run permits, before it runs.

    Politeness is a combination of settings, not one knob (#14): printing the
    derived number at startup, on stderr so the JSON result on stdout stays
    clean, is what lets an operator catch a dangerous combination before the
    crawl — rather than only from a killed process or a struggling site — and
    it is what `--config`'s values actually resolve to, not just what the file
    or flags said in isolation.
    """
    from seohead.crawl import settings as crawl_config

    try:
        # The same overrides the handler will resolve, in the same precedence, or
        # the printed rate describes a run that is not the one about to happen --
        # which is worse than printing nothing, because it is believed.
        overrides = dict(kwargs.get("overrides") or {})
        # Only a named argument that was actually given wins. Updating with None
        # would erase a --set or --max-urls-per-second value and silently fall
        # back to the default, which is how a rate cap becomes a no-op.
        for path, value in (
            ("limits.max_urls", kwargs.get("max_urls")),
            ("limits.max_depth", kwargs.get("max_depth")),
            ("speed.min_delay_seconds", kwargs.get("min_delay")),
            ("speed.concurrency", kwargs.get("concurrency")),
            ("robots.policy", kwargs.get("robots")),
            ("output.dir", kwargs.get("out_dir")),
        ):
            if value is not None:
                overrides[path] = value
        resolved = crawl_config.load(kwargs.get("config"), overrides=overrides)
    except crawl_config.ConfigError:
        return  # the handler call below reports the same error to the user
    rate = crawl_config.effective_request_rate(resolved)
    shown = "unbounded" if rate == float("inf") else f"{rate:.2f} req/s"
    print(f"crawl-site: effective worst-case request rate to one host: {shown}", file=sys.stderr)


def _read_donors(path: str) -> list[str]:
    """Read one donor URL per line, ignoring blank lines and ``#`` comments."""
    with open(path, "r", encoding="utf-8") as fh:  # noqa: UP015 - explicit read-only contract
        return [line.strip() for line in fh if line.strip() and not line.lstrip().startswith("#")]


def _add_flags(sub: argparse.ArgumentParser, cmd: str) -> None:
    sub.add_argument("--input", help="JSON object mapped onto the handler arguments")
    if cmd == "scan-reanalyze":
        _source_flag(sub, "--source", dest="input_path", help="retained SQLite scan to read")
        sub.add_argument("--out", help="new derived SQLite file; never overwrites an existing file")
        sub.add_argument("--producer-build", metavar="SHA", help="current analyzer source build")
    if cmd in URL_COMMANDS:
        _source_flag(sub, "--url", help="target URL")
    if cmd == "links-check":
        sub.add_argument("--internal-only", action="store_true", help="check internal links only")
    if cmd == "log-analyze":
        _source_flag(sub, "--path", help="web server access-log file (Apache, Nginx, or IIS)")
        sub.add_argument(
            "--verify-bots",
            action="store_true",
            help="verify bot identities with forward-confirmed reverse DNS "
            "(performs network lookups)",
        )
    if cmd == "crawl-site":
        _source_flag(
            sub,
            "--urls",
            help="comma-separated URL list: list mode, no discovery",
        )
        sub.add_argument("--max-urls", type=int, help="URL budget (default 200)")
        sub.add_argument("--out-dir", help="directory for pages.jsonl and audit.json")
        sub.add_argument("--scan-out", metavar="FILE", help="opt-in SQLite scan artifact")
        sub.add_argument(
            "--producer-build", metavar="SHA", help="original source build for SQLite capture"
        )
        sub.add_argument("--config", help="path to a crawler config file (JSON)")
        sub.add_argument(
            "--robots",
            choices=["respect", "report_only", "ignore"],
            help="obey, report-only, or skip robots.txt",
        )
        sub.add_argument(
            "--sitemap",
            help="seed and reconcile sitemap URLs; auto-discovery uses --config",
        )
        sub.add_argument(
            "--config-help",
            action="store_true",
            help="list every crawler configuration setting",
        )
        sub.add_argument(
            "--max-urls-per-second",
            type=float,
            metavar="N",
            help="cap the request rate to one host, the way a site owner states it "
            "(sets speed.min_delay_seconds to 1/N). Parity with 'sf run'.",
        )
        sub.add_argument(
            "--set",
            action="append",
            dest="set_settings",
            metavar="PATH=VALUE",
            help="set any crawler setting without writing a config file, e.g. "
            "--set speed.concurrency=4 --set scope.include_patterns=/blog/,/docs/. "
            "Repeatable; applied after --config. See --config-help for every path.",
        )
        # Kept working for scripts written before --config existed, but no longer advertised in
        # --help: depth and delay are exactly the kind of setting #13's config file exists for, and
        # every flag shown here is one more line standing between a new setting and --config.
        # --set is the answer to that tension rather than an exception to it: one flag reaches
        # every setting, and a setting added tomorrow is reachable with no CLI change at all.
        sub.add_argument("--max-depth", type=int, help=argparse.SUPPRESS)
        sub.add_argument("--min-delay", type=float, help=argparse.SUPPRESS)
    if cmd == "site-audit":
        _source_flag(sub, "--url", help="site home page")
        _source_flag(
            sub,
            "--urls",
            help="explicit comma-separated page list (otherwise discovered from the sitemap)",
        )
        sub.add_argument("--limit", type=int, help="maximum pages to inspect (default 25)")
        sub.add_argument("--concurrency", type=int, help="maximum concurrent requests (default 5)")
        sub.add_argument(
            "--render",
            action="store_true",
            help="inspect the rendered DOM for regional selectors (requires Playwright)",
        )
        sub.add_argument("--skip", help="comma-separated tools to skip")
        sub.add_argument(
            "--report",
            choices=("xlsx", "docx", "csv", "md", "json"),
            help="build a report in this format after the audit",
        )
        sub.add_argument("--out", help="report output path")
    if cmd == "keywords-expand":
        _source_flag(sub, "--phrase", help="seed phrase")
        sub.add_argument("--limit", type=int, help="maximum refinements to return (default 300)")
        sub.add_argument("--regions", help="comma-separated Yandex region IDs (225 is Russia)")
    if cmd == "keywords-seasonality":
        _source_flag(sub, "--phrase", help="query phrase")
        sub.add_argument(
            "--from-date",
            dest="from_date",
            help="period start in RFC3339 form, e.g. 2026-01-01T00:00:00Z",
        )
        sub.add_argument("--to-date", dest="to_date", help="period end in RFC3339 form")
        sub.add_argument("--period", help="PERIOD_MONTHLY | PERIOD_WEEKLY | PERIOD_DAILY")
        sub.add_argument("--regions", help="comma-separated Yandex region IDs")
    if cmd == "keywords-exact":
        _source_flag(sub, "--keywords", help="comma-separated phrases")
        sub.add_argument("--region", type=int, help="Yandex region ID (default 225)")
        sub.add_argument(
            "--no-wait",
            action="store_true",
            help="create the paid task and exit; retrieve its result later by task_id",
        )
    if cmd == "serp-fetch":
        _source_flag(sub, "--query", help="single search query")
        _source_flag(sub, "--queries", help="comma-separated batch of search queries")
        sub.add_argument("--region", help="Yandex region ID (default 225)")
        sub.add_argument("--top", type=int, help="number of result positions (default 10)")
    if cmd == "google-keywords":
        _source_flag(sub, "--keywords", help="comma-separated phrases for search-volume lookup")
        _source_flag(sub, "--seed", help="seed phrase for keyword expansion")
        sub.add_argument(
            "--location-code",
            dest="location_code",
            type=int,
            help="DataForSEO location code (2840 is the United States)",
        )
        sub.add_argument("--language", help="language code (default en)")
        sub.add_argument(
            "--country",
            help="country used by the coverage guard; Russia and Belarus are unsupported",
        )
        sub.add_argument("--limit", type=int, help="maximum keyword ideas (default 100)")
        sub.add_argument(
            "--difficulty",
            action="store_true",
            help="return keyword difficulty instead of search volume",
        )
    if cmd == "google-serp":
        _source_flag(sub, "--query", help="search query")
        sub.add_argument(
            "--location-code",
            dest="location_code",
            type=int,
            help="DataForSEO location code (2840 is the United States)",
        )
        sub.add_argument("--language", help="language code (default en)")
        sub.add_argument("--depth", type=int, help="number of result positions (default 10)")
        sub.add_argument("--country", help="country used by the provider coverage guard")
    if cmd == "wayback-history":
        _source_flag(sub, "--url", help="URL to look up in the Wayback Machine")
        sub.add_argument("--limit", type=int, help="maximum snapshots to return")
        sub.add_argument("--from-date", dest="from_date", help="earliest timestamp, e.g. 2024")
        sub.add_argument("--to-date", dest="to_date", help="latest timestamp, e.g. 20260101")
    if cmd == "crtsh-subdomains":
        _source_flag(sub, "--domain", help="domain to search Certificate Transparency logs for")
    if cmd == "gsc-query":
        _source_flag(sub, "--site-url", dest="site_url", help="verified Search Console property")
        sub.add_argument(
            "--mode",
            choices=("search_analytics", "inspect_url"),
            help="search_analytics (default) or inspect_url",
        )
        sub.add_argument("--start-date", dest="start_date", help="period start (default 28daysAgo)")
        sub.add_argument("--end-date", dest="end_date", help="period end (default today)")
        sub.add_argument("--dimensions", help="comma-separated dimensions, e.g. query,page")
        sub.add_argument("--row-limit", dest="row_limit", type=int, help="rows to return")
        sub.add_argument("--inspection-url", dest="inspection_url", help="URL for mode=inspect_url")
    if cmd == "crux-report":
        _source_flag(sub, "--url", help="page URL to report on")
        _source_flag(sub, "--origin", help="origin to report on, instead of a single URL")
        sub.add_argument(
            "--form-factor", dest="form_factor", choices=("PHONE", "DESKTOP", "TABLET")
        )
        sub.add_argument("--metrics", help="comma-separated CrUX metric names")
    if cmd == "indexnow-submit":
        _source_flag(sub, "--urls", help="comma-separated URLs to submit")
        sub.add_argument("--host", help="host the submitted URLs and key belong to")
        sub.add_argument("--key-location", dest="key_location", help="key file URL, if non-default")
    if cmd == "metrika-setup":
        _source_flag(sub, "--counter", help="Yandex Metrika counter ID")
    if cmd == "metrika-report":
        _source_flag(sub, "--counter", help="Yandex Metrika counter ID")
        sub.add_argument(
            "--metrics", help="comma-separated API metrics, e.g. ym:s:visits,ym:s:users"
        )
        sub.add_argument("--dimensions", help="comma-separated API dimensions, e.g. ym:s:startURL")
        sub.add_argument("--date1", help="period start (a date or 30daysAgo)")
        sub.add_argument("--date2", help="period end (a date or today)")
        sub.add_argument("--filters", help="filter expression in Metrika API notation")
        sub.add_argument("--sort", help="sort expression, e.g. -ym:s:visits")
        sub.add_argument("--limit", type=int, help="rows per API page (default 100)")
        sub.add_argument(
            "--paginate", action="store_true", help="collect all API pages, capped at 100,000 rows"
        )
    if cmd == "regions-tree":
        sub.add_argument("--save-to", dest="save_to", help="save a flat {name: id} mapping as JSON")
    if cmd == "spend-report":
        sub.add_argument("--since", help="include charges on or after YYYY-MM-DD")
    if cmd == "report-build":
        _source_flag(
            sub, "--audit", help="path to an audit JSON document or scan.v1 SQLite artifact"
        )
        sub.add_argument(
            "--format",
            choices=("xlsx", "docx", "csv", "md", "json"),
            help="report format (default xlsx)",
        )
        sub.add_argument("--out", help="output file path")
    if cmd == "log-scan":
        # Not `required=True`: that would reject a JSON-only `--input '{"run": ...}'` call before
        # _build_kwargs ever runs, since argparse enforces required flags ahead of dispatch. The
        # handler already raises a clear error when `run` is missing from both sources (#218).
        _source_flag(sub, "--run", help="directory holding audit.json and/or pages.jsonl")
        sub.add_argument(
            "--images-dir",
            help="an images-download output directory, so a recorded size can be compared "
            "against the file on disk",
        )
    if cmd == "compare-crawls":
        # See the log-scan comment above: `required=True` here would reject a JSON-only
        # `--input '{"before": ..., "after": ...}'` call the same way (#218). compare_crawls
        # already raises a clear error when either side is missing.
        _source_flag(
            sub, "--before", help="path to the earlier audit.json or scan.v1 SQLite artifact"
        )
        _source_flag(sub, "--after", help="path to the later audit.json or scan.v1 SQLite artifact")
    if cmd == "segment-diff":
        # See the log-scan comment above: `required=True` here would reject a JSON-only
        # `--input '{"audit": ..., "source": ..., "target": ...}'` call the same way (#218).
        # segment_diff already raises a clear error when any of the three is missing.
        _source_flag(
            sub, "--audit", help="path to the audit.json or scan.v1 SQLite artifact to diff"
        )
        sub.add_argument("--source", help="segment name that should have a counterpart")
        sub.add_argument("--target", help="segment name to look for the counterpart in")
    if cmd == "regions-check":
        _source_flag(sub, "--url", help="any site page, usually the home page")
        sub.add_argument(
            "--extra",
            help="comma-separated additional regional URLs; "
            "satellite domains cannot be discovered from the page",
        )
        sub.add_argument("--limit", type=int, help="maximum regional pages to fetch (default 12)")
        sub.add_argument(
            "--render",
            action="store_true",
            help="find JavaScript-rendered city selectors (requires Playwright)",
        )
    if cmd == "render-check":
        sub.add_argument(
            "--viewport",
            choices=("desktop", "mobile"),
            help="viewport and device emulation mode (default desktop)",
        )
        sub.add_argument(
            "--wait",
            choices=("load", "domcontentloaded", "networkidle"),
            help="DOM capture milestone (default load; networkidle may never "
            "occur on sites with persistent connections)",
        )
    if cmd == "domain-profile":
        _source_flag(sub, "--domain", help="domain name or URL")
        sub.add_argument("--no-tls", action="store_true", help="skip TLS certificate inspection")
    if cmd == "security-check":
        sub.add_argument(
            "--probe-paths",
            action="store_true",
            help="also probe for exposed service paths such as .git and .env",
        )
    if cmd == "schema-build":
        sub.add_argument(
            "--type",
            help="explicit Schema.org type (Product, Service, Article, etc.) "
            "when the classifier has low confidence",
        )
    if cmd == "backlinks-check":
        _source_flag(sub, "--target", help="target domain or exact URL")
        _source_flag(sub, "--donors", help="comma-separated donor-page URLs")
        _source_flag(sub, "--donors-file", help="file containing one donor URL per line")
        sub.add_argument("--concurrency", type=int, help="maximum concurrent requests (default 3)")
    if cmd in ("parse", "images-download"):
        _source_flag(sub, "--urls", help="comma-separated URL list")
    if cmd == "redirects-generate":
        sub.add_argument("--format", help="apache-rewrite-rule|apache-redirect|nginx|custom")
    if cmd == "sitemap-crawl":
        sub.add_argument("--concurrency", type=int, help="parallel fetches (default 3)")
    if cmd == "images-download":
        sub.add_argument("--output-dir", help="download target directory")
    if cmd == "images-optimize":
        _source_flag(sub, "--files", help="comma-separated image paths/dirs")
        sub.add_argument("--output-dir", help="safe output directory (recommended)")
        sub.add_argument(
            "--in-place",
            action="store_true",
            help="explicitly allow source mutation; backups are enabled by default",
        )
        sub.add_argument(
            "--overwrite", action="store_true", help="replace an existing destination file"
        )
        sub.add_argument("--format", choices=("keep", "jpeg", "png", "webp", "avif"))
        sub.add_argument("--quality", type=int, help="lossy quality, clamped to 10..100")
        sub.add_argument("--max-width", type=int, help="maximum output width; never upscales")
        sub.add_argument("--max-height", type=int, help="maximum output height; never upscales")
        sub.add_argument("--max-pixels", type=int, help="input pixel safety limit")
    if cmd == "duplicate-check":
        sub.add_argument(
            "--threshold", type=float, help="similarity threshold from 0 to 1 (default 0.92)"
        )
        sub.add_argument(
            "--fingerprints",
            action="store_true",
            help="include every page fingerprint in output; this can make stdout "
            "very large for substantial datasets",
        )
        sub.add_argument(
            "--all-pages",
            action="store_true",
            help="also compare non-indexable items; by default only items with "
            "indexable=true (or no indexable flag) are compared, since a "
            "canonicalised twin is not a defect",
        )
    if cmd == "llms-txt-check":
        sub.add_argument("--brand", help="brand name that llms.txt should mention")


# crawl-site keeps only its most-used settings as direct flags; everything the crawler build-out
# (#13 onward) has added or will add lives in --config instead, so --help stays short as the
# surface grows. This note is the pointer from one to the other.
CRAWL_SITE_HELP_NOTE = "More crawler settings: seohead crawl-site --config-help."


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seohead", description="Headless Python SEO toolkit.")
    p.add_argument("--version", action="version", version=f"seohead {__version__}")
    subs = p.add_subparsers(dest="command", metavar="<command>")
    for cmd in COMMANDS:
        epilog = CRAWL_SITE_HELP_NOTE if cmd == "crawl-site" else None
        sp = subs.add_parser(cmd, help=f"run the {cmd} tool", epilog=epilog)
        _add_flags(sp, cmd)
    sf = subs.add_parser("sf", help="Screaming Frog crawl audit (run | tasks | doctor)")
    sf.add_argument(
        "sf_args", nargs=argparse.REMAINDER, help="arguments forwarded to the sf-analyzer CLI"
    )
    scan = subs.add_parser("scan", help="work with saved SQLite scan artifacts offline")
    scan_subs = scan.add_subparsers(dest="scan_command", required=True)
    reanalyze = scan_subs.add_parser("reanalyze", help="reanalyze retained inputs without network")
    _source_flag(reanalyze, "--input", dest="input_path", required=True, help="source SQLite scan")
    reanalyze.add_argument("--out", required=True, help="new derived SQLite scan")
    reanalyze.add_argument("--producer-build", metavar="SHA", help="current analyzer source build")
    subs.add_parser("mcp", help="run the MCP server (stdio)")
    return p


def main(argv: list[str] | None = None) -> int:
    runlog.set_interface("cli")
    args = build_parser().parse_args(argv)
    cmd = args.command
    if cmd == "scan":
        cmd = "scan-" + args.scan_command
    if not cmd:
        build_parser().print_help()
        return 0
    if cmd == "sf":
        # The crawl-audit subsystem owns its parser; preserve and forward its argument tail.
        from seohead.sf.cli import main as sf_main

        return sf_main(args.sf_args)
    if cmd == "mcp":
        from seohead.servers.mcp_server import main as mcp_main

        # mcp_main() itself catches a missing optional SDK and returns 1 after a stderr
        # diagnostic (#366), so the direct `python -m seohead.servers.mcp_server` entry
        # point advertised in that module's docstring gives the same outcome as this one.
        return mcp_main()
    if cmd == "crawl-site" and getattr(args, "config_help", False):
        _print_config_help()
        return 0
    try:
        handler_name, kwargs = _build_kwargs(cmd, args)
        report_fmt = kwargs.pop("_report", None)
        report_out = kwargs.pop("_out", None)
        if cmd == "crawl-site":
            _print_effective_rate(kwargs)
        result = handlers.HANDLERS[handler_name](**kwargs)
        if report_fmt and isinstance(result, dict) and result.get("ok"):
            # Build an optional report from the in-memory audit result. This keeps the structured
            # document identical while avoiding a manual JSON handoff between two commands.
            result["report"] = handlers.report_build(result, fmt=report_fmt, out=report_out)
    except Exception as exc:  # CLI boundary: report a concise error and exit non-zero.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    if cmd == "log-scan" and isinstance(result, dict) and result.get("anomaly_count"):
        # A contradiction is a gate, not a report: a pipeline that produced numbers which
        # disagree with each other should stop rather than publish them. 2, not 1, so a
        # caller can tell "the run contradicts itself" from "the command failed".
        return 2
    if handlers.handler_failed(result):
        # The handler could not complete its check (bad input, an unreachable host, a
        # missing dependency) and said so in the JSON rather than raising. A pipeline
        # gating on `$?` needs that reflected in the exit code too, or `ok: false` is
        # indistinguishable from success to anything reading only the exit status.
        return 1
    if isinstance(result, dict) and handlers.handler_failed(result.get("report")):
        # An explicitly requested `--report` is a deliverable in its own right: the
        # outer audit can be `ok: true` while the report it was asked to also produce
        # never got written. Keep the nested error on stdout (it already is) and only
        # change the exit status, so `report-build` run directly still gates on its
        # own top-level `ok` untouched by this branch.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
