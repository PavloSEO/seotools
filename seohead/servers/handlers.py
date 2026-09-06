"""Shared handler layer over the core, used by both the CLI and local stdio MCP server.

Each function takes/returns plain JSON-serializable objects (headless). Add new
behavior to the core + a handler here, then surface it in each face.
"""

from __future__ import annotations

from typing import Any

from seohead import runlog
from seohead.models import ParseManyResult, RobotsCheckResult
from seohead.tools import (
    asset_weight,
    clusterer,
    downloader,
    optimizer,
    parser,
    sitemap,
)
from seohead.tools import (
    headers as headers_core,
)
from seohead.tools import (
    hreflang as hreflang_core,
)
from seohead.tools import (
    links as links_core,
)
from seohead.tools import (
    robots as robots_core,
)


def handler_failed(result: Any) -> bool:
    """A handler reports its own failure to fetch, parse, or reach a provider via ``ok: False``
    in the returned dict, rather than raising (see ``docs/ARCHITECTURE.md``'s "the network never
    kills a tool" invariant). The CLI and the MCP server both call this instead of re-deriving
    the check, so the two surfaces cannot drift on what counts as a failure — see the exit-code
    contract in ``docs/USAGE.md``.
    """
    return isinstance(result, dict) and result.get("ok") is False


# SEO core is extracted BY DEFAULT (the caller can turn any field off with False).
DEFAULT_PARSE_OPTIONS: dict[str, bool] = {
    "meta": True,
    "canonical": True,
    "og": True,
    "headings": True,
    "jsonld": True,
    "links": True,
    "text": True,
}


def parse(
    url: str | None = None, urls: list[str] | None = None, options: dict[str, Any] | None = None
) -> ParseManyResult:
    targets = urls if isinstance(urls, list) else ([url] if url else [])
    if not targets:
        raise ValueError("url or urls[] required")
    opts = {**DEFAULT_PARSE_OPTIONS, **(options or {})}
    results = [parser.parse_url(str(u), opts) for u in targets]
    return {"count": len(results), "results": results}


def redirects_generate(
    redirects: list[dict] | None = None,
    fmt: str = "apache-rewrite-rule",
    default_url: str = "/",
    custom_template: str = "",
) -> dict[str, Any]:
    from seohead.tools import redirects as redirects_core

    items = redirects if isinstance(redirects, list) else []
    return {"rules": redirects_core.generate_rules(items, fmt, default_url, custom_template)}


def redirects_check(
    url: str | None = None, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    from seohead.tools import redirects as redirects_core

    return {"chain": redirects_core.check_chain(url, options or {})}


def sitemap_crawl(url: str | None = None, concurrency: int = 3) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    return sitemap.crawl(url, concurrency)


def _seed_urls_from_sitemap(url: str, sitemap: str | None, auto_discover: bool) -> dict[str, Any]:
    """Resolve and expand the sitemap(s) that should seed a crawl, if any.

    Returns ``{"sitemap_url": <first source, or None>, "sitemap_urls": [...],
    "declared": [...]}``. An explicit ``sitemap`` wins and is the sole source;
    otherwise, with ``auto_discover``, robots.txt can declare more than one
    ``Sitemap:`` directive and every one of them is independent (RFC-wise there
    is no "the" sitemap), so all are fetched and their URLs unioned. Neither
    given means no seeding — the crawl behaves exactly as it did before this
    feature existed.
    """
    from seohead.tools import sitemap as sitemap_tool

    if sitemap:
        targets = [sitemap]
    elif auto_discover:
        from seohead.tools.robots import check_robots

        targets = list(check_robots(url).get("sitemaps") or [])
    else:
        targets = []
    if not targets:
        return {"sitemap_url": None, "sitemap_urls": [], "declared": []}
    declared: list[str] = []
    seen: set[str] = set()
    for target in targets:
        expanded = sitemap_tool.crawl(target)
        for entry in expanded.get("urls") or []:
            loc = entry.get("loc")
            if loc and loc not in seen:
                seen.add(loc)
                declared.append(loc)
    return {"sitemap_url": targets[0], "sitemap_urls": targets, "declared": declared}


def _run_render_escalation(
    result: Any, rendering_config: dict[str, Any], settings: dict[str, Any]
) -> Any:
    """Bind the escalation orchestrator to a real probe and re-fetch.

    This is the interface layer's own job, same as ``crawl_site`` above:
    ``seohead.crawl.render_escalation`` stays free of Playwright and the
    network so it can be unit-tested with fake callables, and this function
    is what supplies the real ones for an actual run. "js" mode reuses
    ``render_check`` for the sample probe -- the raw-versus-rendered
    comparison #18 asks for reuse of -- and the cheaper ``render_document``
    for the full re-fetch of every page an escalated pattern contains.
    "legacy_fragment" mode needs no browser at all: probing and re-fetching
    are both a plain HTTP GET.
    """
    import os

    from seohead.crawl import render_escalation
    from seohead.recon.net import http_client, validate_url
    from seohead.tools import render as render_tool

    mode = rendering_config["mode"]
    browser_cfg = rendering_config["browser"]
    timeout = settings["http"]["timeout_seconds"]
    artifacts_dir = (
        os.path.join(settings["output"]["dir"], "render_artifacts")
        if settings["output"]["dir"]
        else None
    )

    if mode == "js":

        def probe(target: str) -> dict[str, Any]:
            probed = render_tool.render_check(
                target,
                timeout=timeout,
                wait=browser_cfg["wait_until"],
                viewport=browser_cfg["viewport"],
            )
            probed["needs_escalation"] = bool(probed.get("js_dependent"))
            return probed

        def render_fetch(target: str) -> dict[str, Any]:
            # settings["http"]["user_agent"] is what the static crawl fetched
            # every other page with, and render_document falls back to the
            # toolkit's own default when it is empty -- the same resolution
            # collect.py applies, so both halves of one crawl present one
            # identity to the origin.
            return render_tool.render_document(
                target,
                rendering_config,
                artifacts_dir=artifacts_dir,
                user_agent=settings["http"]["user_agent"],
            )

        label = "rendered"
    else:  # legacy_fragment

        def _fetch_raw(target: str) -> str:
            try:
                validate_url(target)
            except ValueError:
                return ""
            client, _ = http_client(timeout)
            try:
                return client.get(target).text
            except Exception:
                return ""
            finally:
                client.close()

        def probe(target: str) -> dict[str, Any]:
            html = _fetch_raw(target)
            return {
                "ok": bool(html),
                "needs_escalation": render_tool.legacy_fragment_target(target, html) is not None,
                "empty_shell": render_tool.detect_empty_shell(html),
            }

        def render_fetch(target: str) -> dict[str, Any]:
            html = _fetch_raw(target)
            escaped = render_tool.legacy_fragment_target(target, html)
            if not escaped:
                return {"ok": False}
            fetched_html = _fetch_raw(escaped)
            if not fetched_html:
                return {"ok": False}
            return {"ok": True, "url": target, "final_url": escaped, "html": fetched_html}

        label = "legacy_fragment"

    return render_escalation.escalate(
        result.pages,
        rendering_config,
        probe=probe,
        render_fetch=render_fetch,
        representation_label=label,
    )


def _sitemap_comparable_pages(result: Any, start_url: str) -> list[str]:
    """The pages an XML sitemap of this site is supposed to declare.

    A sitemap lists indexable HTML pages of one host. Comparing it against every link
    destination the crawl recorded answers a different question and answers it wrongly: on a
    live 124-page site that comparison produced 392 URL_NOT_IN_SITEMAP findings — 307 .jpg and
    55 .webp files the gallery links to directly, five off-host links, and 30 URLs the crawl
    never fetched — which was 74% of the entire report (issue #94). So the population is built
    here from what the crawl actually fetched:

    * fetched at all, with a real status — a URL nobody requested is evidence of nothing;
    * 2xx — a 404 or a redirect is not a page a sitemap should declare;
    * HTML by its own Content-Type — not an image, a PDF or a feed;
    * on the start URL's host — a sitemap may not declare someone else's domain;
    * indexable — a noindex page (meta robots or X-Robots-Tag) is deliberately excluded,
      and so is one this same crawl's own evidence marks ``robots_blocked`` (#316):
      ``build_evidence()``'s ``_indexability()`` already projects a robots-blocked URL as
      non-indexable, and a report-only robots policy still fetches and links the page, so
      leaving it comparable here let one page be simultaneously non-indexable (BLOCKED_BY_ROBOTS)
      and reported as an indexable page the sitemap forgot (URL_NOT_IN_SITEMAP) -- two
      first-class projections of the same crawl contradicting each other.

    The 2xx+HTML re-check below looks like the gap ``AuditContext.html_pages`` had before
    issue #133 (it isn't calling that method), but ``result.pages`` here are
    ``seohead.crawl.collect.PageRecord`` from a native crawl, not ``sf.core.models.Page`` — a
    different type with no ``AuditContext`` in scope at this point, and this population also
    needs the host and indexable filters ``html_pages`` never applied. Fixing #133 narrowed
    ``html_pages`` to match this function's already-correct logic; it did not make this
    re-check redundant, since there is no shared call to route through.
    """
    from urllib.parse import urlsplit

    from seohead.recon.net import normalize_url as _normalize_host_url
    from seohead.tools.parser import robots_directives

    try:
        host = urlsplit(_normalize_host_url(start_url)).netloc.lower()
    except Exception:
        host = ""

    robots_blocked = set(getattr(result, "robots_blocked", None) or [])

    comparable: list[str] = []
    for page in getattr(result, "pages", []) or []:
        status = getattr(page, "status_code", None)
        if status is None or not (200 <= status < 300):
            continue
        if not getattr(page, "is_html", False):
            continue
        if host and urlsplit(page.url).netloc.lower() != host:
            continue
        if "noindex" in robots_directives(page.meta_robots, page.x_robots):
            continue
        if page.url in robots_blocked:
            continue
        comparable.append(page.url)
    return comparable


def _rewrite_pages_sidecar(path: str, pages: list[Any]) -> None:
    """Replace the streamed page sidecar with every page's current field values.

    ``path`` is the same file the spider appended to line-by-line as pages were
    fetched (``pages_resume_path`` in ``crawl_site``), written before render
    escalation exists to mutate ``result.pages`` in place. Called only once
    escalation has actually re-fetched something, so the file catches up to
    whatever changed rather than being rewritten on every run for nothing.
    Written to a temp file in the same directory first and swapped in with
    ``os.replace``, which POSIX and Windows both guarantee is atomic within one
    filesystem, so a process killed mid-write leaves the previous, still
    internally-consistent version in place rather than a half-written file
    (issue #244's third acceptance criterion).
    """
    import contextlib
    import dataclasses
    import json
    import os
    import tempfile

    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".pages-rewrite-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for page in pages:
                fh.write(json.dumps(dataclasses.asdict(page)) + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_path)
        raise


def _segment_counts(
    pages: list[Any], issues: list[dict[str, Any]], scope_config: dict[str, Any]
) -> dict[str, dict[str, int]]:
    """Page and issue counts per named segment (#358).

    Every page and every issue -- including an audit-wide finding with no
    single ``target_url``, such as ``TITLE_TEMPLATED`` -- is assigned to
    exactly one bucket: a declared segment, or the built-in "default", so
    these counts always sum to the ungrouped totals reported elsewhere in
    the same audit (#441).

    The bucketing itself is delegated to ``sf.core.segments.segment_report``,
    the tested engine that already carries this exact invariant (#456),
    rather than re-deriving it here with ``Scope.segment_for`` alone, which
    only ever looked at issues that had a ``target_url``.
    """
    from urllib.parse import urlsplit

    from seohead.crawl.spider import Scope
    from seohead.sf.core.segments import UNSEGMENTED, assign_segments

    scope_rules = Scope.from_config(scope_config)
    if not scope_rules.segments:
        return {}

    engine_segments = [
        {
            "name": rule.name,
            "rules": [
                r
                for r in (
                    {"op": "prefix", "field": "path", "value": rule.prefix}
                    if rule.prefix
                    else None,
                    {"op": "eq", "field": "host", "value": rule.host} if rule.host else None,
                    {"op": "regex", "field": "url", "value": rule.pattern.pattern}
                    if rule.pattern
                    else None,
                )
                if r is not None
            ],
        }
        for rule in scope_rules.segments
    ]

    def record(url: str) -> dict[str, str]:
        parts = urlsplit(url)
        return {"url": url, "path": parts.path, "host": (parts.hostname or "").lower()}

    def bucket(name: str | None) -> dict[str, int]:
        bucket_name = "default" if name in (None, UNSEGMENTED) else name
        return counts.setdefault(bucket_name, {"pages": 0, "issues": 0})

    counts: dict[str, dict[str, int]] = {}

    # Pages decide segment membership; no rule here references another segment
    # (op="segment"), so an issue's target can be classified on its own,
    # against the same rule set, without needing to belong to the page pool.
    page_records = [record(page.url) for page in pages]
    page_primary = assign_segments(page_records, engine_segments)["primary"]
    for page_record in page_records:
        bucket(page_primary.get(page_record["url"]))["pages"] += 1

    for issue in issues:
        target = issue.get("target_url")
        if target:
            issue_primary = assign_segments([record(target)], engine_segments)["primary"]
            bucket(issue_primary.get(target))["issues"] += 1
        else:
            # An audit-wide finding with no single target still counts
            # somewhere, so segment sums keep matching issues_total (#441).
            bucket(None)["issues"] += 1
    return counts


def crawl_site(
    url: str | None = None,
    urls: list[str] | None = None,
    config: str | None = None,
    max_urls: int | None = None,
    max_depth: int | None = None,
    min_delay: float | None = None,
    concurrency: int | None = None,
    robots: str | None = None,
    out_dir: str | None = None,
    sitemap: str | None = None,
    scan_out: str | None = None,
    producer_build: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Crawl a site from a start URL, or fetch an explicit list, then audit it.

    The interface layer is where collector and analyzer are allowed to meet:
    ``seohead.crawl`` gathers evidence and never imports the analyzer,
    ``seohead.sf`` judges it and never imports the collector, and this function
    hands the projection from one to the other.

    ``min_delay`` defaults to half a second because the target is somebody's
    production site: polite by accident beats fast by accident.

    ``sitemap`` (or ``sitemaps.auto_discover`` in ``config``) seeds the crawl
    from a sitemap's declared URLs, in addition to following links from
    ``url``, and reconciles the two sources — see
    ``seohead.crawl.reconcile.reconcile_sitemap``. The same URL also feeds
    ``seohead.sf.core.sitemap_coverage.run_sitemap``, which independently
    re-fetches it to check the sitemap protocol's own limits and whether
    robots.txt declares it; with none given, those checks skip by name
    rather than guess at a default sitemap location.
    """
    import contextlib
    import os

    from seohead.crawl import cache as http_cache
    from seohead.crawl import settings as crawl_config
    from seohead.crawl.collect import collect_urls
    from seohead.crawl.spider import crawl_site as _spider

    if not url and not urls:
        raise ValueError("url or urls required")

    # Defaults, then file, then environment, then these explicit arguments.
    # ``overrides`` carries whatever the caller reached for by dotted path -- the
    # CLI's --set and --max-urls-per-second. Only a named argument that was
    # actually given wins over it: updating with None would erase a caller's
    # override and silently fall back to the default, which is how a rate cap
    # becomes a no-op.
    resolved_overrides: dict[str, Any] = dict(overrides or {})
    for path, value in (
        ("limits.max_urls", max_urls),
        ("limits.max_depth", max_depth),
        ("speed.min_delay_seconds", min_delay),
        ("speed.concurrency", concurrency),
        ("robots.policy", robots),
        ("output.dir", out_dir),
    ):
        if value is not None:
            resolved_overrides[path] = value
    settings = crawl_config.load(config, overrides=resolved_overrides)
    if settings.get("resources", {}).get("fetch") and not scan_out:
        raise ValueError("resources.fetch requires a SQLite scan_out artifact")
    if scan_out:
        if not url or urls:
            raise ValueError(
                "SQLite scan mode requires a start URL; list mode remains directory-based"
            )
        if out_dir or settings["output"]["dir"]:
            raise ValueError("scan_out and a legacy output directory cannot be combined")
        from seohead.servers.scan_handlers import crawl_site_scan

        return crawl_site_scan(
            url,
            scan_out=scan_out,
            settings=settings,
            sitemap=sitemap,
            producer_build=producer_build,
        )
    out_dir = settings["output"]["dir"] or None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    # The human-readable export: absent whenever the operator turned it off. Only
    # ``collect_urls`` (the list-mode branch below, which has no resume mechanism of
    # its own) is allowed to treat this as the whole story.
    pages_export_path = (
        os.path.join(out_dir, "pages.jsonl")
        if out_dir and settings["output"]["write_pages_jsonl"]
        else None
    )
    # Tied to out_dir alone, not the write_pages_jsonl toggle -- same shape as
    # links_path below. spider.crawl_site's resume mechanism reloads previously
    # fetched pages from whatever path it was given as out_path; passing None
    # here whenever the export was off used to defeat that reload silently, so a
    # resumed run reported fewer pages than the interrupted one had already
    # found (issue #242). When the export is on the two paths are the same file;
    # when it is off, a private sidecar the operator never asked to see still
    # carries what a resume needs, and is removed once there is nothing left to
    # resume (see the "finished" cleanup after the spider call below).
    pages_resume_path = pages_export_path or (
        os.path.join(out_dir, ".pages_resume.jsonl") if out_dir else None
    )
    # Tied to out_dir alone, not the write_pages_jsonl toggle: this sidecar is what makes a
    # resumed run's result.links whole again (see spider.crawl_site), a correctness need
    # distinct from whether the operator also wants pages.jsonl as a human-readable export.
    links_path = os.path.join(out_dir, "links.jsonl") if out_dir else None
    # Same "tied to out_dir, gated by its own toggle" shape as pages_export_path: a
    # decision log is a diagnostic artifact, not something a resumed run
    # depends on (issue #134).
    decisions_path = (
        os.path.join(out_dir, "decisions.jsonl")
        if out_dir and settings["output"]["write_decisions_jsonl"]
        else None
    )
    max_seconds = settings["limits"]["max_crawl_seconds"]
    # One cache per run, shared by every worker thread a concurrent crawl starts — see
    # seohead.crawl.cache for the freshness policy and seohead.crawl.settings for cache.mode /
    # cache.invalidate. A directory this session cannot trust (missing, world-writable) degrades
    # to no cache rather than failing the run.
    cache = http_cache.build(
        http_cache.resolve_dir(),
        mode=settings["cache"]["mode"],
        invalidate=settings["cache"]["invalidate"],
    )

    sitemap_seed = {"sitemap_url": None, "sitemap_urls": [], "declared": []}
    if url and (sitemap or settings["sitemaps"]["auto_discover"]):
        sitemap_seed = _seed_urls_from_sitemap(url, sitemap, settings["sitemaps"]["auto_discover"])

    if url:
        result = _spider(
            url,
            max_urls=settings["limits"]["max_urls"],
            max_depth=settings["limits"]["max_depth"],
            max_seconds=max_seconds,
            min_delay=settings["speed"]["min_delay_seconds"],
            timeout=settings["http"]["timeout_seconds"],
            robots_policy=settings["robots"]["policy"],
            scope=settings["scope"],
            seed_urls=sitemap_seed["declared"] or None,
            out_path=pages_resume_path,
            links_path=links_path,
            decisions_path=decisions_path,
            credential_headers=settings["http"]["credential_headers"],
            # Checkpointed only when there is somewhere durable to put it; a
            # crawl with no out_dir has nothing to resume into anyway.
            state_path=os.path.join(out_dir, "crawl_state.json") if out_dir else None,
            config_fingerprint=crawl_config.fingerprint(settings),
            concurrency=settings["speed"]["concurrency"],
            max_response_bytes=settings["limits"]["max_response_bytes"],
            max_url_length=settings["limits"]["max_url_length"],
            max_query_variants_per_path=settings["limits"]["max_query_variants_per_path"],
            retry_on_timeout=settings["http"]["retry_on_timeout"],
            user_agent=settings["http"]["user_agent"],
            robots_token=settings["robots"]["user_agent_token"],
            unavailable_means_stop=settings["robots"]["unavailable_means_stop"],
            stop_after_consecutive_timeouts=settings["speed"]["stop_after_consecutive_timeouts"],
            max_delay_seconds=settings["speed"]["max_delay_seconds"],
            follow_nofollow=settings["discovery"]["follow_nofollow"],
            classify_links=settings["link_position"]["classify"],
            link_position_rules=settings["link_position"]["rules"] or None,
            cache=cache,
            extra_request_headers=settings["http"]["headers"] or None,
            adaptive=settings["speed"]["adaptive"],
            store_hyperlinks=settings["discovery"]["hyperlinks"]["store"],
            crawl_hyperlinks=settings["discovery"]["hyperlinks"]["crawl"],
            store_external_links=settings["discovery"]["external"]["store"],
            crawl_redirects=settings["discovery"]["redirects"]["crawl"],
            capture_link_attributes=settings["link_attributes"]["capture"],
        )
        # Nothing left to resume into, so the private sidecar (used only when the
        # human-readable export was off) would otherwise linger as a hidden, ever
        # more stale copy of pages.jsonl's data next to a finished run's output.
        if (
            pages_resume_path
            and pages_resume_path != pages_export_path
            and result.finish_reason == "finished"
        ):
            with contextlib.suppress(FileNotFoundError):
                os.remove(pages_resume_path)
        discovery = {
            "mode": "spider",
            # #332: named here too, matching list mode -- a robots-blocked count
            # without the policy that produced it is not self-explanatory.
            "directive_policy": settings["robots"]["policy"],
            "max_depth_reached": result.max_depth_reached,
            "links_seen": len(result.links),
            "excluded": result.excluded,
            "robots_note": result.robots_note,
            "robots_blocked": len(result.robots_blocked),
            "crawl_delay_applied": result.crawl_delay_applied,
            "effective_delay_seconds": round(result.effective_delay, 3),
            "effective_concurrency": result.effective_concurrency,
            "resume_note": result.resume_note,
            "sitemap_url": sitemap_seed["sitemap_url"],
            "sitemap_urls": sitemap_seed["sitemap_urls"],
            "sitemap_seeded": len(result.seed_urls),
        }
        if settings["scope"]["segments_only"]:
            # #358's acceptance criterion: a crawl scoped to a segment must say so
            # in its own run output, not leave it to be inferred from which URLs
            # happen to be missing.
            discovery["segments_only"] = settings["scope"]["segments_only"]
    else:
        result = collect_urls(
            urls or [],
            max_urls=settings["limits"]["max_urls"],
            max_seconds=max_seconds,
            min_delay=settings["speed"]["min_delay_seconds"],
            timeout=settings["http"]["timeout_seconds"],
            out_path=pages_export_path,
            credential_headers=settings["http"]["credential_headers"],
            max_response_bytes=settings["limits"]["max_response_bytes"],
            max_url_length=settings["limits"]["max_url_length"],
            retry_on_timeout=settings["http"]["retry_on_timeout"],
            user_agent=settings["http"]["user_agent"],
            stop_after_consecutive_timeouts=settings["speed"]["stop_after_consecutive_timeouts"],
            max_delay_seconds=settings["speed"]["max_delay_seconds"],
            cache=cache,
            extra_request_headers=settings["http"]["headers"] or None,
            adaptive=settings["speed"]["adaptive"],
            robots_policy=settings["robots"]["policy"],
            robots_token=settings["robots"]["user_agent_token"],
            resolve_redirect_destination=settings["discovery"]["resolve_redirect_destination"],
        )
        discovery = {
            "mode": "list",
            # #21: the configured policy must be stated, not merely applied — a report that
            # says nothing here is indistinguishable from one that silently ignored it.
            "directive_policy": settings["robots"]["policy"],
            "robots_blocked": len(result.robots_blocked),
        }

    response, _audit = _audit_crawl_result(
        result,
        settings=settings,
        url=url,
        sitemap_seed=sitemap_seed,
        discovery=discovery,
        out_dir=out_dir,
        pages_resume_path=pages_resume_path,
    )
    return response


def _audit_crawl_result(
    result,
    *,
    settings,
    url,
    sitemap_seed,
    discovery,
    out_dir=None,
    pages_resume_path=None,
    stored_scan=None,
    stored_sitemap=None,
    offline: bool = False,
    captured_render_summary: dict[str, Any] | None = None,
):
    """Run the existing native analysis over a complete, admitted population."""
    import json
    import os
    from datetime import datetime, timezone
    from urllib.parse import urlsplit

    from seohead.crawl import settings as crawl_config
    from seohead.crawl.evidence import build_evidence
    from seohead.crawl.reconcile import reconcile_sitemap
    from seohead.sf.config import load_config
    from seohead.sf.core.aggregate import aggregate
    from seohead.sf.core.context import AuditContext
    from seohead.sf.core.heuristics import run_heuristics
    from seohead.sf.core.inlinks import run_inlinks
    from seohead.sf.core.loader import LoadedExports
    from seohead.sf.core.rules import run_rules
    from seohead.sf.core.sitemap_coverage import run_sitemap

    if type(offline) is not bool:
        raise ValueError("offline must be a boolean")
    if captured_render_summary is not None and not isinstance(captured_render_summary, dict):
        raise ValueError("captured_render_summary must be an object when supplied")

    requires_rendering = False
    requires_rendering_reason = ""
    render_summary: dict[str, Any] = {}
    if url:
        from seohead.crawl import render_escalation
        from seohead.recon.net import normalize_url

        start_norm = normalize_url(url)
        rendering_config = settings["rendering"]
        escalation = None
        if offline and rendering_config["mode"] != "raw":
            render_summary = (
                dict(captured_render_summary)
                if captured_render_summary is not None
                else {
                    "mode": rendering_config["mode"],
                    "state": "unavailable",
                    "reason": "offline reanalysis has no captured render summary",
                }
            )
        elif rendering_config["mode"] != "raw" and result.pages:
            if stored_scan is None:
                escalation = _run_render_escalation(result, rendering_config, settings)
                render_escalation.apply_rendered_evidence(result.pages, result.links, escalation)
                # The spider already streamed pages_resume_path during the crawl, before
                # this escalation existed to mutate result.pages -- so whichever pages
                # were re-fetched now have audit-and-memory evidence the file on disk
                # does not, and a resumed run or a pages.jsonl reader would see stale
                # static evidence next to an audit.json that says rendered (#244).
                if pages_resume_path and escalation.render_requests:
                    _rewrite_pages_sidecar(pages_resume_path, result.pages)
            else:
                from seohead.crawl.sqlite_render import run_render_escalation

                # The SQLite path commits each DOM and its observations before
                # admitting it to this transient audit view.  It deliberately
                # leaves HTML out of ``EscalationResult`` so a large scan never
                # accumulates every serialized DOM in memory.
                escalation = run_render_escalation(stored_scan, result, settings)
                if not offline:
                    from seohead.crawl.sqlite_resources import capture_resources

                    capture_resources(stored_scan, settings)
                coverage = stored_scan.con.execute(
                    "SELECT crawl_partial,limitations_json FROM scan"
                ).fetchone()
                result.partial = bool(coverage[0])
                result.limitations = json.loads(coverage[1])
            render_summary = {
                "mode": escalation.mode,
                "patterns_sampled": escalation.patterns_sampled,
                "patterns_escalated": escalation.patterns_escalated,
                "probe_requests": escalation.probe_requests,
                "render_requests": escalation.render_requests,
                "render_budget_exhausted": escalation.render_budget_exhausted,
                # Set independently of render_budget_exhausted (#198): max_render_urls and
                # max_render_seconds are two different operator-set limits that run out for
                # unrelated reasons, and a report must say which one cut this run short.
                "time_budget_exhausted": escalation.time_budget_exhausted,
                # Which escalated patterns the budget actually reached, and
                # which it ran out on before finishing -- patterns_escalated
                # alone cannot tell the two apart (#147).
                "render_counts": escalation.render_counts,
                "patterns_partially_rendered": escalation.patterns_partially_rendered,
                "patterns_unprobed": escalation.patterns_unprobed,
            }

        # Re-evaluated after escalation so a run that actually renders its
        # start page is judged on that rendered evidence, not the raw
        # snapshot escalation was meant to fix -- but the gate itself is
        # static-only (see start_page_gate) so it still fires for "raw" mode,
        # which has no render to fall back on.
        start_record = next((p for p in result.pages if p.url == start_norm), None)
        if start_record is not None:
            # Stored scans reconstruct this from the retained *static*
            # document before analysis.  Rendering is a later representation
            # and must not trigger a new request merely to satisfy the raw
            # start-page gate on resume.
            rendered_start = (
                escalation.rendered.get(start_norm) if escalation and stored_scan is None else None
            )
            start_html = (
                getattr(result, "_rendered_start_html", None)
                or (rendered_start or {}).get("html")
                or result.start_page_evidence.get("html", "")
            )
            gate = render_escalation.start_page_gate(
                start_norm,
                max(start_record.outlinks - start_record.external_outlinks, 0),
                start_html,
            )
            requires_rendering, requires_rendering_reason = gate.requires_rendering, gate.reason

    stored_graph_available = False
    if stored_scan is None:
        evidence = build_evidence(result)
    else:
        from seohead.crawl.sql_graph import StoredGraph

        stored_graph_available = (
            stored_scan.con.execute("SELECT 1 FROM links LIMIT 1").fetchone() is not None
        )
        with StoredGraph(stored_scan.con) as graph:
            counts = (
                {
                    item["url"]: (item["inlinks"], item["unique_inlinks"])
                    for item in graph.iter_inlink_counts()
                }
                if stored_graph_available
                else None
            )
        evidence = build_evidence(
            result, inlink_counts=counts, stored_graph_available=stored_graph_available
        )
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])

    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))
    run_rules(ctx)
    # Same pipeline the Screaming Frog export path runs (seohead/sf/core/audit.py)
    # -- omitting it here left every inlinks-derived check (anchor text, hreflang,
    # link score, discovery path, inlink composition, insecure subresources)
    # neither fired nor skipped: silently uninvoked rather than honestly absent
    # (issue #128). ``all_inlinks`` is populated above from the crawl's own
    # hyperlink graph when one exists (see crawl/evidence.py), so the checks it
    # feeds now answer for real instead of only reaching their skip branch.
    if stored_graph_available:
        from seohead.sf.core.inlinks import _site_host
        from seohead.sf.core.normalize import norm_url
        from seohead.storage.analysis_graph import AnalysisGraph

        with AnalysisGraph(stored_scan.con, normalize=norm_url, site_host=_site_host(ctx)) as graph:
            ctx.graph_access = graph
            run_inlinks(ctx)
        ctx.graph_access = None
    else:
        run_inlinks(ctx)
    # Same gap, two more modules (issue #165): DOM size, HTML weight, templated
    # titles and the near-duplicate/exact-duplicate heuristic fallback all live in
    # heuristics.py and were never reached from a crawl either. DOM depth/nodes and
    # the near-duplicate fallback need HTML stored to disk (``input.html_store_dir``),
    # which a native crawl never writes -- they land on their own existing "no
    # stored HTML" skip branch rather than gaining new evidence here. HTML weight
    # and templated-title detection need only Size (bytes), Word Count and Title,
    # which build_evidence already puts on every page, so those genuinely fire.
    run_heuristics(ctx)

    # ``run_sitemap`` covers the sitemap-protocol and robots.txt checks that need a
    # live network fetch (SITEMAP_TOO_LARGE, SITEMAP_STALE_LASTMOD, SITEMAP_NOT_IN_ROBOTS,
    # ROBOTS_BLOCKS_RESOURCES, ...) and, before this, was never called from crawl_site
    # either (issue #165) -- those checks were silently uninvoked on every native crawl,
    # sitemap or not. It is given the same sitemap URL used to seed this crawl, if any;
    # with none, it reaches its own honest per-check skip branches instead of guessing
    # at a default sitemap location. Its own declared-vs-crawled comparison
    # (SITEMAP_DESYNC and the "in_sitemap_and_linked"-shaped summary keys) is cruder
    # than the dedicated reconciliation below, so those three summary keys are
    # overwritten by it further down rather than the other way around.
    if offline:
        reason = "offline reanalysis has no retained sitemap XML, robots.txt, or lastmod evidence"
        for check in (
            "SITEMAP_NOT_IN_ROBOTS",
            "ROBOTS_BLOCKS_RESOURCES",
            "SITEMAP_FETCH_INCOMPLETE",
            "SITEMAP_TOO_MANY_URLS",
            "SITEMAP_TOO_LARGE",
            "SITEMAP_URL_DUPLICATED",
            "SITEMAP_STALE_LASTMOD",
        ):
            ctx.skip(check, reason)
        measured: dict[str, Any] = {}
    else:
        measured = run_sitemap(
            ctx,
            sitemap_url=sitemap_seed["sitemap_url"],
            sitemap_urls=sitemap_seed["sitemap_urls"],
            compare_with_crawl=stored_scan is None and not sitemap_seed["declared"],
            crawl_partial=bool(getattr(result, "partial", False)),
        )
    # Only surfaced when something was actually measured. run_sitemap always
    # returns its keys, and a run with no sitemap at all would otherwise report
    # urls_in_sitemap: 0 -- which reads as "the sitemap is empty" when the truth
    # is "there was no sitemap". The checks themselves still ran and skipped by
    # name above; it is the summary that must not invent a zero.
    sitemap_summary: dict[str, Any] = (
        dict(measured)
        if (measured.get("sitemaps") or measured.get("declared_in_robots") is not None)
        else {}
    )
    if stored_scan is not None and (stored_sitemap is None or not stored_sitemap.available):
        reason = (
            stored_sitemap.reason if stored_sitemap is not None else "no saved sitemap declarations"
        )
        for check in ("SITEMAP_ORPHAN", "URL_NOT_IN_SITEMAP", "SITEMAP_DESYNC"):
            ctx.skip(check, reason)
        sitemap_summary.update(reconciliation_available=False, reconciliation_reason=reason)
    if sitemap_seed["declared"] or (stored_sitemap is not None and stored_sitemap.available):
        # "Reached by following links" — not merely fetched, since a seeded
        # URL is fetched regardless of whether anything links to it. Three
        # disjoint facts, reported under the check ids the Screaming Frog
        # pipeline already uses for the same distinction (SITEMAP_ORPHAN,
        # URL_NOT_IN_SITEMAP), so audit.json has one schema either way.
        if stored_sitemap is None:
            observed = [edge.destination for edge in result.links]
            reconciled = reconcile_sitemap(
                sitemap_seed["declared"], observed, _sitemap_comparable_pages(result, url)
            )
        else:
            reconciled = stored_sitemap.materialize(100_000)
            reconciled.pop("available", None)
            reconciled.pop("reason", None)
            reconciled.pop("crawl_partial", None)
        reconciled["sitemap_url"] = sitemap_seed["sitemap_url"]
        reconciled["sitemap_urls"] = sitemap_seed["sitemap_urls"]
        for orphan_url in reconciled["in_sitemap_not_linked"]:
            ctx.add("SITEMAP_ORPHAN", target_url=orphan_url, details={"in_sitemap": True})
        for extra_url in reconciled["linked_not_in_sitemap"]:
            ctx.add("URL_NOT_IN_SITEMAP", target_url=extra_url)
        # The site-level verdict belongs to whichever module did the comparison,
        # and here that is this one -- run_sitemap was told to skip it by name
        # (compare_with_crawl above) precisely so the two never answer the same
        # question with two different degrees of rigour. The per-URL findings
        # above say which URLs disagree; this says whether the disagreement is
        # large enough to be a fact about the site rather than a handful of URLs.
        comparable_total = max(
            len(reconciled["in_sitemap_and_linked"]) + len(reconciled["linked_not_in_sitemap"]), 1
        )
        crawl_only_pct = round(100 * len(reconciled["linked_not_in_sitemap"]) / comparable_total, 1)
        sitemap_only_pct = round(
            100
            * len(reconciled["in_sitemap_not_linked"])
            / max(
                len(sitemap_seed["declared"])
                if stored_sitemap is None
                else stored_sitemap.declared_raw_count,
                1,
            ),
            1,
        )
        threshold = ctx.thresholds["sitemap_desync_pct_warn"]
        if getattr(result, "partial", False):
            # A URL-limited or otherwise incomplete native crawl can still count sitemap
            # URLs it never reached as "unlinked" -- but the unfetched frontier may hold
            # exactly the link that would prove them linked, so a thresholded site-wide
            # verdict from this graph is unsound (#362). The per-URL findings above
            # (SITEMAP_ORPHAN, URL_NOT_IN_SITEMAP) still fire on what was actually
            # observed; only the whole-graph percentage verdict is withheld.
            ctx.skip(
                "SITEMAP_DESYNC",
                "crawl is partial: a sitemap-versus-link-graph verdict cannot be proven "
                "when the crawl did not reach every URL",
            )
        elif crawl_only_pct >= threshold or sitemap_only_pct >= threshold:
            ctx.add(
                "SITEMAP_DESYNC",
                target_url=url,
                details={
                    "in_crawl_not_in_sitemap": len(reconciled["linked_not_in_sitemap"]),
                    "in_sitemap_not_in_crawl": len(reconciled["in_sitemap_not_linked"]),
                    "crawl_not_in_sitemap_pct": crawl_only_pct,
                    "sitemap_not_in_crawl_pct": sitemap_only_pct,
                    "examples_missing_from_sitemap": reconciled["linked_not_in_sitemap"][:20],
                    "examples_in_sitemap_not_crawled": reconciled["in_sitemap_not_linked"][:20],
                },
            )
        sitemap_summary.update(reconciled)

    # Same "native crawl produces evidence the SF export never carries" shape
    # as the sitemap reconciliation above: classification runs inside the
    # spider's own link recording (see crawl/spider.py), never through the
    # analyzer, and only meets the SF-shaped audit here.
    link_position: dict[str, Any] = {}
    if url and settings["link_position"]["classify"]:
        from urllib.parse import urlsplit

        from seohead.crawl.linkgraph import inlink_composition

        if stored_scan is None:
            # The crawled site's own host: the in-memory graph holds every edge the
            # crawl recorded, external destinations included, and without a host it
            # would advise adding a contextual link to somebody else's site (#208).
            link_position = inlink_composition(result.links, urlsplit(url).hostname or "")
        else:
            # The stored graph needs no equivalent: its population is built from
            # destinations that appear in the pages table, so an uncrawled external
            # URL is outside it by construction -- a stricter filter than a host
            # match, and it labels itself "crawled_destinations" rather than
            # claiming to have judged the whole graph.
            from seohead.crawl.sql_graph import StoredGraph
            from seohead.crawl.sql_graph_output import composition

            with StoredGraph(stored_scan.con) as graph:
                link_position = composition(graph, max_pages=100_000)
        # "Never linked from body content" is a claim about every inlink a page
        # has -- exactly the shape #246 asks graph-wide claims to withhold on a
        # partial crawl: the missing frontier could still hold the one content
        # link that would clear this page. Unlike LOW_LINK_SCORE and its three
        # siblings (see aggregate.GRAPH_WIDE_FINDING_CHECKS), this is computed
        # here rather than in seohead.sf, so it needs its own guard instead of
        # joining that withholding pass.
        if result.partial:
            ctx.skip(
                "INLINK_BOILERPLATE_ONLY",
                "crawl is partial: 'never linked from body content' cannot be "
                "proven when the crawl did not reach every URL",
            )
        else:
            for page in link_position["pages"]:
                if page["boilerplate_only"]:
                    ctx.add(
                        "INLINK_BOILERPLATE_ONLY",
                        target_url=page["url"],
                        occurrences_count=page["inlinks_total"],
                        details={"by_position": page["by_position"]},
                    )

    # Same shape again (issue #125): pure functions over the crawl's own LinkEdge/FormEdge
    # evidence, never through the SF-export analyzer. Localhost outlinks, the per-target
    # follow/nofollow mix, and both form checks need only fields every crawl already
    # records; the cross-origin and protocol-relative checks additionally need
    # link_attributes.capture (off by default -- see its own docstring).
    if url:
        from contextlib import nullcontext

        from seohead.crawl import link_findings
        from seohead.crawl.sql_graph import StoredGraph

        with StoredGraph(stored_scan.con) if stored_scan else nullcontext(None) as graph:
            crawl_host = urlsplit(start_norm).hostname or ""
            for item in (
                graph.iter_localhost_findings()
                if graph
                else link_findings.outlinks_to_localhost(result.links)
            ):
                ctx.add("OUTLINK_TO_LOCALHOST", target_url=item["target_url"], details=item)
            for dest in (
                graph.iter_follow_and_nofollow(crawl_host)
                if graph
                else link_findings.follow_and_nofollow_inlinks(result.links, crawl_host)
            ):
                ctx.add("FOLLOW_AND_NOFOLLOW_INLINKS", target_url=dest)
            for item in (
                graph.iter_insecure_forms()
                if graph
                else link_findings.form_url_insecure(result.forms)
            ):
                ctx.add("FORM_URL_INSECURE", target_url=item["target_url"], details=item)
            for item in (
                graph.iter_password_forms_on_http()
                if graph
                else link_findings.forms_on_http_pages_with_password(result.forms)
            ):
                ctx.add("FORM_ON_HTTP_URL", target_url=item["target_url"], details=item)
            if settings["link_attributes"]["capture"]:
                for item in (
                    graph.iter_unsafe_cross_origin()
                    if graph
                    else link_findings.unsafe_cross_origin_links(result.links)
                ):
                    ctx.add("UNSAFE_CROSS_ORIGIN_LINK", target_url=item["target_url"], details=item)
                for item in (
                    graph.iter_protocol_relative()
                    if graph
                    else link_findings.protocol_relative_links(result.links)
                ):
                    ctx.add("PROTOCOL_RELATIVE_LINK", target_url=item["target_url"], details=item)

    audit = aggregate(
        ctx,
        {
            "input_mode": "crawl" if url else "crawl-list",
            "source": url or "url-list",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "collector": "seohead.crawl",
            "crawl_partial": result.partial,
            "crawl_stopped_reason": result.stopped_reason,
            # Categorical and always present, unlike stopped_reason which is
            # only a sentence when something went wrong.
            "crawl_finish_reason": result.finish_reason,
            "crawl_resumed": result.resumed,
            # Resolved values of every setting that can change what was found.
            # Without these two reports on the same site are not comparable.
            "crawl_config": crawl_config.manifest(settings),
            "effective_max_requests_per_second": (
                "unbounded"
                if settings["speed"]["min_delay_seconds"] == 0
                else crawl_config.effective_request_rate(settings)
            ),
            # "The site is fine" and "the site was fine when we last looked" are different
            # claims — cache_replay says which one this report can support, and cache_stats
            # says how much of the corpus was measured now versus remembered.
            "cache_replay": result.cache_replay,
            "cache_stats": result.cache_stats,
            # Whether this run is a false-green that must not reach a health
            # score (#18), and the escalation that ran (if any) to check.
            "requires_rendering": requires_rendering,
            "requires_rendering_reason": requires_rendering_reason,
            "render_escalation": render_summary or None,
        },
        {},
        sitemap_summary,
    ).to_json()
    # Page and issue counts per named segment (#358) -- only when the operator
    # actually declared segments, so a plain crawl's audit.json is unchanged.
    audit["segments"] = (
        _segment_counts(result.pages, audit["issues"], settings["scope"])
        if settings["scope"]["segments"]
        else {}
    )

    tasks_written: dict[str, str] = {}
    if out_dir:
        with open(os.path.join(out_dir, "audit.json"), "w", encoding="utf-8") as fh:
            json.dump(audit, fh, ensure_ascii=False, indent=2)
        if settings["output"]["write_tasks"]:
            # build_tasks has always taken an audit document, and a native crawl
            # has always produced one -- the two were simply never joined, so a
            # crawl done without Screaming Frog produced findings and no list of
            # what to do about them. Same pipeline `sf run --tasks` drives, over
            # this crawl's own audit: no network, no second pass.
            from seohead.sf.tasks import build_tasks, write_tasks

            # None, not this crawl's own config: tasks_pipeline lives in the
            # sf config (seohead/sf/config.py), a different document from the
            # crawler settings, and passing the crawler's path here would fail
            # on the first .get(). Defaults it is, until someone asks for the
            # two to be joined.
            backlog = build_tasks(audit, None)
            json_path, md_path = write_tasks(
                backlog,
                os.path.join(out_dir, "tasks.json"),
                os.path.join(out_dir, "tasks.md"),
            )
            tasks_written = {"tasks_json": json_path, "tasks_md": md_path}

    response = {
        "urls_collected": len(result.pages),
        "tasks": tasks_written,
        "partial": result.partial,
        "stopped_reason": result.stopped_reason,
        "finish_reason": result.finish_reason,
        "resumed": result.resumed,
        "discovery": discovery,
        "limitations": result.limitations,
        "link_position": link_position,
        "summary": audit["summary"],
        "segments": audit["segments"],
        "checks_skipped": len(audit["run"].get("checks_skipped", [])),
        "out_dir": out_dir,
        # See the matching keys in audit["run"] for why these are surfaced twice: a caller
        # reading only this dict (no out_dir, no audit.json) must still be able to tell a
        # replayed answer from a measured one.
        "cache_replay": result.cache_replay,
        "cache_stats": result.cache_stats,
        "requires_rendering": requires_rendering,
        "requires_rendering_reason": requires_rendering_reason,
        "render_escalation": render_summary,
    }

    return response, audit


def crawl_describe_settings() -> dict[str, Any]:
    """Every crawl-site config setting: path, type, default, and description.

    The MCP half of #23: an agent can ask what it can configure instead of
    guessing a key name or reading the source. Backed by the same
    ``describe_settings`` that ``crawl-site --config-help`` prints, so the CLI
    and MCP surfaces cannot describe the same setting two different ways.
    """
    from seohead.crawl import settings as crawl_config

    return {"settings": crawl_config.describe_settings()}


def images_download(
    urls: list[str] | None = None,
    output_dir: str | None = None,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not output_dir:
        raise ValueError("output_dir required")
    results = downloader.download_images(urls or [], output_dir, options or {})
    return {"count": len(results), "results": results}


def images_optimize(
    files: list[str] | None = None, settings: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Optimize image files with explicit output semantics.

    Provide ``settings.out_dir`` for non-destructive output. Rewriting source files requires the
    caller to opt in with ``settings.in_place=true``; the optimizer's backup safeguards still apply.
    """
    if not files:
        raise ValueError("files[] required")
    return optimizer.optimize_files(files, settings or {})


def keywords_cluster(**params: Any) -> dict[str, Any]:
    return clusterer.run_clusterer(params)


def robots_check(
    url: str | None = None, user_agent: str = "*", paths: list[str] | None = None
) -> RobotsCheckResult:
    if not url:
        raise ValueError("url required")
    return robots_core.check_robots(url, user_agent=user_agent, paths=paths)


def headers_check(url: str | None = None, method: str = "GET") -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    return headers_core.check_headers(url, method=method)


def asset_weight_check(
    url: str | None = None, file_size_threshold: int | None = None
) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    kwargs: dict[str, Any] = {}
    if file_size_threshold is not None:
        kwargs["file_size_threshold"] = int(file_size_threshold)
    return asset_weight.analyze_page_asset_weight(url, **kwargs)


def links_check(
    url: str | None = None, internal_only: bool = False, limit: int = 200
) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    return links_core.check_links(url, internal_only=internal_only, limit=limit)


def hreflang_check(url: str | None = None) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    return hreflang_core.check_hreflang(url)


def domain_profile(domain: str | None = None, with_tls: bool = True) -> dict[str, Any]:
    if not domain:
        raise ValueError("domain required")
    from seohead.recon import domain as domain_core

    return domain_core.profile_domain(domain, with_tls=with_tls)


def cdn_check(url: str | None = None) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    from seohead.recon import cdn as cdn_core

    return cdn_core.check_cdn(url)


def tech_detect(url: str | None = None) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    from seohead.recon import tech as tech_core

    return tech_core.detect_tech(url)


def security_check(url: str | None = None, probe_paths: bool = False) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    from seohead.recon import security as security_core

    return security_core.check_security(url, probe_paths=bool(probe_paths))


def schema_check(url: str | None = None, html: str | None = None) -> dict[str, Any]:
    if not url and not html:
        raise ValueError("url or html required")
    from seohead.tools import schema_org as schema_core

    return schema_core.check_schema(url=url, html=html)


def schema_build(
    url: str | None = None, html: str | None = None, override_type: str | None = None
) -> dict[str, Any]:
    if not url and not html:
        raise ValueError("url or html required")
    from seohead.tools import schema_build as builder

    return builder.build_schema(url=url, html=html, override_type=override_type)


def log_analyze(path: str | None = None, verify_bots: bool = False) -> dict[str, Any]:
    if not path:
        raise ValueError("path required (web server access-log file)")
    from seohead.tools import logs as logs_core

    return logs_core.analyze_log(path, verify_bots=bool(verify_bots))


def regions_check(
    url: str | None = None, extra: list[str] | None = None, limit: int = 12, render: bool = False
) -> dict[str, Any]:
    if not url:
        raise ValueError("url required (any site page, usually the home page)")
    from seohead.recon import regions as regions_core

    return regions_core.analyze_regions(url, extra=extra or [], limit=limit, render=bool(render))


def site_audit(
    url: str | None = None,
    urls: list[str] | None = None,
    limit: int = 25,
    concurrency: int = 5,
    render: bool = False,
    skip: list[str] | None = None,
) -> dict[str, Any]:
    if not url:
        raise ValueError("url required (site home page)")
    from seohead.audit.site import audit_site

    return audit_site(
        url,
        urls=urls,
        limit=limit,
        concurrency=concurrency,
        render=bool(render),
        skip=skip,
        tools=HANDLERS,
    )


def report_build(audit: Any = None, fmt: str = "xlsx", out: str | None = None) -> dict[str, Any]:
    if audit is None:
        raise ValueError("audit required: audit document or path to its JSON representation")
    from seohead.reports import build_report

    return build_report(audit, fmt=fmt, path=out)


def facts_export(sites: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a facts.v1 multi-site export from already-produced crawl/site
    audits. Makes zero network requests and picks no winner -- see
    seohead.reports.facts for the state machine and the two failure modes
    it refuses (site-identity mismatch, duplicate registrable domain)."""
    if not sites:
        raise ValueError("sites required: a list of {label, crawl_audit, site_audit} descriptors")
    from seohead.reports.facts import build_facts_export

    try:
        result = build_facts_export(sites)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, **result}


def _load_audit(
    value: Any, label: str, diagnostics: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Accept a document, JSON path or validated scan without changing its contents."""
    from seohead.storage.inputs import load_audit_document

    return load_audit_document(value, label, diagnostics)


def compare_crawls(before: Any = None, after: Any = None) -> dict[str, Any]:
    """Diff two audits: which findings were fixed, which are new, which pages
    dropped out of the crawl entirely. See seohead.sf.core.compare for why
    "fixed" and "no longer crawled" are kept apart rather than merged."""
    from seohead.sf.core.compare import compare

    diagnostics: list[dict[str, str]] = []
    before_doc = _load_audit(before, "before", diagnostics)
    after_doc = _load_audit(after, "after", diagnostics)
    result = compare(before_doc, after_doc)
    if diagnostics:
        result["input_diagnostics"] = diagnostics
    return result


def segment_diff(
    audit: Any = None, source: str | None = None, target: str | None = None
) -> dict[str, Any]:
    """Cross-segment counterpart diff (#358): which pages of ``source`` have a
    counterpart in ``target``, and which do not.

    This is the one place ``seohead.crawl`` and ``seohead.sf`` meet for this
    feature: the audit's own recorded ``scope.segments`` / ``scope.segments_only``
    build a ``Scope`` here, and only its ``segment_for``/``rejection`` methods
    are handed to the pure analyzer in ``seohead.sf.core.segment_diff``, which
    never imports the crawl module itself.
    """
    if not source or not target:
        raise ValueError("source and target segment names required")
    from urllib.parse import urlsplit

    from seohead.crawl.spider import Scope
    from seohead.sf.core.segment_diff import diff_segments

    doc = _load_audit(audit, "audit")
    run = doc.get("run") or {}
    crawl_config = run.get("crawl_config") or {}
    segments_cfg = crawl_config.get("scope.segments") or []
    segments_only_cfg = crawl_config.get("scope.segments_only") or []
    scope = Scope.from_config({"segments": segments_cfg, "segments_only": segments_only_cfg})
    start_host = urlsplit(str(run.get("source") or "")).hostname or ""

    return diff_segments(
        doc.get("pages") or [],
        source=source,
        target=target,
        segments=segments_cfg,
        segment_for=scope.segment_for,
        rejection=(lambda u: scope.rejection(u, start_host)) if start_host else None,
        segments_only=set(segments_only_cfg),
        crawl_partial=bool(run.get("crawl_partial")),
    )


def render_check(
    url: str | None = None, viewport: str = "desktop", wait: str = "load"
) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    from seohead.tools import render as render_core

    return render_core.render_check(url, viewport=viewport, wait=wait)


def backlinks_check(
    target: str | None = None, donors: list[str] | None = None, concurrency: int = 3
) -> dict[str, Any]:
    if not target:
        raise ValueError("target required")
    if not donors:
        raise ValueError("donors[] required")
    from seohead.recon import backlinks as backlinks_core

    return backlinks_core.check_backlinks(target, donors, concurrency=concurrency)


def duplicate_check(
    items: list[dict] | None = None,
    threshold: float = 0.92,
    with_fingerprints: bool = False,
    only_indexable: bool = True,
) -> dict[str, Any]:
    if not items:
        raise ValueError("items[] required (list of {id, text})")
    from seohead.tools import duplicate as dup_core

    return dup_core.find_duplicates(
        items,
        threshold=threshold,
        with_fingerprints=with_fingerprints,
        only_indexable=only_indexable,
    )


def mirror_check(url: str | None = None, timeout: float = 12.0) -> dict[str, Any]:
    """Verify canonical host consolidation across scheme, ``www``, index-file, case, and slash variants."""
    if not url:
        raise ValueError("url required")
    from seohead.recon import mirrors as mirrors_core

    return mirrors_core.check_mirrors(url, timeout=timeout)


def ai_bots_check(url: str | None = None, robots_text: str | None = None) -> dict[str, Any]:
    """Evaluate AI-crawler access from supplied robots.txt content or a site URL."""
    if not url and not robots_text:
        raise ValueError("url or robots_text required")
    from seohead.recon import ai_bots as ai_bots_core

    if robots_text is None:
        from seohead.recon.net import http_client, normalize_url

        target = normalize_url(url or "")
        if not target:
            return {"ok": False, "error": f"not a recognizable URL: {url!r}"}
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(target)
        robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
        try:
            client, _ = http_client(20.0)
            with client:
                resp = client.get(robots_url)
        except Exception as exc:  # Tool boundary: network failures are result data, not crashes.
            return {"ok": False, "url": robots_url, "error": str(exc)}
        code = resp.status_code
        if code >= 500:
            # A server error is "we could not read the rules", not "there are no
            # rules" — #135 established the same distinction for the native
            # crawler's own robots fetch. Reporting every AI bot allowed here
            # would be a false permission grant on evidence that never loaded.
            return {
                "ok": False,
                "url": url,
                "robots_url": robots_url,
                "status_code": code,
                "error": f"robots.txt returned {code}; rules could not be read",
            }
        # A 4xx robots.txt means "no restrictions" per RFC 9309, same as
        # tools.robots.check_robots; the response body (an error page, not
        # rules) is discarded rather than handed to the parser.
        robots_text = resp.text if code < 400 else ""
        fetched = {"robots_url": robots_url, "status_code": code}
    else:
        fetched = {}
    result = ai_bots_core.check_ai_access(robots_text)
    return {**result, "url": url, **fetched}


def llms_txt_check(url: str | None = None, brand: str | None = None) -> dict[str, Any]:
    if not url:
        raise ValueError("url required")
    from seohead.tools import llms_txt as llms_core

    return llms_core.check_llms_txt(url, brand=brand)


def _require_fetched_html(url: str) -> dict[str, Any]:
    """Fetch ``url`` and tighten ``fetch_html``'s transport-only ``ok`` to a 2xx status.

    Matches the success contract every other URL-fetching handler here already
    uses (``parse_url``'s ``ok``): a transport failure or a non-2xx response
    both come back as ``ok: False``, so a caller can check one flag either way.
    """
    fetched = parser.fetch_html(url)
    if fetched["ok"]:
        fetched["ok"] = 200 <= fetched["status_code"] < 300
    return fetched


def citability_check(
    url: str | None = None, text: str | None = None, content_area: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Score whether content is self-contained and evidence-rich enough to support a cited AI answer.

    Scored over the resolved content area when fetched from a ``url``: nav and
    footer boilerplate would otherwise dilute statistical density, and — more
    importantly — the parser's whole-document ``text`` field has no paragraph
    or heading breaks at all (it is a single collapsed line), which silently
    zeroes the Answer-Blocks and Structure-Quality dimensions for every live
    page. ``markdown_extract``'s content-area Markdown keeps both the
    boilerplate exclusion and the structure the scorer depends on. Passing
    ``text`` directly is unaffected: the caller chose exactly what to score.
    """
    if not url and not text:
        raise ValueError("url or text required")
    from seohead.tools import citability as cit_core

    if text is not None:
        return cit_core.score_citability(text)
    fetched = _require_fetched_html(url)
    if not fetched["ok"]:
        return {"ok": False, "url": url, "error": fetched.get("error", "fetch failed")}
    from seohead.tools import markdown_extract as md_core

    content_markdown = md_core.extract_markdown(fetched["html"], content_area)["content_markdown"]
    return {"url": url, **cit_core.score_citability(content_markdown)}


def markdown_extract(
    url: str | None = None, html: str | None = None, content_area: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Render a page as Markdown in two scopes: content-area only, and full document.

    Pass ``html`` to render offline, or ``url`` to fetch it first. The
    content-area rendering is what is worth diffing between crawls, scoring,
    or handing to a model; the full-document rendering (header and footer
    included) is what ``boilerplate_report`` hashes to check whether
    boilerplate is actually consistent across a crawl.
    """
    if not url and not html:
        raise ValueError("url or html required")
    from seohead.tools import markdown_extract as md_core

    if html is not None:
        return {"ok": True, **md_core.extract_markdown(html, content_area)}
    fetched = _require_fetched_html(url)
    if not fetched["ok"]:
        return {"ok": False, "url": url, "error": fetched.get("error", "fetch failed")}
    return {
        "ok": True,
        "url": url,
        "final_url": fetched["final_url"],
        "status_code": fetched["status_code"],
        **md_core.extract_markdown(fetched["html"], content_area),
    }


def log_scan(
    run: str | None = None, images_dir: str | None = None, max_per_rule: int = 20
) -> dict[str, Any]:
    """Report claims a finished run makes that cannot all be true at once.

    ``run`` is a directory holding ``audit.json``, ``pages.jsonl`` and/or
    ``decisions.jsonl`` — whatever ``crawl-site --out-dir`` or ``sf run --out`` wrote.
    ``decisions.jsonl`` (issue #134) is the per-URL exclusion log a native crawl writes
    beside ``pages.jsonl``; it lets a rule catch a contradiction that never survives into
    ``audit.json`` at all, not only one visible in the finished output. ``images_dir`` is an
    ``images-download`` output directory, whose manifest lets a recorded size be compared
    against the file itself.

    This is not a second audit. It reports only pairs of facts from the same run that
    contradict each other, each with both values and where they came from, so a surprising
    number can be traced instead of trusted. See ``seohead.tools.logscan`` for the rules and
    for the defects each one was written from.
    """
    from seohead.tools import logscan

    if not run:
        raise ValueError("log_scan needs a run directory")
    artifacts = logscan.load_run(run, images_dir)
    if artifacts.audit is None and not artifacts.pages:
        return {
            "ok": False,
            "error": f"no audit.json or pages.jsonl in {run}",
            "anomalies": [],
            "anomaly_count": 0,
        }
    return logscan.scan(artifacts, max_per_rule=max_per_rule)


def boilerplate_report(pages: list[dict] | None = None) -> dict[str, Any]:
    """Group a crawled corpus by header/nav/footer hash and report minority template groups.

    Each page is ``{"url": str, "html": str}`` or, when the hash was already
    computed upstream (``boilerplate_report.boilerplate_hash``), ``{"url": str,
    "hash": str}``.
    """
    if not pages:
        raise ValueError("pages[] required (list of {url, html} or {url, hash})")
    from seohead.tools import boilerplate_report as bp_core

    return bp_core.boilerplate_consistency_report(pages)


def social_meta_check(
    url: str | None = None, og: dict[str, str] | None = None, twitter: dict[str, str] | None = None
) -> dict[str, Any]:
    """Identify missing Open Graph and Twitter Card fields required for a stable link preview."""
    if not url and og is None and twitter is None:
        raise ValueError("url or og/twitter required")
    from seohead.tools import social_meta as sm_core

    if og is None and twitter is None:
        from seohead.tools import parser as _parser

        page = _parser.parse_url(
            url,
            {
                "meta": False,
                "canonical": False,
                "og": True,
                "headings": False,
                "jsonld": False,
                "links": False,
                "text": False,
            },
        )
        if not page.get("ok"):
            return {"ok": False, "url": url, "error": page.get("error", "parse failed")}
        og, twitter = page.get("og") or {}, page.get("twitter") or {}
        fetched = {"url": url}
    else:
        fetched = {}
    return {**sm_core.check_social_meta(og=og, twitter=twitter), **fetched}


def soft404_check(url: str | None = None) -> dict[str, Any]:
    """Probe two deterministic nonexistent URLs to distinguish honest 404s from soft-404 responses."""
    if not url:
        raise ValueError("url required")
    from seohead.tools import soft404 as s4_core

    return s4_core.check_soft404(url)


# Registry consumed by the CLI and MCP server: one source of truth for public behavior.

# --- External data providers: demand, search results, and spend -------------------------


def keywords_expand(
    phrase: str | None = None, limit: int = 300, regions: list[str] | None = None
) -> dict[str, Any]:
    """Expand a seed phrase with Yandex Wordstat refinements and related queries.

    Returned frequency is **base frequency**, not exact frequency: the API does not expose
    ``!``, ``+``, or ``[]`` operators, and base counts are typically about nine times higher than
    exact counts. They are suitable for initial filtering; use Arsenkin for exact ``!W`` values.
    A multi-region request sums demand across its regions, so request regions separately when
    regional comparison matters. This method is paid and subject to Wordstat's hourly quota.
    """
    if not phrase:
        raise ValueError("phrase required")
    from seohead.data_sources.credentials import MissingCredential
    from seohead.data_sources.yandex_cloud import Wordstat

    try:
        pool, meta = Wordstat().expand(phrase, limit=limit, regions=tuple(regions or ["225"]))
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    ranked = sorted(pool.items(), key=lambda kv: -kv[1])
    return {
        "ok": True,
        "phrase": phrase,
        "found": len(ranked),
        "total_count": meta.get("totalCount"),
        "from_results": meta.get("results"),
        "from_associations": meta.get("associations"),
        "keywords": [
            {"phrase": p, "base_frequency": c, "origin": meta["origin"].get(p)} for p, c in ranked
        ],
    }


def keywords_seasonality(
    phrase: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    period: str = "PERIOD_MONTHLY",
    regions: list[str] | None = None,
) -> dict[str, Any]:
    """Return Yandex Wordstat demand dynamics; dates use RFC3339, e.g. ``2026-01-01T00:00:00Z``."""
    if not phrase or not from_date or not to_date:
        raise ValueError("phrase, from_date and to_date required")
    from seohead.data_sources.credentials import MissingCredential
    from seohead.data_sources.yandex_cloud import Wordstat

    try:
        body = Wordstat().dynamics(
            phrase, from_date, to_date, period=period, regions=tuple(regions or ["225"])
        )
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "phrase": phrase, "period": period, "dynamics": body}


def serp_fetch(
    query: str | None = None, queries: list[str] | None = None, region: str = "225", top: int = 10
) -> dict[str, Any]:
    """Fetch Yandex results for one query or a batch through the asynchronous API only.

    The synchronous endpoint is deliberately excluded because it costs roughly sixteen times more.
    Batch operations are launched together and polled as a group, so N queries take approximately
    one batch duration rather than N sequential request durations. An exact duplicate query is
    billed once, since a second submission of identical text could never surface a second result.
    Submitted operations are billed even if polling times out; their operation records remain in
    the local spend journal. A submission the provider rejected outright is never billed and
    reaches the caller as an explicit error, distinct from a billed operation that timed out.
    """
    targets = [q for q in ([query] if query else []) + list(queries or []) if q]
    if not targets:
        raise ValueError("query or queries required")
    unique_targets = list(dict.fromkeys(targets))
    from seohead.data_sources.credentials import MissingCredential
    from seohead.data_sources.yandex_cloud import WebSearch

    try:
        client = WebSearch()
        raw = client.search_batch(unique_targets, region=region, groups=top)
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    results = {}
    for q, v in raw.items():
        entry = {"docs": v.get("docs", []), "error": v.get("error"), "status": v.get("status")}
        if v.get("operation_id") is not None:
            entry["operation_id"] = v["operation_id"]
        if v.get("http_status") is not None:
            entry["http_status"] = v["http_status"]
        results[q] = entry
    # A query is only ever absent here because its operation was billed and never finished
    # before the timeout: search_batch already gives every rejected or lost submission an
    # explicit entry above, so this is never a stand-in for "the provider said no".
    missing = [q for q in unique_targets if q not in results]
    return {
        "ok": True,
        "region": region,
        "requested": len(unique_targets),
        "returned": len(results),
        "results": results,
        "not_returned": missing,
        "note": (
            "queries not returned before timeout were already billed; their operations "
            "are recorded in the spend journal"
            if missing
            else None
        ),
    }


def keywords_exact(
    keywords: list[str] | None = None, region: int = 225, wait: bool = True
) -> dict[str, Any]:
    """Fetch exact ``!W`` frequency through Arsenkin, which Wordstat's API does not expose.

    This operation is paid and consumes account limits. The charge and ``task_id`` are journaled as
    soon as the task is created, allowing a result whose polling or parsing failed to be retrieved
    later without paying for a duplicate task.
    """
    if not keywords:
        raise ValueError("keywords required")
    from seohead.data_sources.arsenkin import ArsenkinClient, ArsenkinError
    from seohead.data_sources.credentials import MissingCredential

    try:
        client = ArsenkinClient()
        task = client.set_task(
            "keywords_frequency", {"keywords": list(keywords), "region": int(region)}
        )
        if not wait:
            return {
                "ok": True,
                "task_id": task["task_id"],
                "cost": task["cost"],
                "note": "task created and billed; retrieve the result later by task_id",
            }
        result = client.wait(task["task_id"])
        return {
            "ok": True,
            "task_id": task["task_id"],
            "cost": task["cost"],
            "result": result.get("result", result),
        }
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except ArsenkinError as exc:
        error: dict[str, Any] = {"ok": False, "error": str(exc), "code": exc.code}
        # `task` exists only once `set_task` has already succeeded (and billed) --
        # that identifier must survive into a subsequent `wait()` failure, since
        # parsing it back out of the free-text error string is exactly the recovery
        # route this handler's own docstring documents. When `set_task` itself is
        # what raised, nothing was billed, so no task_id/cost should be fabricated.
        if "task" in locals():
            error["task_id"] = task["task_id"]
            error["cost"] = task["cost"]
        return error


def google_keywords(
    keywords: list[str] | None = None,
    seed: str | None = None,
    location_code: int = 2840,
    language: str = "en",
    country: str | None = None,
    limit: int = 100,
    difficulty: bool = False,
) -> dict[str, Any]:
    """Query Google demand through DataForSEO by keyword list or seed phrase.

    ``keywords`` returns search volume and competition for an existing list. ``seed`` expands one
    phrase into keyword ideas, analogous to Wordstat refinements but for Google. Set
    ``difficulty=true`` to return keyword difficulty instead of search volume.

    DataForSEO does not support locations in Russia or Belarus. The geographic
    guard blocks such requests before they reach the paid provider and directs callers to Wordstat
    or Arsenkin. The default ``sandbox`` environment returns realistic response shapes with fake
    data and incurs no charge; production requires explicit provider configuration.
    """
    from seohead.data_sources import dataforseo as core

    if seed:
        ideas = core.keyword_ideas(
            seed, location_code=location_code, language=language, limit=limit, country=country
        )
        if not difficulty or not ideas.get("ok"):
            return ideas
        # `difficulty=True` is a documented, independent option -- it must not be
        # silently dropped just because `seed` also routed through the ideas path.
        expanded = [k.get("phrase") for k in ideas.get("keywords") or [] if k.get("phrase")]
        if not expanded:
            return ideas
        scored = core.keyword_difficulty(
            expanded, location_code=location_code, language=language, country=country
        )
        ideas["difficulty"] = scored
        if scored.get("ok") is False:
            ideas["ok"] = False
        return ideas
    if not keywords:
        raise ValueError("keywords or seed required")
    if difficulty:
        return core.keyword_difficulty(
            keywords, location_code=location_code, language=language, country=country
        )
    return core.search_volume(
        keywords, location_code=location_code, language=language, country=country
    )


def google_serp(
    query: str | None = None,
    location_code: int = 2840,
    language: str = "en",
    depth: int = 10,
    country: str | None = None,
) -> dict[str, Any]:
    """Return the Google organic results that actually rank for a query in the selected market."""
    if not query:
        raise ValueError("query required")
    from seohead.data_sources import dataforseo as core

    return core.serp(
        query, location_code=location_code, language=language, depth=depth, country=country
    )


def metrika_counters() -> dict[str, Any]:
    """List Metrika counters visible to the token and expose the ``counter_id`` required by reports."""
    from seohead.data_sources.credentials import MissingCredential
    from seohead.data_sources.metrika import MetrikaClient, MetrikaError

    try:
        counters = MetrikaClient().counters()
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except MetrikaError as exc:
        return {"ok": False, "error": exc.message, "status": exc.status}
    return {
        "ok": True,
        "count": len(counters),
        "counters": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "site": c.get("site"),
                "status": c.get("status"),
            }
            for c in counters
        ],
    }


def metrika_setup(counter_id: str | None = None) -> dict[str, Any]:
    """Inspect a Metrika counter's goals, filters, and data-processing operations.

    Run this before drawing conclusions from traffic. Counter operations can silently reshape
    reports—for example by removing URL parameters. If no goals are configured, the dataset cannot
    contain conversions; reporting a "zero conversion rate" would describe instrumentation, not
    observed user behavior.
    """
    if not counter_id:
        raise ValueError("counter_id required")
    from seohead.data_sources.credentials import MissingCredential
    from seohead.data_sources.metrika import MetrikaClient, MetrikaError

    try:
        client = MetrikaClient()
        goals = client.goals(counter_id)
        filters = client.filters(counter_id)
        operations = client.operations(counter_id)
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except MetrikaError as exc:
        return {"ok": False, "error": exc.message, "status": exc.status}
    return {
        "ok": True,
        "counter_id": counter_id,
        "goals": [{"id": g.get("id"), "name": g.get("name"), "type": g.get("type")} for g in goals],
        "goals_count": len(goals),
        "filters": (filters or {}).get("filters", []),
        "operations": (operations or {}).get("operations", []),
        "note": (
            "no goals are configured, so conversions cannot appear in the data; "
            "a zero conversion rate would reflect instrumentation, not observed behavior"
        )
        if not goals
        else None,
    }


def metrika_report(
    counter_id: str | None = None,
    metrics: str | None = None,
    dimensions: str | None = None,
    date1: str = "30daysAgo",
    date2: str = "today",
    filters: str | None = None,
    sort: str | None = None,
    limit: int = 100,
    paginate: bool = False,
) -> dict[str, Any]:
    """Return a Metrika report as flat JSON-serializable records.

    ``metrics`` and ``dimensions`` are comma-separated API identifiers such as ``ym:s:visits`` and
    ``ym:s:startURL``. Dates also accept relative forms such as ``30daysAgo``. With
    ``paginate=true`` the client collects successive pages but stops at 100,000 rows and marks the
    result as capped rather than implying that the dataset is complete.
    """
    if not counter_id or not metrics:
        raise ValueError("counter_id and metrics required")
    from seohead.data_sources.credentials import MissingCredential
    from seohead.data_sources.metrika import MetrikaClient, MetrikaError, rows_to_records

    params = {"ids": counter_id, "metrics": metrics, "date1": date1, "date2": date2}
    if dimensions:
        params["dimensions"] = dimensions
    if filters:
        params["filters"] = filters
    if sort:
        params["sort"] = sort
    try:
        body = MetrikaClient().report(params, paginate=paginate, limit=limit)
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except MetrikaError as exc:
        return {"ok": False, "error": exc.message, "status": exc.status}
    return {
        "ok": True,
        "counter_id": counter_id,
        "period": f"{date1}..{date2}",
        "total_rows": body.get("total_rows"),
        "returned": len(body.get("data") or []),
        "capped": body.get("capped", False),
        "totals": body.get("totals"),
        "rows": rows_to_records(body),
    }


def wayback_history(
    url: str | None = None,
    limit: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """Every recorded Wayback Machine snapshot of a URL: when it changed, and what it looked like.

    Free and keyless. Answers what a crawl cannot: *when* a page started returning its current
    status, and what content preceded it — the difference between a bug report and a restoration
    plan. A URL the archive never captured is not an error; it returns an empty list.
    """
    if not url:
        raise ValueError("url required")
    from seohead.data_sources import wayback as core

    return core.history(url, limit=limit, from_date=from_date, to_date=to_date)


def crtsh_subdomains(domain: str | None = None) -> dict[str, Any]:
    """Subdomains discovered from public Certificate Transparency logs (crt.sh).

    Free and keyless. Every TLS certificate ever issued for a domain is public record, so this
    finds hosts that no page ever links to — the gap `mirror-check` and `regions-check` both
    currently rely on being told about by hand.
    """
    if not domain:
        raise ValueError("domain required")
    from seohead.data_sources import crtsh as core

    return core.subdomains(domain)


def gsc_query(
    site_url: str | None = None,
    mode: str = "search_analytics",
    start_date: str = "28daysAgo",
    end_date: str = "today",
    dimensions: list[str] | None = None,
    row_limit: int = 1000,
    inspection_url: str | None = None,
) -> dict[str, Any]:
    """Google Search Console: search performance (``mode=search_analytics``) or Google's own
    indexing verdict for one URL (``mode=inspect_url``).

    Requires an own, verified property and an OAuth2 bearer token — see
    ``seohead sources-doctor`` and docs/SETUP.md. A missing token returns an explicit failure
    naming what to configure; it never fabricates a result.
    """
    if not site_url:
        raise ValueError("site_url required")
    from seohead.data_sources import gsc as core

    if mode == "inspect_url":
        if not inspection_url:
            raise ValueError("inspection_url required for mode=inspect_url")
        return core.inspect_url(site_url, inspection_url)
    if mode != "search_analytics":
        raise ValueError("mode must be search_analytics or inspect_url")
    return core.search_analytics(
        site_url,
        start_date=start_date,
        end_date=end_date,
        dimensions=dimensions,
        row_limit=row_limit,
    )


def crux_report(
    url: str | None = None,
    origin: str | None = None,
    form_factor: str | None = None,
    metrics: list[str] | None = None,
) -> dict[str, Any]:
    """Field Core Web Vitals (LCP, INP, CLS) as real Chrome users experienced them, at the 75th
    percentile — the honest counterpart to a synthesized lab score (see `render-check` and
    issue #59). Pass exactly one of ``url``/``origin``. Requires a Chrome UX Report API key.
    """
    from seohead.data_sources import crux as core

    return core.query(url=url, origin=origin, form_factor=form_factor, metrics=metrics)


def indexnow_submit(
    urls: list[str] | None = None,
    host: str | None = None,
    key_location: str | None = None,
) -> dict[str, Any]:
    """Push changed URLs to Bing, Yandex, Naver, and Seznam in one call.

    Google has not joined IndexNow as of 2026 — a submission here does not affect Google's crawl
    schedule. Requires a self-generated key, published at ``https://<host>/<key>.txt`` before the
    first call; see docs/SETUP.md.
    """
    if not urls:
        raise ValueError("urls required")
    if not host:
        raise ValueError("host required")
    from seohead.data_sources import indexnow as core

    return core.submit(urls, host=host, key_location=key_location)


def regions_tree(save_to: str | None = None) -> dict[str, Any]:
    """Fetch the authoritative Yandex region tree used by the ``regions[]`` parameter.

    This is Wordstat's only free method. Static mappings in ``data_sources/regions.py`` are faster,
    but some entries are documentation-derived rather than verified against the current API; use
    this live tree to validate an unfamiliar region before issuing a paid request.
    """
    from seohead.data_sources import yandex_regions as regions_core

    return regions_core.fetch_tree(save_to=save_to)


def spend_report(since: str | None = None) -> dict[str, Any]:
    """Summarize recorded provider charges by source, operation, and day."""
    from seohead.data_sources import spend as spend_core

    return spend_core.report(since=since)


def sources_doctor() -> dict[str, Any]:
    """Report provider readiness and credential locations without exposing secret values."""
    from seohead.data_sources import credentials as creds

    checks = {
        "arsenkin": ("arsenkin/token", "ARSENKIN_TOKEN"),
        "yandex_cloud_api_key": ("yandex-wordstat/api_key", "YANDEX_CLOUD_API_KEY"),
        "yandex_cloud_folder": ("yandex-wordstat/folder_id", "YANDEX_CLOUD_FOLDER_ID"),
        "yandex_metrika": ("yandex-metrika/token", "YANDEX_METRIKA_TOKEN"),
        "dataforseo": ("dataforseo/login", "DATAFORSEO_LOGIN"),
        "gsc": ("gsc/access_token", "GSC_ACCESS_TOKEN"),
        "crux": ("crux/api_key", "CRUX_API_KEY"),
        "indexnow": ("indexnow/key", "INDEXNOW_KEY"),
    }
    sources = {
        name: {
            "ready": creds.available(path, env),
            "file": str(creds.CONFIG_ROOT / path),
            "env": env,
        }
        for name, (path, env) in checks.items()
    }
    dataforseo_ready, dataforseo_components = creds.dataforseo_ready()
    sources["dataforseo"]["ready"] = dataforseo_ready
    sources["dataforseo"]["components"] = dataforseo_components
    from seohead.data_sources import spend as spend_core

    return {"ok": True, "sources": sources, "spend_log": str(spend_core.log_path())}


def scan_reanalyze(input_path: str, out: str, producer_build: str | None = None) -> dict[str, Any]:
    """Derive a fresh SQLite scan by parsing retained evidence without network access."""
    from seohead.servers.reanalysis_handlers import reanalyze_scan

    return reanalyze_scan(input_path=input_path, out=out, producer_build=producer_build)


def scan_list(directory: str, offset: int = 0, limit: int = 100) -> dict[str, Any]:
    from seohead.servers.history_handlers import scan_list as core

    return core(directory, offset=offset, limit=limit)


def scan_inspect(
    input_path: str,
    table: str = "pages",
    offset: int = 0,
    limit: int = 100,
    max_bytes: int = 1_048_576,
) -> dict[str, Any]:
    from seohead.servers.history_handlers import scan_inspect as core

    return core(input_path, table=table, offset=offset, limit=limit, max_bytes=max_bytes)


def scan_snapshot(input_path: str, out: str) -> dict[str, Any]:
    from seohead.servers.history_handlers import scan_snapshot as core

    return core(input_path, out)


def scan_pin(input_path: str, pinned: bool = True) -> dict[str, Any]:
    from seohead.servers.history_handlers import scan_pin as core

    return core(input_path, pinned=pinned)


def scan_prune(
    directory: str,
    older_than_days: int = 30,
    keep_newest: int = 5,
    plan: dict[str, Any] | str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    from seohead.servers.history_handlers import scan_prune as core

    return core(
        directory,
        older_than_days=older_than_days,
        keep_newest=keep_newest,
        plan=plan,
        apply=apply,
    )


def scan_body_diff(
    left: str,
    right: str,
    url: str,
    variant_key: str | None = None,
    representation: str = "static",
    text: bool = False,
    max_bytes: int = 5 * 1024 * 1024,
    max_lines: int = 10_000,
) -> dict[str, Any]:
    from seohead.servers.history_handlers import scan_body_diff as core

    return core(
        left,
        right,
        url,
        variant_key=variant_key,
        representation=representation,
        text=text,
        max_bytes=max_bytes,
        max_lines=max_lines,
    )


_RAW_HANDLERS = {
    "parse": parse,
    "redirects_generate": redirects_generate,
    "redirects_check": redirects_check,
    "sitemap_crawl": sitemap_crawl,
    "crawl_site": crawl_site,
    "crawl_describe_settings": crawl_describe_settings,
    "images_download": images_download,
    "images_optimize": images_optimize,
    "keywords_cluster": keywords_cluster,
    "robots_check": robots_check,
    "headers_check": headers_check,
    "asset_weight_check": asset_weight_check,
    "links_check": links_check,
    "hreflang_check": hreflang_check,
    "domain_profile": domain_profile,
    "cdn_check": cdn_check,
    "tech_detect": tech_detect,
    "security_check": security_check,
    "backlinks_check": backlinks_check,
    "schema_check": schema_check,
    "schema_build": schema_build,
    "duplicate_check": duplicate_check,
    "ai_bots_check": ai_bots_check,
    "mirror_check": mirror_check,
    "llms_txt_check": llms_txt_check,
    "citability_check": citability_check,
    "markdown_extract": markdown_extract,
    "boilerplate_report": boilerplate_report,
    "log_scan": log_scan,
    "social_meta_check": social_meta_check,
    "soft404_check": soft404_check,
    "log_analyze": log_analyze,
    "regions_check": regions_check,
    "render_check": render_check,
    "site_audit": site_audit,
    "report_build": report_build,
    "facts_export": facts_export,
    "compare_crawls": compare_crawls,
    "segment_diff": segment_diff,
    "keywords_expand": keywords_expand,
    "keywords_seasonality": keywords_seasonality,
    "keywords_exact": keywords_exact,
    "serp_fetch": serp_fetch,
    "spend_report": spend_report,
    "sources_doctor": sources_doctor,
    "regions_tree": regions_tree,
    "metrika_counters": metrika_counters,
    "metrika_setup": metrika_setup,
    "metrika_report": metrika_report,
    "google_keywords": google_keywords,
    "google_serp": google_serp,
    "wayback_history": wayback_history,
    "crtsh_subdomains": crtsh_subdomains,
    "gsc_query": gsc_query,
    "crux_report": crux_report,
    "indexnow_submit": indexnow_submit,
    "scan_reanalyze": scan_reanalyze,
    "scan_list": scan_list,
    "scan_inspect": scan_inspect,
    "scan_snapshot": scan_snapshot,
    "scan_pin": scan_pin,
    "scan_prune": scan_prune,
    "scan_body_diff": scan_body_diff,
}

# Journaling sits here rather than in each interface: the CLI and the MCP server
# both dispatch through this mapping, so one wrapper records every call exactly
# once and no future tool can be added without being recorded.
HANDLERS = {name: runlog.journaled(name, fn) for name, fn in _RAW_HANDLERS.items()}
