"""Bulk site auditing: one call, one JSON document, any supported report.

This module performs the consolidation in code: site-level tools run once,
page-level tools run for every selected URL, and all results are assembled into a
document with a known shape.

That document shape (``schema: seohead.site-audit/1``) is a public contract consumed
by the generators in :mod:`seohead.reports`. It must not change silently, because a
schema drift here would break every report format at once.

A failure in one tool does not abort the audit. The failed check and its reason are
recorded in ``summary.tools_failed`` instead. An audit that silently loses half its
checks is more dangerous than one that states exactly which evidence is missing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from seohead.recon.net import normalize_url

SCHEMA = "seohead.site-audit/1"

# Finding severity is assigned here by policy; it is not measured by the source
# tool. The rules intentionally live in one readable list so reviewers can inspect
# and challenge the policy. Ordering is significant: the first match wins.
SEVERITY_RULES: tuple[tuple[str, str], ...] = (
    # The page is physically unavailable to a crawler or removes itself from the index.
    ("empty page", "critical"),
    ("empty container", "critical"),
    ("canonicaliz", "critical"),
    ("redirect to the main site", "critical"),
    ("noindex", "critical"),
    ("contains effectively no page copy", "critical"),
    ("unavailable or return errors", "critical"),
    ("5xx", "critical"),
    # The issue can hinder ranking or waste crawl budget.
    ("only after JavaScript", "warning"),
    ("content matches", "warning"),
    ("same title", "warning"),
    ("city is missing", "warning"),
    ("wasting crawl budget", "warning"),
    ("title changes after JavaScript", "warning"),
    ("canonical", "warning"),
    ("affiliate site", "warning"),
    ("two schema types at once", "warning"),
    # The markup exists and is void: the owner believes they publish it.
    ("cannot be parsed", "warning"),
    ("json-ld block(s) on the page are invalid", "warning"),
    # These missing directives are visible to both search engines and browsers.
    ("missing strict-transport-security", "warning"),
    ("missing content-security-policy", "warning"),
    ("missing meta description", "warning"),
    ("no analytics integration was detected", "warning"),
    ("neither etag nor last-modified", "warning"),
    # Every other observation remains a notice.
)

# Site-level tools run once against the domain or homepage.
SITE_TOOLS: tuple[str, ...] = (
    "domain_profile",
    "cdn_check",
    "tech_detect",
    "security_check",
    "robots_check",
    "ai_bots_check",
    "llms_txt_check",
    "regions_check",
    "render_check",
    "sitemap_crawl",
)

# Page-level tools run once for every URL in the selected page set.
PAGE_TOOLS: tuple[str, ...] = ("parse", "schema_check", "social_meta_check")


def classify(text: str) -> str:
    """Classify finding text with the documented, explicitly heuristic policy."""
    low = (text or "").lower()
    for marker, level in SEVERITY_RULES:
        if marker.lower() in low:
            return level
    return "notice"


def _site_kwargs(
    tool: str, url: str, domain: str, render: bool, sitemap_url: str = ""
) -> dict[str, Any]:
    if tool == "domain_profile":
        return {"domain": domain}
    if tool == "regions_check":
        return {"url": url, "limit": 8, "render": render}
    if tool == "sitemap_crawl":
        # The tool requires the sitemap address, not the site homepage.
        return {"url": sitemap_url or url}
    return {"url": url}


def _run(fn: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    """Invoke a tool without allowing its exception to escape the audit boundary."""
    try:
        result = fn(**kwargs)
        return result if isinstance(result, dict) else {"ok": True, "result": result}
    # This is an intentional orchestrator boundary: a single tool failure becomes
    # result data and must not abort the remaining site checks.
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _urls_from_sitemap(sitemap: dict[str, Any], limit: int) -> list[str]:
    """Extract page URLs from a sitemap result regardless of its nesting depth."""
    found: list[str] = []
    # Fast path: the standard sitemap-crawl response stores records under ``urls``.
    for entry in (sitemap.get("urls") or []) if isinstance(sitemap, dict) else []:
        if isinstance(entry, str) and entry.startswith("http"):
            found.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("loc"), str):
            found.append(entry["loc"])
        if len(found) >= limit * 4:
            break
    stack: list[Any] = [] if found else [sitemap]
    while stack and len(found) < limit * 4:
        node = stack.pop()
        if isinstance(node, dict):
            for key in ("loc", "url"):
                value = node.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    found.append(value)
            stack += [v for v in node.values() if isinstance(v, (dict, list))]
        elif isinstance(node, list):
            stack += node
    # Sitemap order is meaningful--important pages usually appear first--so preserve it.
    seen: set[str] = set()
    unique = [u for u in found if not (u in seen or seen.add(u))]
    return unique[:limit]


def _first_h1(facts: dict[str, Any]) -> str:
    """Read H1 from any heading shape returned by ``parse``."""
    direct = facts.get("h1")
    if isinstance(direct, str) and direct.strip():
        return direct
    headings = facts.get("headings")
    if isinstance(headings, dict):
        first = headings.get("h1") or headings.get("H1") or []
        if isinstance(first, list) and first:
            return str(first[0])
        if isinstance(first, str):
            return first
    if isinstance(headings, list):
        for item in headings:
            if isinstance(item, dict) and str(item.get("level", "")).lower() in ("1", "h1"):
                return str(item.get("text", ""))
    return ""


def _page_row(url: str, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Build one normalized page row for the Excel and Word reports."""
    parsed = results.get("parse") or {}
    # ``parse`` returns a batch envelope: {"count": N, "results": [...]}.
    # Reading fields directly from the envelope produces empty report columns;
    # this exact mistake surfaced during the first production run.
    rows = parsed.get("results") or []
    facts = rows[0] if rows else parsed
    schema = results.get("schema_check") or {}
    schema_types = schema.get("types") or sorted(
        {
            type_name
            for entity in (schema.get("entities") or [])
            if isinstance(entity, dict)
            for type_name in (entity.get("types") or [])
            if isinstance(type_name, str)
        }
    )
    social = results.get("social_meta_check") or {}
    issues: list[str] = []
    for tool, data in results.items():
        if not isinstance(data, dict):
            continue
        if data.get("ok") is False and data.get("error"):
            issues.append(f"{tool}: {data['error']}")
        for finding in (data.get("findings") or [])[:5]:
            issues.append(str(finding))
    return {
        "url": url,
        "status": facts.get("status_code") or facts.get("status"),
        "title": (facts.get("title") or "")[:300],
        "title_length": len(facts.get("title") or ""),
        "description_length": len(facts.get("meta_description") or facts.get("description") or ""),
        "h1": (_first_h1(facts) or "")[:300],
        "canonical": facts.get("canonical") or "",
        "words": facts.get("word_count") or facts.get("words") or 0,
        "schema_types": ", ".join(schema_types),
        "schema_errors": len(schema.get("errors") or []),
        "social_missing": len(social.get("missing") or []),
        "issues": issues,
    }


def audit_site(
    url: str,
    urls: list[str] | None = None,
    limit: int = 25,
    concurrency: int = 5,
    render: bool = False,
    skip: list[str] | None = None,
    tools: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a complete site audit into one contract-stable document.

    ``urls`` is an explicit page list; when omitted, pages are read from the
    sitemap. ``limit`` caps the selected pages, while ``skip`` names checks that
    should not run.

    The audit makes many concurrent network requests, so ``concurrency`` remains
    deliberately conservative: an SEO audit must not resemble a load test to the
    target site.
    """
    if not url or not str(url).strip():
        return {"ok": False, "error": "URL is required"}
    start = normalize_url(str(url).strip())
    domain = (urlparse(start).hostname or "").strip()
    # ``normalize_url`` is intentionally permissive and can turn arbitrary text
    # into a host-like value. Because an audit emits dozens of requests, reject
    # malformed input before any network work begins.
    if not domain or " " in domain or "." not in domain.strip("."):
        return {"ok": False, "error": f"Value does not look like a domain: {url!r}"}
    try:
        limit = max(1, int(limit))
        concurrency = max(1, min(int(concurrency), 10))
    except (TypeError, ValueError):
        return {"ok": False, "error": "limit and concurrency must be numeric"}

    if tools is None:
        # The analyzer is handed the tools it composes rather than importing the
        # interface layer to fetch them (#221): seohead/audit reaching into
        # seohead.servers inverted the dependency every other package follows,
        # and the deferred import that made the resulting cycle survive import
        # time is what kept it invisible. Failing by name here beats a
        # TypeError several frames deeper.
        return {"ok": False, "url": url, "error": "audit_site requires a tool registry"}

    skipped = set(skip or [])
    site_tools = [t for t in SITE_TOOLS if t not in skipped]
    page_tools = [t for t in PAGE_TOOLS if t not in skipped]

    site: dict[str, Any] = {}

    # Run robots first and separately because it declares the sitemap URL(s). Without
    # this step, sitemap-crawl receives the homepage, returns "Unknown sitemap
    # format", and the audit silently falls back to a single page.
    sitemap_url = ""
    sitemap_urls: list[str] = []
    if "robots_check" in site_tools:
        site["robots_check"] = _run(tools["robots_check"], url=start)
        sitemap_urls = list(site["robots_check"].get("sitemaps") or [])
        sitemap_url = sitemap_urls[0] if sitemap_urls else ""
        site_tools = [t for t in site_tools if t != "robots_check"]
    if not sitemap_url:
        sitemap_url = start.rstrip("/") + "/sitemap.xml"
        sitemap_urls = [sitemap_url]

    if "sitemap_crawl" in site_tools and len(sitemap_urls) > 1:
        # robots.txt can declare several independent Sitemap: directives (e.g. one
        # per content type); each is its own document, so each needs its own fetch.
        # Page selection below then samples from their combined url set instead of
        # only ever seeing whichever source happened to be declared first.
        seen: set[str] = set()
        merged_urls: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        truncated = False
        for one in sitemap_urls:
            fetched = _run(tools["sitemap_crawl"], url=one)
            for entry in fetched.get("urls") or []:
                loc = entry.get("loc") if isinstance(entry, dict) else entry
                if loc and loc not in seen:
                    seen.add(loc)
                    merged_urls.append(entry)
            for err in fetched.get("errors") or []:
                errors.append(err if isinstance(err, dict) else {"url": one, "error": str(err)})
            if fetched.get("truncated"):
                truncated = True
            if fetched.get("ok") is False:
                errors.append({"url": one, "error": str(fetched.get("error") or "fetch failed")})
        # A root with no URLs and no errors is a legitimately empty sitemap (the
        # #200 all-successful case); a root with errors and nothing collected is a
        # real failure and must not read as clean evidence.
        merged: dict[str, Any] = {
            "ok": bool(merged_urls) or not errors,
            "urls": merged_urls,
            "sources": sitemap_urls,
        }
        if errors:
            merged["errors"] = errors
            if merged["ok"]:
                # Some roots still produced URLs, so this is not a hard failure —
                # but the unavailable roots must still surface to report writers
                # instead of disappearing behind the successful ones.
                # The wording is not free: classify() matches SEVERITY_RULES by
                # plain substring, and its marker is "unavailable or return
                # errors". Writing "returned" here instead left this finding
                # classified as a notice -- sorted to the bottom of every report,
                # under exactly the partial evidence it exists to raise.
                merged["findings"] = [
                    "Sitemap source unavailable or return errors: "
                    f"{err.get('url') or '(unknown root)'} — {err.get('error')}"
                    for err in errors
                ]
        if truncated:
            merged["truncated"] = True
        site["sitemap_crawl"] = merged
        site_tools = [t for t in site_tools if t != "sitemap_crawl"]

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            tool: pool.submit(
                _run,
                tools[tool],
                **_site_kwargs(tool, start, domain, render, sitemap_url),
            )
            for tool in site_tools
            if tool in tools
        }
        for tool, future in futures.items():
            site[tool] = future.result()

    page_urls = [normalize_url(u) for u in (urls or []) if u and str(u).strip()]
    if not page_urls:
        page_urls = _urls_from_sitemap(site.get("sitemap_crawl") or {}, limit)
    if not page_urls:
        page_urls = [start]  # An empty sitemap still warrants a homepage analysis.
    page_urls = page_urls[:limit]

    def _one_page(page_url: str) -> tuple[dict[str, Any], set[str]]:
        results = {tool: _run(tools[tool], url=page_url) for tool in page_tools if tool in tools}
        failed_tools = {
            tool
            for tool, data in results.items()
            if isinstance(data, dict) and data.get("ok") is False
        }
        return _page_row(page_url, results), failed_tools

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        page_results = list(pool.map(_one_page, page_urls))
    pages = [row for row, _ in page_results]

    # A page tool that raises for every sampled page hides just as much evidence
    # as a site-level tool failure and must be counted the same way, not left to
    # whatever severity classify() happens to assign a raw exception message.
    page_tool_failure_counts: dict[str, int] = {}
    for _, failed_tools in page_results:
        for tool in failed_tools:
            page_tool_failure_counts[tool] = page_tool_failure_counts.get(tool, 0) + 1
    page_tools_failed = [
        {"tool": tool, "failed_pages": count, "pages_checked": len(pages)}
        for tool, count in sorted(page_tool_failure_counts.items())
        if pages and count == len(pages)
    ]
    fully_failed_page_tools = {row["tool"] for row in page_tools_failed}

    findings: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for tool, data in site.items():
        if not isinstance(data, dict):
            continue
        if data.get("ok") is False:
            failed.append({"tool": tool, "error": str(data.get("error"))[:300]})
            continue
        for text in data.get("findings") or []:
            findings.append({"source": tool, "severity": classify(str(text)), "text": str(text)})
    for page in pages:
        for text in page.get("issues", []):
            text_str = str(text)
            severity = classify(text_str)
            if any(text_str.startswith(f"{tool}: ") for tool in fully_failed_page_tools):
                # This tool produced zero usable evidence for the whole crawl -- a raw
                # exception message must not be allowed to fall through to "notice"
                # purely because its text doesn't match a SEVERITY_RULES marker.
                severity = "critical"
            findings.append(
                {
                    "source": "page",
                    "url": page["url"],
                    "severity": severity,
                    "text": text_str,
                }
            )

    # Duplicate titles become visible only after pages are assembled; no isolated
    # page tool can detect them. This cross-page step caught duplicate titles in
    # the very first production audit.
    titles: dict[str, list[str]] = {}
    for page in pages:
        title = (page.get("title") or "").strip()
        if title:
            titles.setdefault(title, []).append(page["url"])
    for title, urls_with_title in titles.items():
        if len(urls_with_title) > 1:
            findings.append(
                {
                    "source": "pages",
                    "severity": "warning",
                    "text": f"The same title is used by {len(urls_with_title)} pages "
                    f"({title[:60]}…): {', '.join(urls_with_title[:3])}",
                }
            )

    order = {"critical": 0, "warning": 1, "notice": 2}
    findings.sort(key=lambda f: order.get(f["severity"], 3))
    by_severity = {
        level: sum(1 for f in findings if f["severity"] == level)
        for level in ("critical", "warning", "notice")
    }

    return {
        "ok": True,
        "schema": SCHEMA,
        "url": start,
        "domain": domain,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site": site,
        "pages": pages,
        "findings": findings,
        "summary": {
            "pages_checked": len(pages),
            "findings_total": len(findings),
            "findings_by_severity": by_severity,
            "tools_run": sorted(
                tool
                for tool, data in site.items()
                if not (isinstance(data, dict) and data.get("ok") is False)
            ),
            "tools_failed": failed,
            "page_tools_failed": page_tools_failed,
            "severity_note": "Severity is assigned by the aggregation policy "
            "(SEVERITY_RULES); it is not measured by the source tool.",
        },
    }
