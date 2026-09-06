"""Local stdio MCP server exposing the complete SEOHEAD Tools capability set.

One connector provides live URL and content tools, domain and infrastructure reconnaissance,
external demand and traffic providers, report generation, and Screaming Frog crawl-export audits.
The transport is local stdio only: it does not open a network port or expose a hosted endpoint.

Run ``python -m seohead.servers.mcp_server`` or ``seohead mcp``.
Requires the optional ``mcp`` dependency: ``pip install "seohead-seotools[mcp]"``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from seohead import runlog
from seohead.models import ParseManyResult, RobotsCheckResult
from seohead.servers import handlers


def _all_parse_results_failed(result: Any) -> bool:
    """``handlers.parse`` returns a ``ParseManyResult`` (``{"count", "results": [...]}``) with no
    top-level ``ok`` key — only each item in ``results`` carries its own. ``handlers.handler_failed``
    only ever looks at the top level, so it never catches this shape. Total failure (every
    requested URL failed) must not read as success; a partial failure (some ok, some not) stays a
    normal result so the per-item errors remain visible to the caller.
    """
    if not isinstance(result, dict):
        return False
    items = result.get("results")
    if not isinstance(items, list) or not items:
        return False
    return all(isinstance(r, dict) and r.get("ok") is False for r in items)


def _checked(result: Any) -> Any:
    """Raise so FastMCP marks the call ``isError`` instead of returning a handler's own-reported
    failure (``ok: False``, see ``handlers.handler_failed``) as a normal success — the same
    distinction the CLI makes with a non-zero exit (docs/USAGE.md). Every ``return handlers.*``
    call below passes through here rather than each ``@mcp.tool`` decorator wrapping its own
    function, because ``tool_reference.py`` reads that decorator's literal shape with `ast` to
    generate docs/TOOL_REFERENCE.md and must keep finding it unchanged.
    """
    from mcp.server.fastmcp.exceptions import ToolError

    if handlers.handler_failed(result) or _all_parse_results_failed(result):
        raise ToolError(json.dumps(result, ensure_ascii=False, default=str))
    return result


def build_server():  # -> FastMCP
    runlog.set_interface("mcp")
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    mcp = FastMCP("SEOHEAD Tools")

    pure = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    fetch = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
    )
    read_files = ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    create_files = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
    create_files_from_web = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
    rewrite_files = ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
    paid = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
    submit = ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
    )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_parse(
        url: str = "", urls: list[str] | None = None, options: dict[str, Any] | None = None
    ) -> ParseManyResult:
        """Parse SEO data (title, meta description, canonical, OG/Twitter, H1-H6,
        JSON-LD, links, visible text, word count) from one URL or a list of URLs."""
        return _checked(handlers.parse(url=url or None, urls=urls, options=options))

    @mcp.tool(annotations=pure, structured_output=True)
    def seo_redirects_generate(
        redirects: list[dict],
        fmt: str = "apache-rewrite-rule",
        default_url: str = "/",
        custom_template: str = "",
    ) -> dict[str, Any]:
        """Generate redirect rules (Apache mod_rewrite/Redirect, Nginx, or a custom
        template) from a list of {old_url, new_url} pairs."""
        return _checked(
            handlers.redirects_generate(
                redirects=redirects,
                fmt=fmt,
                default_url=default_url,
                custom_template=custom_template,
            )
        )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_redirects_check(url: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        """Follow a live redirect chain for a URL and report each hop (status, location)."""
        return _checked(handlers.redirects_check(url=url, options=options))

    @mcp.tool(annotations=create_files_from_web, structured_output=True)
    def seo_crawl_site(
        url: str = "",
        urls: list[str] | None = None,
        sitemap: str | None = None,
        config: str | None = None,
        max_urls: int | None = None,
        max_depth: int | None = None,
        min_delay: float | None = None,
        robots: str | None = None,
        concurrency: int | None = None,
        out_dir: str | None = None,
        scan_out: str | None = None,
        producer_build: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Crawl a site from a start URL by following links, or fetch an explicit
        ``urls`` list instead of following links at all, then audit the result
        through the same checks used for Screaming Frog exports. One of ``url``
        or ``urls`` is required. Same host only when following links, politeness
        adapts to the origin. Checks whose evidence a native crawl cannot produce
        are reported as skipped, never as clean.

        Pass ``urls`` instead of ``url`` for list mode: fetch exactly that set,
        depth 0, no link discovery -- the migration-audit shape (a redirect map,
        a Search Console export). ``max_depth`` and ``concurrency`` have nothing
        to discover in that mode and are ignored.

        ``robots`` is "respect" (obey), "report_only" (fetch robots.txt, crawl
        anyway, and report what a compliant crawler would have missed) or
        "ignore" (do not fetch it at all) -- applied in list mode too, and named
        in the result's ``discovery.directive_policy``, not only enforced
        silently. ``concurrency`` is a per-origin ceiling the adaptive throttle
        grows into, not a fixed thread count. ``sitemap`` seeds the crawl from a
        sitemap's declared URLs in addition to following links from ``url``, and
        reconciles the two sources (declared vs. observed). ``config`` is a path
        to a crawler config file (JSON) on this machine, the same file
        ``crawl-site --config`` reads. ``max_urls``, ``max_depth``, ``min_delay``,
        ``robots`` and ``concurrency`` are left unset by default: an omitted
        override preserves whatever ``config`` (or its own defaults) already
        says, exactly like the CLI's flags -- pass one explicitly only to
        change that one setting. ``seo_crawl_describe_settings`` lists the
        defaults each of them falls back to.

        ``scan_out`` opts into a SQLite scan file with bounded collection and
        resume; it cannot be combined with list mode or ``out_dir``. It requires
        raw rendering and cache off. G captures bounded HTML entity bytes and separate rendered
        DOM evidence when available; resource fetching and offline replay are unavailable. Audit creation has an explicit
        compatibility population limit and may return unavailable. Pass the
        producing source SHA in ``producer_build`` if the installed build cannot
        determine it from a clean checkout. No response bodies are retained."""
        return _checked(
            handlers.crawl_site(
                url=url or None,
                urls=urls,
                sitemap=sitemap,
                config=config,
                max_urls=max_urls,
                max_depth=max_depth,
                min_delay=min_delay,
                robots=robots,
                concurrency=concurrency,
                out_dir=out_dir,
                scan_out=scan_out,
                producer_build=producer_build,
                overrides=overrides,
            )
        )

    @mcp.tool(annotations=pure, structured_output=True)
    def seo_crawl_describe_settings() -> dict[str, Any]:
        """List every crawl-site config setting: dotted path, type, default value,
        description, and whether it changes what the audit finds (results-affecting)
        or only cost/duration. The same source ``crawl-site --config-help`` reads, so
        an agent can discover the configuration surface without a filesystem."""
        return _checked(handlers.crawl_describe_settings())

    @mcp.tool(annotations=pure, structured_output=True)
    def seo_log_scan(
        run: str, images_dir: str | None = None, max_per_rule: int = 20
    ) -> dict[str, Any]:
        """Report claims a finished run makes that cannot all be true at once: a recorded
        size that disagrees with the file, a check firing more often than there are pages
        to fire on, a finding about a URL the run never fetched, a summary that disagrees
        with its own rows. Not a second audit and not a threshold — only contradictions,
        each naming both values and where each came from, so a surprising number can be
        traced instead of trusted. ``run`` is a directory holding audit.json and/or
        pages.jsonl; ``images_dir`` is an images-download directory whose manifest lets a
        recorded size be checked against the bytes on disk."""
        return _checked(
            handlers.log_scan(run=run, images_dir=images_dir, max_per_rule=max_per_rule)
        )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_sitemap_crawl(url: str, concurrency: int = 3) -> dict[str, Any]:
        """Recursively parse a sitemap (index/urlset, gzip supported) into a URL tree,
        with duplicate detection."""
        return _checked(handlers.sitemap_crawl(url=url, concurrency=concurrency))

    @mcp.tool(annotations=create_files_from_web, structured_output=True)
    def seo_images_download(
        urls: list[str], output_dir: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Download images from a URL list, setting the correct extension by
        content-type and skipping already-downloaded files."""
        return _checked(handlers.images_download(urls=urls, output_dir=output_dir, options=options))

    @mcp.tool(annotations=rewrite_files, structured_output=True)
    def seo_images_optimize(
        files: list[str], settings: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Compress, convert, or resize raster images and conservatively minify SVG.

        Safe default: settings.out_dir is required. Source mutation needs
        settings.in_place=true and creates a backup by default. Existing destinations
        require settings.overwrite=true. Animated and multipage images are rejected.
        """
        return _checked(handlers.images_optimize(files=files, settings=settings))

    @mcp.tool(annotations=pure, structured_output=True)
    def seo_keywords_cluster(
        keywords: list[str], algorithm: str = "kmeans", n_clusters: int | None = None
    ) -> dict[str, Any]:
        """Cluster keywords into topic groups (K-Means, DBSCAN, Agglomerative)."""
        params: dict[str, Any] = {"keywords": keywords, "algorithm": algorithm}
        if n_clusters is not None:
            params["n_clusters"] = n_clusters
        return _checked(handlers.keywords_cluster(**params))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_robots_check(
        url: str, user_agent: str = "*", paths: list[str] | None = None
    ) -> RobotsCheckResult:
        """Fetch and analyze a site's robots.txt: user-agent groups, declared
        sitemaps, and whether given paths are crawlable."""
        return _checked(handlers.robots_check(url=url, user_agent=user_agent, paths=paths))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_headers_check(url: str, method: str = "GET") -> dict[str, Any]:
        """Inspect SEO-relevant response headers (X-Robots-Tag, canonical Link,
        Cache-Control, HSTS, ...), HTTP version, TTFB, and body size."""
        return _checked(handlers.headers_check(url=url, method=method))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_asset_weight_check(url: str, file_size_threshold: int | None = None) -> dict[str, Any]:
        """Fetch a page's linked CSS/JS and report delivery problems: render-blocking
        resources in <head>, oversized files, duplicate libraries bundled more than
        once (by content hash), missing minification, missing font-display, legacy
        polyfilled JS, and resources served without compression or a long-lived
        Cache-Control. Unused-code and cross-page outlier detection need a rendered
        DOM / a multi-page run and are reported under `skipped`, not silently clean."""
        return _checked(
            handlers.asset_weight_check(url=url, file_size_threshold=file_size_threshold)
        )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_links_check(url: str, internal_only: bool = False, limit: int = 200) -> dict[str, Any]:
        """Check a page's outbound links for broken (4xx/5xx) targets and links
        that point at redirects (wasted crawl hops)."""
        return _checked(handlers.links_check(url=url, internal_only=internal_only, limit=limit))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_hreflang_check(url: str) -> dict[str, Any]:
        """Extract and validate a page's hreflang alternates (x-default,
        self-reference, duplicates, malformed codes)."""
        return _checked(handlers.hreflang_check(url=url))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_domain_profile(domain: str, with_tls: bool = True) -> dict[str, Any]:
        """Infrastructure profile of a domain: registrar and domain age (RDAP, whois
        fallback), DNS records with DNS/mail provider, hosting IP with ASN, owner and
        country, reverse DNS, TLS certificate and its expiry, plus risk flags."""
        return _checked(handlers.domain_profile(domain=domain, with_tls=with_tls))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_cdn_check(url: str) -> dict[str, Any]:
        """Which CDN sits in front of a URL and whether caching actually works:
        cache status on a repeat request (MISS->HIT), Cache-Control, ETag/Last-Modified,
        304 revalidation, HTTP version, HTTP/3 advertisement, brotli/gzip and TTFB."""
        return _checked(handlers.cdn_check(url=url))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_tech_detect(url: str) -> dict[str, Any]:
        """Detect the technologies behind a page: CMS, framework, server stack,
        analytics and ad pixels, chat widgets, consent tools, fonts and third-party
        script hosts. Every hit carries the marker it was detected by."""
        return _checked(handlers.tech_detect(url=url))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_security_check(url: str, probe_paths: bool = False) -> dict[str, Any]:
        """Security headers with a score and grade (HSTS, CSP, X-Frame-Options,
        X-Content-Type-Options, Referrer-Policy, Permissions-Policy), software version
        disclosure, cookie flags and the http->https upgrade. Set probe_paths=true to
        also check whether .git/.env and similar service files are exposed."""
        return _checked(handlers.security_check(url=url, probe_paths=probe_paths))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_backlinks_check(target: str, donors: list[str], concurrency: int = 3) -> dict[str, Any]:
        """Verify backlinks from a list of donor pages: is the link still there, its
        anchor and rel, whether it passes weight (nofollow/ugc/sponsored), and whether
        the donor page itself is indexable."""
        return _checked(
            handlers.backlinks_check(target=target, donors=donors, concurrency=concurrency)
        )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_schema_check(url: str = "", html: str = "") -> dict[str, Any]:
        """Validate a page's structured data in two layers. Layer one is the Schema.org
        vocabulary itself (1010 types, 1676 properties, shipped with the toolkit): does
        the type exist, is the property allowed on it once inheritance is resolved, does
        the value type match, is the term deprecated or still in the pending layer.
        Layer two is Google rich-result eligibility per type. Also analyses the JSON-LD
        as a GRAPH: which entities carry @id, which are linked, which hang as islands,
        and whether any @id reference dangles. Pass html to check markup offline."""
        return _checked(handlers.schema_check(url=url or None, html=html or None))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_schema_build(url: str = "", html: str = "", override_type: str = "") -> dict[str, Any]:
        """Suggest a connected Schema.org @graph for a page. Classifies the page
        (Article/Product/Service/LocalBusiness/...) from URL + content + existing
        JSON-LD, shows the signals it used and its confidence, then builds a linked
        graph (Organization <- WebSite <- WebPage <- <type>) using ONLY facts
        actually visible on the page. Also diffs the suggestion against markup the
        page already carries: which recommended fields are missing, which it can
        fill now. Set override_type when the classifier is unsure (confidence=low)."""
        return _checked(
            handlers.schema_build(
                url=url or None, html=html or None, override_type=override_type or None
            )
        )

    @mcp.tool(annotations=pure, structured_output=True)
    def seo_duplicate_check(
        items: list[dict],
        threshold: float = 0.92,
        with_fingerprints: bool = False,
        only_indexable: bool = True,
    ) -> dict[str, Any]:
        """Find near-duplicate pages among a list of {id, text} documents using
        simhash + locality-sensitive hashing (no O(n^2) pairwise comparison).
        Returns exact duplicates (by content hash) separately from near-duplicate
        clusters (similarity at or above the threshold, with exact pairwise
        similarity inside each cluster), so a byte-identical pair is never reported
        twice. Feed it page texts from a crawl (SF export, sitemap + parse) to
        surface thin/duplicate content on large sites; ideally each item's text is
        already scoped to the page's content area (see seo_markdown_extract or
        parse's content_text field), so shared navigation and footer boilerplate
        does not create false matches. only_indexable=True (default) compares only
        items whose indexable flag is true or absent, since a page canonicalised to
        another is an intended twin, not a defect; set it to false to audit the
        canonical tags themselves."""
        return _checked(
            handlers.duplicate_check(
                items=items,
                threshold=threshold,
                with_fingerprints=with_fingerprints,
                only_indexable=only_indexable,
            )
        )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_mirror_check(url: str, timeout: float = 12.0) -> dict[str, Any]:
        """Audit host consolidation across HTTP/HTTPS, ``www``, index.php/index.html,
        case, and trailing-slash variants. Returns every redirect hop and identifies live
        200 duplicates, HTTPS-to-HTTP downgrades, chains longer than one hop, dead variants,
        and ``www`` DNS availability. DNS is checked through DNS-over-HTTPS rather than the
        machine's local resolver so local cache or split-DNS state does not create false evidence.
        """
        return _checked(handlers.mirror_check(url=url, timeout=timeout))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_ai_bots_check(url: str = "", robots_text: str = "") -> dict[str, Any]:
        """Which AI crawlers (GPTBot, ClaudeBot, Perplexity, Google-Extended, CCBot,
        Bytespider, Meta-ExternalAgent, …) the site lets in, and which it blocks in
        robots.txt. For each bot: its role (training/retrieval/user), whether it has an
        explicit robots group, and whether the root path is blocked. Pass robots_text to
        check offline; otherwise it fetches /robots.txt from the url's host."""
        return _checked(handlers.ai_bots_check(url=url or None, robots_text=robots_text or None))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_llms_txt_check(url: str, brand: str = "") -> dict[str, Any]:
        """Fetch and score the site's /llms.txt (the LLM-facing manifest): 9 checks —
        H1 title, >=3 sections, >=3 links, brand mention, category mention, product/
        proof/docs pages, size <= 60KB. Returns a 0-10 score, a letter grade, and the
        per-check breakdown. A missing llms.txt is itself a finding (no AI-ready context).
        Set brand to verify the project name is mentioned."""
        return _checked(handlers.llms_txt_check(url=url, brand=brand or None))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_citability_check(
        url: str = "", text: str = "", content_area: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Score how citable a piece of content is for AI answers (GEO/AEO): 0-100 across
        four dimensions (25 each) — Answer Blocks (self-contained 20-200 word paragraphs),
        Self-Containment (no context-dependent phrases like 'as mentioned above'),
        Statistical Density (numbers/percentages/dates + evidence markers per 100 words),
        and Structure Quality (headings/lists/TL;DR). Pass text to score a fragment exactly
        as given, or url to fetch the page and score it: fetching scores the resolved
        content area's Markdown (navigation and footer excluded, headings/lists/paragraph
        breaks preserved), not the raw whole-document text, since a flat text blob has no
        structure for the scorer to find. content_area configures that region — see
        seo_markdown_extract / seo_parse's content_area option for its keys."""
        return _checked(
            handlers.citability_check(url=url or None, text=text or None, content_area=content_area)
        )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_markdown_extract(
        url: str = "", html: str = "", content_area: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Render a page as Markdown in two scopes. content_markdown strips navigation
        and footer (boilerplate) while keeping headings, lists, and links -- the
        representation worth diffing between crawls, feeding to content scoring
        (seo_citability_check, seo_duplicate_check), or handing to a model. full_markdown
        keeps header/nav/footer too, for reading -- Markdown has already lost the tag
        structure seo_boilerplate_report hashes, so it is not a valid input there; pass
        that tool the original html (or a hash precomputed from it) instead.
        content_area_strategy records how the region was resolved for this page. Pass
        html to render offline, or url to fetch it first. content_area configures the
        region (root/include CSS selectors, tag/selector exclusions); defaults exclude
        <nav> and <footer>."""
        return _checked(
            handlers.markdown_extract(url=url or None, html=html or None, content_area=content_area)
        )

    @mcp.tool(annotations=pure, structured_output=True)
    def seo_boilerplate_report(pages: list[dict]) -> dict[str, Any]:
        """Answer "is the boilerplate actually the same everywhere?" across a crawled
        corpus. Hashes each page's header/nav/footer markup (structure kept, not just
        text, so a link dropped from a menu still changes the hash), groups pages by
        that hash, and reports every group that is not the dominant one -- with its
        fraction of the corpus and a sample URL. Catches a nav block that lost links on
        one template, a footer never migrated on old pages, or a menu that renders
        differently under one language branch. Each page is {"url", "html"}, or
        {"url", "hash"} when the hash was already computed upstream."""
        return _checked(handlers.boilerplate_report(pages=pages))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_social_meta_check(
        url: str = "", og: dict | None = None, twitter: dict | None = None
    ) -> dict[str, Any]:
        """Check OpenGraph and Twitter Card tags against the minimum needed for a link
        preview to render: which required tags (og:title/type/url/image/image:alt,
        twitter:card/title/description/image/image:alt) are missing, and which recommended
        ones. Pass url to fetch and check, or hand in pre-extracted og/twitter dicts."""
        return _checked(handlers.social_meta_check(url=url or None, og=og, twitter=twitter))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_soft404_check(url: str) -> dict[str, Any]:
        """Detect soft-404: whether the site returns an honest 404/410 for non-existent URLs,
        or silently serves 200/3xx (which pollutes the index with junk pages). Sends two
        deterministic probes (sha256 of origin, under /.well-known/) and applies strict
        AND-logic: both 2xx/3xx -> soft-404 confirmed (warning); both 404/410 -> pass;
        anything else -> unknown. Screaming Frog cannot see this — it crawls known URLs,
        not invented ones, so this needs an active request."""
        return _checked(handlers.soft404_check(url=url))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_log_analyze(path: str, verify_bots: bool = False) -> dict[str, Any]:
        """Analyse a web server access log (Apache/Nginx Common or Combined, IIS W3C).
        Shows who actually crawls the site and how: hits per bot, bytes, unique IPs,
        response codes served to bots (they differ from what humans get more often than
        people expect), which site sections each bot family visits, top paths and a daily
        trend. Set verify_bots=true to check bot authenticity by forward-confirmed rDNS —
        a spoofed User-Agent is one line, a forged PTR of google.com is not. That check
        hits the network, and if reverse DNS is unavailable it says so instead of
        declaring every bot fake."""
        return _checked(handlers.log_analyze(path=path, verify_bots=verify_bots))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_regions_check(
        url: str, extra: list[str] | None = None, limit: int = 12, render: bool = False
    ) -> dict[str, Any]:
        """Audit a site's regional structure: subdomains (msk.site.ru), folders
        (site.ru/msk/) and satellite domains (site-msk.ru). Finds the city switcher on the
        page, recognises ~100 Russian cities by URL slug (msk/moskva/moscow all collapse to
        one region) and by Cyrillic anchor text, then fetches each regional page and reports
        what actually kills regional promotion: hosts that just redirect to the main domain
        (so the region does not exist), regional pages canonicalised to another host
        (self-removal from the index), noindex, identical content across cities, one shared
        phone number for the whole country, a missing city name in the title, and two
        schemes used at once. Satellite domains are invisible from the page — pass them in
        `extra`. Set render=true when the city switcher is drawn by JavaScript (needs
        Playwright) — on many large sites it is not in the raw HTML at all."""
        return _checked(handlers.regions_check(url=url, extra=extra, limit=limit, render=render))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_render_check(url: str, viewport: str = "desktop", wait: str = "load") -> dict[str, Any]:
        """Compare the raw server HTML with the DOM after JavaScript runs — the gap between
        them is what a non-rendering crawler loses. Reports an empty SPA shell
        (<div id="root"></div> means a robot gets a blank page), the share of text and
        internal links that appear only after JS, a title/canonical rewritten by script,
        and Schema.org markup injected client-side. Also returns lab timings (TTFB, FCP,
        LCP, CLS, load) measured in one Chromium run — these are lab numbers, not field
        Core Web Vitals from CrUX, and are labelled metrics_lab for that reason. Also
        returns dual_crawl (schema dualcrawl.v1): per-URL image/link evidence seen by only
        the raw pass or only the rendered pass, a separate question from the raw/rendered
        diff above. Requires Playwright; if it is missing the tool says so and gives the
        install command instead of failing."""
        return _checked(handlers.render_check(url=url, viewport=viewport, wait=wait))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_site_audit(
        url: str,
        urls: list[str] | None = None,
        limit: int = 25,
        concurrency: int = 5,
        render: bool = False,
        skip: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run the whole live toolkit over one site and return a single audit document
        (schema seohead.site-audit/1). Site-level tools run once (domain profile, CDN and
        cache, tech stack, security headers, robots, AI crawlers, llms.txt, regions,
        raw-vs-rendered, sitemap); page-level tools run per URL (parse, Schema.org,
        Open Graph). URLs come from the sitemap unless you pass `urls`. Every finding is
        collected into one sorted list with a severity assigned by aggregator rules — the
        document says so explicitly, because severity here is a rule, not a measurement.
        A tool that fails does NOT fail the audit: it lands in summary.tools_failed with
        its reason, so silence is never mistaken for a clean result. Feed the returned
        document straight into seo_report_build."""
        return _checked(
            handlers.site_audit(
                url=url, urls=urls, limit=limit, concurrency=concurrency, render=render, skip=skip
            )
        )

    @mcp.tool(annotations=create_files, structured_output=True)
    def seo_report_build(
        audit: dict | str, fmt: str = "xlsx", out: str | None = None
    ) -> dict[str, Any]:
        """Turn an audit document into a file: xlsx, docx, csv, md or json. Pass the dict
        returned by seo_site_audit, an SF Analyzer audit.json from sf_audit_run (or a
        path to either one's JSON, or a validated scan.v1 SQLite artifact) — both audit
        schemas are recognized and normalized before
        rendering. xlsx has four sheets with filters and a live Excel chart — for work;
        docx is prose with headings — for the client; csv is flat data for a tracker (two
        files: findings and pages); md is for reading and for git. The generators compute
        nothing and reach no network: what is not in the JSON does not appear in the
        report. A document matching neither schema is refused with ok: false naming the
        mismatch, never rendered as an empty report."""
        return _checked(handlers.report_build(audit=audit, fmt=fmt, out=out))

    @mcp.tool(annotations=pure, structured_output=True)
    def seo_compare_crawls(before: Any, after: Any) -> dict[str, Any]:
        """Diff two audit documents (dict, JSON path, or scan.v1 SQLite path) into four disjoint
        sets per finding: entered (new problem on a page that existed before),
        left (the page is still crawled and no longer matches — a real fix),
        appeared (a genuinely new page with a finding), disappeared (the page is
        not in this crawl at all, so a missing finding proves nothing). "left" and
        "disappeared" look identical in a naive diff and mean opposite things.
        Warns when the two runs used different results-affecting settings, since
        part of the difference may be the configuration rather than the site."""
        return _checked(handlers.compare_crawls(before=before, after=after))

    # --- External data providers: demand, search results, traffic, and spend ---

    @mcp.tool(annotations=paid, structured_output=True)
    def seo_keywords_expand(
        phrase: str, limit: int = 300, regions: list[str] | None = None
    ) -> dict[str, Any]:
        """Expand a seed phrase via Yandex Wordstat: refinements (left column) plus similar
        queries (right column), each with its base frequency. Frequency here is BASE, not
        exact — the API has no !/+/[] operators and base runs roughly 9x higher than exact;
        good enough for a first cut, then top up exact via seo_keywords_exact. A
        multi-region request SUMS frequency, so query regions one at a time. Paid and
        quota-bound: 100 Wordstat requests per hour."""
        return _checked(handlers.keywords_expand(phrase=phrase, limit=limit, regions=regions))

    @mcp.tool(annotations=paid, structured_output=True)
    def seo_keywords_seasonality(
        phrase: str,
        from_date: str,
        to_date: str,
        period: str = "PERIOD_MONTHLY",
        regions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Demand over time for one phrase (Yandex Wordstat dynamics). Dates are RFC3339,
        e.g. 2026-01-01T00:00:00Z. Use it to tell a dead query from a seasonal one before
        deciding a page is not worth building."""
        return _checked(
            handlers.keywords_seasonality(
                phrase=phrase, from_date=from_date, to_date=to_date, period=period, regions=regions
            )
        )

    @mcp.tool(annotations=paid, structured_output=True)
    def seo_keywords_exact(
        keywords: list[str], region: int = 225, wait: bool = True
    ) -> dict[str, Any]:
        """Exact frequency (!W) for a list of phrases via Arsenkin — the number Wordstat's
        API will not give you. Paid, spends account limits. The charge and task_id are
        journaled the moment the task is created, so a paid result is never lost: pass
        wait=false to get the task_id and collect the result later for free."""
        return _checked(handlers.keywords_exact(keywords=keywords, region=region, wait=wait))

    @mcp.tool(annotations=paid, structured_output=True)
    def seo_serp_fetch(
        query: str | None = None,
        queries: list[str] | None = None,
        region: str = "225",
        top: int = 10,
    ) -> dict[str, Any]:
        """Yandex search results for one query or a batch. Async only: the synchronous endpoint
        is materially more expensive and deliberately absent. A batch launches all operations
        at once and polls them together, so N queries take one batch's time, not N times
        one. Use it to see who actually ranks before promising a client a position."""
        return _checked(handlers.serp_fetch(query=query, queries=queries, region=region, top=top))

    @mcp.tool(annotations=paid, structured_output=True)
    def seo_google_keywords(
        keywords: list[str] | None = None,
        seed: str | None = None,
        location_code: int = 2840,
        language: str = "en",
        country: str | None = None,
        limit: int = 100,
        difficulty: bool = False,
    ) -> dict[str, Any]:
        """Google demand via DataForSEO: pass `keywords` for search volume and competition on a
        list, or `seed` to expand semantics from one phrase (the Google counterpart of
        Wordstat's left column). `difficulty=true` returns keyword difficulty instead of volume.

        IMPORTANT: DataForSEO does not support locations in Russia or Belarus.
        A request with such a country does NOT go out — it returns a refusal naming what to use
        instead (Wordstat, Arsenkin). Use seo_keywords_expand / seo_keywords_exact for Yandex
        and the Russian-speaking market; this tool is for Google and everywhere else.

        Default environment is `sandbox`: real response shape, fake data, nothing charged.
        Production is an explicit opt-in via DATAFORSEO_ENV=prod."""
        return _checked(
            handlers.google_keywords(
                keywords=keywords,
                seed=seed,
                location_code=location_code,
                language=language,
                country=country,
                limit=limit,
                difficulty=difficulty,
            )
        )

    @mcp.tool(annotations=paid, structured_output=True)
    def seo_google_serp(
        query: str,
        location_code: int = 2840,
        language: str = "en",
        depth: int = 10,
        country: str | None = None,
    ) -> dict[str, Any]:
        """Google organic results for a query — who actually ranks. Same geo rules as
        seo_google_keywords: Russia and Belarus are not covered, use seo_serp_fetch (Yandex)
        for those. Default environment is sandbox."""
        return _checked(
            handlers.google_serp(
                query=query,
                location_code=location_code,
                language=language,
                depth=depth,
                country=country,
            )
        )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_metrika_counters() -> dict[str, Any]:
        """List the Yandex Metrika counters this token can see (id, name, site). Start here to
        get the counter_id the report tools need. Requires a Metrika OAuth token."""
        return _checked(handlers.metrika_counters())

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_metrika_setup(counter_id: str) -> dict[str, Any]:
        """How a counter is configured: goals, filters, data operations. Check this BEFORE
        drawing conclusions from traffic. Operations can silently reshape reports (URL
        parameter trimming, for instance), and with no goals configured there are no
        conversions in the data at all — so "zero conversion" in a report would be a
        consequence of setup, not a fact about the site."""
        return _checked(handlers.metrika_setup(counter_id=counter_id))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_metrika_report(
        counter_id: str,
        metrics: str,
        dimensions: str | None = None,
        date1: str = "30daysAgo",
        date2: str = "today",
        filters: str | None = None,
        sort: str | None = None,
        limit: int = 100,
        paginate: bool = False,
    ) -> dict[str, Any]:
        """What visitors actually did, as flat records. metrics and dimensions are
        comma-separated in API notation (ym:s:visits, ym:s:startURL); dates accept relative
        forms like 30daysAgo. This is the missing half of an audit: a page can be technically
        perfect and get no visits at all. paginate=true walks every page but stops at
        100 000 rows, and says so via "capped"."""
        return _checked(
            handlers.metrika_report(
                counter_id=counter_id,
                metrics=metrics,
                dimensions=dimensions,
                date1=date1,
                date2=date2,
                filters=filters,
                sort=sort,
                limit=limit,
                paginate=paginate,
            )
        )

    @mcp.tool(annotations=create_files_from_web, structured_output=True)
    def seo_regions_tree(save_to: str | None = None) -> dict[str, Any]:
        """Authoritative tree of Yandex region IDs for the regions[] parameter. This is the
        only FREE Wordstat method, so it is not journaled. Note that a multi-region request
        SUMS frequency rather than reporting per region — query regions one at a time."""
        return _checked(handlers.regions_tree(save_to=save_to))

    @mcp.tool(annotations=read_files, structured_output=True)
    def seo_spend_report(since: str | None = None) -> dict[str, Any]:
        """What the paid sources have actually charged: totals by source, by operation and
        by day, read from the local journal. Estimating spend by eye has already missed the
        provider usage was recorded, so check here before and after a large run. since is
        YYYY-MM-DD."""
        return _checked(handlers.spend_report(since=since))

    @mcp.tool(annotations=read_files, structured_output=True)
    def seo_sources_doctor() -> dict[str, Any]:
        """Which external data sources are ready to use: whether each secret is present,
        where it is read from, and where the spend journal lives. Call this before planning
        a paid run — a missing key is cheaper to find now than mid-collection."""
        return _checked(handlers.sources_doctor())

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_wayback_history(
        url: str,
        limit: int | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Every recorded Wayback Machine snapshot of a URL, oldest first: timestamp, HTTP
        status, and MIME type at capture time. Free and keyless. Answers what a crawl cannot —
        *when* a page started returning its current status, and what preceded it. A URL the
        archive never captured is not an error; it comes back with an empty snapshot list."""
        return _checked(
            handlers.wayback_history(url=url, limit=limit, from_date=from_date, to_date=to_date)
        )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_crtsh_subdomains(domain: str) -> dict[str, Any]:
        """Subdomains discovered from public Certificate Transparency logs (crt.sh). Free and
        keyless. Every TLS certificate ever issued for a domain is public record, so this finds
        hosts no page links to — the gap seo_mirror_check and seo_regions_check both currently
        rely on being told about by hand. crt.sh is a free public service without an SLA; a
        slow or unavailable response is reported as a failure, never as "zero subdomains"."""
        return _checked(handlers.crtsh_subdomains(domain=domain))

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_gsc_query(
        site_url: str,
        mode: str = "search_analytics",
        start_date: str = "28daysAgo",
        end_date: str = "today",
        dimensions: list[str] | None = None,
        row_limit: int = 1000,
        inspection_url: str | None = None,
    ) -> dict[str, Any]:
        """Google Search Console: search performance for a verified property
        (mode=search_analytics: clicks, impressions, position, CTR) or Google's own indexing
        verdict for one URL (mode=inspect_url).

        Requires an OAuth2 bearer token for an own, verified property — see seo_sources_doctor
        and docs/SETUP.md for how to obtain one. A missing token returns an explicit failure
        naming what to configure; it never fabricates a result."""
        return _checked(
            handlers.gsc_query(
                site_url=site_url,
                mode=mode,
                start_date=start_date,
                end_date=end_date,
                dimensions=dimensions,
                row_limit=row_limit,
                inspection_url=inspection_url,
            )
        )

    @mcp.tool(annotations=fetch, structured_output=True)
    def seo_crux_report(
        url: str | None = None,
        origin: str | None = None,
        form_factor: str | None = None,
        metrics: list[str] | None = None,
    ) -> dict[str, Any]:
        """Field Core Web Vitals (LCP, INP, CLS) as real Chrome users experienced them, at the
        75th percentile — the honest counterpart to seo_render_check's synthesized-score-free
        design (issue #59). Pass exactly one of url/origin. Requires a Chrome UX Report API key.
        A target with too little real-user traffic is not an error; CrUX has nothing to report
        for it, which comes back here as an empty metrics object."""
        return _checked(
            handlers.crux_report(url=url, origin=origin, form_factor=form_factor, metrics=metrics)
        )

    @mcp.tool(annotations=submit, structured_output=True)
    def seo_indexnow_submit(
        urls: list[str], host: str, key_location: str | None = None
    ) -> dict[str, Any]:
        """Push up to 10,000 changed URLs to Bing, Yandex, Naver, and Seznam in one call.

        IMPORTANT: Google has not joined IndexNow as of 2026 — this does not affect Google's
        crawl schedule. Requires a self-generated key published at https://<host>/<key>.txt
        before the first call; see docs/SETUP.md. Natural pairing: submit exactly the URLs
        seo_compare_crawls reports as new or changed."""
        return _checked(handlers.indexnow_submit(urls=urls, host=host, key_location=key_location))

    # Register Screaming Frog crawl-export tools on the same local connector.
    from seohead.servers import sf_mcp

    sf_mcp.register(mcp)

    return mcp


def main() -> int:
    """Run the stdio server; return an exit code instead of letting the caller import
    ``mcp`` itself to find out whether the server started.

    ``build_server()`` imports the optional MCP SDK lazily so the rest of the CLI stays
    usable without it (docs/SETUP.md). Before that import runs, no MCP session exists
    and nothing has reached stdout, so a missing SDK is reported as one stderr
    diagnostic naming the install command, with exit code 1 -- instead of an uncaught
    ModuleNotFoundError traceback (#366). This is the single place that diagnostic is
    produced, so both ``seohead mcp`` (seohead/cli.py) and the direct
    ``python -m seohead.servers.mcp_server`` invocation advertised above give the same
    outcome.
    """
    try:
        build_server().run()
    except ModuleNotFoundError:
        # build_server()'s only lazy import is the optional "mcp" SDK (see its own
        # docstring) -- nothing else in this path is optional, so any
        # ModuleNotFoundError reaching here is that one.
        print(
            'seohead mcp requires the optional "mcp" dependency. '
            'Install it with: pip install "seohead-seotools[mcp]"',
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
