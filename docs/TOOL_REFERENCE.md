# Tool reference

Generated from the MCP tool definitions in `seohead/servers/mcp_server.py` and `seohead/servers/sf_mcp.py` — do not edit by hand. Regenerate with:

```bash
python scripts/generate_tool_reference.py
```

**54 live/recon/data-source tools** (`seohead <command>` / `seo_<command>` on the MCP server) plus **5 crawl-audit tools** (`sf_<command>`, driven by `seohead sf ...`) — 59 in total.

Every tool shares one contract: JSON in, JSON out. A target that could not be reached comes back as `{"ok": false, "error": "..."}` instead of raising, so an unreachable site is data, not a crash.

- **Cost** — network/file/spend flags read from the tool's `ToolAnnotations` profile: whether it reaches beyond the process, whether it creates or changes files, whether repeating the call is safe, and whether it spends an external provider's quota.
- **Behavior and failure modes** — the remainder of the tool's own docstring: what it deliberately skips, what a degraded answer looks like, and what to use instead when it is the wrong tool for the job.

---

## Live URL, recon, and data-source tools

### `parse`

MCP name: `seo_parse`

Parse SEO data (title, meta description, canonical, OG/Twitter, H1-H6, JSON-LD, links, visible text, word count) from one URL or a list of URLs.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `''` |
| `urls` | `list[str] | None` | `None` |
| `options` | `dict[str, Any] | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `redirects-generate`

MCP name: `seo_redirects_generate`

Generate redirect rules (Apache mod_rewrite/Redirect, Nginx, or a custom template) from a list of {old_url, new_url} pairs.

| Argument | Type | Default |
|---|---|---|
| `redirects` | `list[dict]` | `required` |
| `fmt` | `str` | `'apache-rewrite-rule'` |
| `default_url` | `str` | `'/'` |
| `custom_template` | `str` | `''` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

### `redirects-check`

MCP name: `seo_redirects_check`

Follow a live redirect chain for a URL and report each hop (status, location).

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `options` | `dict[str, Any] | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `crawl-site`

MCP name: `seo_crawl_site`

Crawl a site from a start URL by following links, or fetch an explicit ``urls`` list instead of following links at all, then audit the result through the same checks used for Screaming Frog exports. One of ``url`` or ``urls`` is required. Same host only when following links, politeness adapts to the origin. Checks whose evidence a native crawl cannot produce are reported as skipped, never as clean.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `''` |
| `urls` | `list[str] | None` | `None` |
| `sitemap` | `str | None` | `None` |
| `config` | `str | None` | `None` |
| `max_urls` | `int | None` | `None` |
| `max_depth` | `int | None` | `None` |
| `min_delay` | `float | None` | `None` |
| `robots` | `str | None` | `None` |
| `concurrency` | `int | None` | `None` |
| `out_dir` | `str | None` | `None` |
| `scan_out` | `str | None` | `None` |
| `producer_build` | `str | None` | `None` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: no

**Behavior and failure modes**

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
raw rendering, cache off and credential-free configuration. Audit creation has an explicit
compatibility population limit and may return unavailable. Pass the
producing source SHA in ``producer_build`` if the installed build cannot
determine it from a clean checkout. No response bodies are retained.

### `crawl-describe-settings`

MCP name: `seo_crawl_describe_settings`

List every crawl-site config setting: dotted path, type, default value, description, and whether it changes what the audit finds (results-affecting) or only cost/duration. The same source ``crawl-site --config-help`` reads, so an agent can discover the configuration surface without a filesystem.

Takes no arguments.

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

### `log-scan`

MCP name: `seo_log_scan`

Report claims a finished run makes that cannot all be true at once: a recorded size that disagrees with the file, a check firing more often than there are pages to fire on, a finding about a URL the run never fetched, a summary that disagrees with its own rows. Not a second audit and not a threshold — only contradictions, each naming both values and where each came from, so a surprising number can be traced instead of trusted. ``run`` is a directory holding audit.json and/or pages.jsonl; ``images_dir`` is an images-download directory whose manifest lets a recorded size be checked against the bytes on disk.

| Argument | Type | Default |
|---|---|---|
| `run` | `str` | `required` |
| `images_dir` | `str | None` | `None` |
| `max_per_rule` | `int` | `20` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

### `sitemap-crawl`

MCP name: `seo_sitemap_crawl`

Recursively parse a sitemap (index/urlset, gzip supported) into a URL tree, with duplicate detection.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `concurrency` | `int` | `3` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `images-download`

MCP name: `seo_images_download`

Download images from a URL list, setting the correct extension by content-type and skipping already-downloaded files.

| Argument | Type | Default |
|---|---|---|
| `urls` | `list[str]` | `required` |
| `output_dir` | `str` | `required` |
| `options` | `dict[str, Any] | None` | `None` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: no

### `images-optimize`

MCP name: `seo_images_optimize`

Compress, convert, or resize raster images and conservatively minify SVG.

| Argument | Type | Default |
|---|---|---|
| `files` | `list[str]` | `required` |
| `settings` | `dict[str, Any] | None` | `None` |

**Cost** — network: no · writes files: yes · idempotent: no · spends money: no · can overwrite/remove existing data

**Behavior and failure modes**

Safe default: settings.out_dir is required. Source mutation needs
settings.in_place=true and creates a backup by default. Existing destinations
require settings.overwrite=true. Animated and multipage images are rejected.

### `keywords-cluster`

MCP name: `seo_keywords_cluster`

Cluster keywords into topic groups (K-Means, DBSCAN, Agglomerative).

| Argument | Type | Default |
|---|---|---|
| `keywords` | `list[str]` | `required` |
| `algorithm` | `str` | `'kmeans'` |
| `n_clusters` | `int | None` | `None` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

### `robots-check`

MCP name: `seo_robots_check`

Fetch and analyze a site's robots.txt: user-agent groups, declared sitemaps, and whether given paths are crawlable.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `user_agent` | `str` | `'*'` |
| `paths` | `list[str] | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `headers-check`

MCP name: `seo_headers_check`

Inspect SEO-relevant response headers (X-Robots-Tag, canonical Link, Cache-Control, HSTS, ...), HTTP version, TTFB, and body size.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `method` | `str` | `'GET'` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `asset-weight-check`

MCP name: `seo_asset_weight_check`

Fetch a page's linked CSS/JS and report delivery problems: render-blocking resources in <head>, oversized files, duplicate libraries bundled more than once (by content hash), missing minification, missing font-display, legacy polyfilled JS, and resources served without compression or a long-lived Cache-Control. Unused-code and cross-page outlier detection need a rendered DOM / a multi-page run and are reported under `skipped`, not silently clean.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `file_size_threshold` | `int | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `links-check`

MCP name: `seo_links_check`

Check a page's outbound links for broken (4xx/5xx) targets and links that point at redirects (wasted crawl hops).

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `internal_only` | `bool` | `False` |
| `limit` | `int` | `200` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `hreflang-check`

MCP name: `seo_hreflang_check`

Extract and validate a page's hreflang alternates (x-default, self-reference, duplicates, malformed codes).

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `domain-profile`

MCP name: `seo_domain_profile`

Infrastructure profile of a domain: registrar and domain age (RDAP, whois fallback), DNS records with DNS/mail provider, hosting IP with ASN, owner and country, reverse DNS, TLS certificate and its expiry, plus risk flags.

| Argument | Type | Default |
|---|---|---|
| `domain` | `str` | `required` |
| `with_tls` | `bool` | `True` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `cdn-check`

MCP name: `seo_cdn_check`

Which CDN sits in front of a URL and whether caching actually works: cache status on a repeat request (MISS->HIT), Cache-Control, ETag/Last-Modified, 304 revalidation, HTTP version, HTTP/3 advertisement, brotli/gzip and TTFB.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `tech-detect`

MCP name: `seo_tech_detect`

Detect the technologies behind a page: CMS, framework, server stack, analytics and ad pixels, chat widgets, consent tools, fonts and third-party script hosts. Every hit carries the marker it was detected by.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `security-check`

MCP name: `seo_security_check`

Security headers with a score and grade (HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy), software version disclosure, cookie flags and the http->https upgrade. Set probe_paths=true to also check whether .git/.env and similar service files are exposed.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `probe_paths` | `bool` | `False` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `backlinks-check`

MCP name: `seo_backlinks_check`

Verify backlinks from a list of donor pages: is the link still there, its anchor and rel, whether it passes weight (nofollow/ugc/sponsored), and whether the donor page itself is indexable.

| Argument | Type | Default |
|---|---|---|
| `target` | `str` | `required` |
| `donors` | `list[str]` | `required` |
| `concurrency` | `int` | `3` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `schema-check`

MCP name: `seo_schema_check`

Validate a page's structured data in two layers. Layer one is the Schema.org vocabulary itself (1010 types, 1676 properties, shipped with the toolkit): does the type exist, is the property allowed on it once inheritance is resolved, does the value type match, is the term deprecated or still in the pending layer. Layer two is Google rich-result eligibility per type. Also analyses the JSON-LD as a GRAPH: which entities carry @id, which are linked, which hang as islands, and whether any @id reference dangles. Pass html to check markup offline.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `''` |
| `html` | `str` | `''` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `schema-build`

MCP name: `seo_schema_build`

Suggest a connected Schema.org @graph for a page. Classifies the page (Article/Product/Service/LocalBusiness/...) from URL + content + existing JSON-LD, shows the signals it used and its confidence, then builds a linked graph (Organization <- WebSite <- WebPage <- <type>) using ONLY facts actually visible on the page. Also diffs the suggestion against markup the page already carries: which recommended fields are missing, which it can fill now. Set override_type when the classifier is unsure (confidence=low).

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `''` |
| `html` | `str` | `''` |
| `override_type` | `str` | `''` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `duplicate-check`

MCP name: `seo_duplicate_check`

Find near-duplicate pages among a list of {id, text} documents using simhash + locality-sensitive hashing (no O(n^2) pairwise comparison). Returns exact duplicates (by content hash) separately from near-duplicate clusters (similarity at or above the threshold, with exact pairwise similarity inside each cluster), so a byte-identical pair is never reported twice. Feed it page texts from a crawl (SF export, sitemap + parse) to surface thin/duplicate content on large sites; ideally each item's text is already scoped to the page's content area (see seo_markdown_extract or parse's content_text field), so shared navigation and footer boilerplate does not create false matches. only_indexable=True (default) compares only items whose indexable flag is true or absent, since a page canonicalised to another is an intended twin, not a defect; set it to false to audit the canonical tags themselves.

| Argument | Type | Default |
|---|---|---|
| `items` | `list[dict]` | `required` |
| `threshold` | `float` | `0.92` |
| `with_fingerprints` | `bool` | `False` |
| `only_indexable` | `bool` | `True` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

### `mirror-check`

MCP name: `seo_mirror_check`

Audit host consolidation across HTTP/HTTPS, ``www``, index.php/index.html, case, and trailing-slash variants. Returns every redirect hop and identifies live 200 duplicates, HTTPS-to-HTTP downgrades, chains longer than one hop, dead variants, and ``www`` DNS availability. DNS is checked through DNS-over-HTTPS rather than the machine's local resolver so local cache or split-DNS state does not create false evidence.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `timeout` | `float` | `12.0` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `ai-bots-check`

MCP name: `seo_ai_bots_check`

Which AI crawlers (GPTBot, ClaudeBot, Perplexity, Google-Extended, CCBot, Bytespider, Meta-ExternalAgent, …) the site lets in, and which it blocks in robots.txt. For each bot: its role (training/retrieval/user), whether it has an explicit robots group, and whether the root path is blocked. Pass robots_text to check offline; otherwise it fetches /robots.txt from the url's host.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `''` |
| `robots_text` | `str` | `''` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `llms-txt-check`

MCP name: `seo_llms_txt_check`

Fetch and score the site's /llms.txt (the LLM-facing manifest): 9 checks — H1 title, >=3 sections, >=3 links, brand mention, category mention, product/ proof/docs pages, size <= 60KB. Returns a 0-10 score, a letter grade, and the per-check breakdown. A missing llms.txt is itself a finding (no AI-ready context). Set brand to verify the project name is mentioned.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `brand` | `str` | `''` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `citability-check`

MCP name: `seo_citability_check`

Score how citable a piece of content is for AI answers (GEO/AEO): 0-100 across four dimensions (25 each) — Answer Blocks (self-contained 20-200 word paragraphs), Self-Containment (no context-dependent phrases like 'as mentioned above'), Statistical Density (numbers/percentages/dates + evidence markers per 100 words), and Structure Quality (headings/lists/TL;DR). Pass text to score a fragment exactly as given, or url to fetch the page and score it: fetching scores the resolved content area's Markdown (navigation and footer excluded, headings/lists/paragraph breaks preserved), not the raw whole-document text, since a flat text blob has no structure for the scorer to find. content_area configures that region — see seo_markdown_extract / seo_parse's content_area option for its keys.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `''` |
| `text` | `str` | `''` |
| `content_area` | `dict[str, Any] | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `markdown-extract`

MCP name: `seo_markdown_extract`

Render a page as Markdown in two scopes. content_markdown strips navigation and footer (boilerplate) while keeping headings, lists, and links -- the representation worth diffing between crawls, feeding to content scoring (seo_citability_check, seo_duplicate_check), or handing to a model. full_markdown keeps header/nav/footer too, for reading -- Markdown has already lost the tag structure seo_boilerplate_report hashes, so it is not a valid input there; pass that tool the original html (or a hash precomputed from it) instead. content_area_strategy records how the region was resolved for this page. Pass html to render offline, or url to fetch it first. content_area configures the region (root/include CSS selectors, tag/selector exclusions); defaults exclude <nav> and <footer>.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `''` |
| `html` | `str` | `''` |
| `content_area` | `dict[str, Any] | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `boilerplate-report`

MCP name: `seo_boilerplate_report`

Answer "is the boilerplate actually the same everywhere?" across a crawled corpus. Hashes each page's header/nav/footer markup (structure kept, not just text, so a link dropped from a menu still changes the hash), groups pages by that hash, and reports every group that is not the dominant one -- with its fraction of the corpus and a sample URL. Catches a nav block that lost links on one template, a footer never migrated on old pages, or a menu that renders differently under one language branch. Each page is {"url", "html"}, or {"url", "hash"} when the hash was already computed upstream.

| Argument | Type | Default |
|---|---|---|
| `pages` | `list[dict]` | `required` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

### `social-meta-check`

MCP name: `seo_social_meta_check`

Check OpenGraph and Twitter Card tags against the minimum needed for a link preview to render: which required tags (og:title/type/url/image/image:alt, twitter:card/title/description/image/image:alt) are missing, and which recommended ones. Pass url to fetch and check, or hand in pre-extracted og/twitter dicts.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `''` |
| `og` | `dict | None` | `None` |
| `twitter` | `dict | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `soft404-check`

MCP name: `seo_soft404_check`

Detect soft-404: whether the site returns an honest 404/410 for non-existent URLs, or silently serves 200/3xx (which pollutes the index with junk pages). Sends two deterministic probes (sha256 of origin, under /.well-known/) and applies strict AND-logic: both 2xx/3xx -> soft-404 confirmed (warning); both 404/410 -> pass; anything else -> unknown. Screaming Frog cannot see this — it crawls known URLs, not invented ones, so this needs an active request.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `log-analyze`

MCP name: `seo_log_analyze`

Analyse a web server access log (Apache/Nginx Common or Combined, IIS W3C). Shows who actually crawls the site and how: hits per bot, bytes, unique IPs, response codes served to bots (they differ from what humans get more often than people expect), which site sections each bot family visits, top paths and a daily trend. Set verify_bots=true to check bot authenticity by forward-confirmed rDNS — a spoofed User-Agent is one line, a forged PTR of google.com is not. That check hits the network, and if reverse DNS is unavailable it says so instead of declaring every bot fake.

| Argument | Type | Default |
|---|---|---|
| `path` | `str` | `required` |
| `verify_bots` | `bool` | `False` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `regions-check`

MCP name: `seo_regions_check`

Audit a site's regional structure: subdomains (msk.site.ru), folders (site.ru/msk/) and satellite domains (site-msk.ru). Finds the city switcher on the page, recognises ~100 Russian cities by URL slug (msk/moskva/moscow all collapse to one region) and by Cyrillic anchor text, then fetches each regional page and reports what actually kills regional promotion: hosts that just redirect to the main domain (so the region does not exist), regional pages canonicalised to another host (self-removal from the index), noindex, identical content across cities, one shared phone number for the whole country, a missing city name in the title, and two schemes used at once. Satellite domains are invisible from the page — pass them in `extra`. Set render=true when the city switcher is drawn by JavaScript (needs Playwright) — on many large sites it is not in the raw HTML at all.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `extra` | `list[str] | None` | `None` |
| `limit` | `int` | `12` |
| `render` | `bool` | `False` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `render-check`

MCP name: `seo_render_check`

Compare the raw server HTML with the DOM after JavaScript runs — the gap between them is what a non-rendering crawler loses. Reports an empty SPA shell (<div id="root"></div> means a robot gets a blank page), the share of text and internal links that appear only after JS, a title/canonical rewritten by script, and Schema.org markup injected client-side. Also returns lab timings (TTFB, FCP, LCP, CLS, load) measured in one Chromium run — these are lab numbers, not field Core Web Vitals from CrUX, and are labelled metrics_lab for that reason. Also returns dual_crawl (schema dualcrawl.v1): per-URL image/link evidence seen by only the raw pass or only the rendered pass, a separate question from the raw/rendered diff above. Requires Playwright; if it is missing the tool says so and gives the install command instead of failing.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `viewport` | `str` | `'desktop'` |
| `wait` | `str` | `'load'` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `site-audit`

MCP name: `seo_site_audit`

Run the whole live toolkit over one site and return a single audit document (schema seohead.site-audit/1). Site-level tools run once (domain profile, CDN and cache, tech stack, security headers, robots, AI crawlers, llms.txt, regions, raw-vs-rendered, sitemap); page-level tools run per URL (parse, Schema.org, Open Graph). URLs come from the sitemap unless you pass `urls`. Every finding is collected into one sorted list with a severity assigned by aggregator rules — the document says so explicitly, because severity here is a rule, not a measurement. A tool that fails does NOT fail the audit: it lands in summary.tools_failed with its reason, so silence is never mistaken for a clean result. Feed the returned document straight into seo_report_build.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `urls` | `list[str] | None` | `None` |
| `limit` | `int` | `25` |
| `concurrency` | `int` | `5` |
| `render` | `bool` | `False` |
| `skip` | `list[str] | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `report-build`

MCP name: `seo_report_build`

Turn an audit document into a file: xlsx, docx, csv, md or json. Pass the dict returned by seo_site_audit, an SF Analyzer audit.json from sf_audit_run (or a path to either one's JSON, or a validated scan.v1 SQLite artifact) — both audit schemas are recognized and normalized before rendering. xlsx has four sheets with filters and a live Excel chart — for work; docx is prose with headings — for the client; csv is flat data for a tracker (two files: findings and pages); md is for reading and for git. The generators compute nothing and reach no network: what is not in the JSON does not appear in the report. A document matching neither schema is refused with ok: false naming the mismatch, never rendered as an empty report.

| Argument | Type | Default |
|---|---|---|
| `audit` | `dict | str` | `required` |
| `fmt` | `str` | `'xlsx'` |
| `out` | `str | None` | `None` |

**Cost** — network: no · writes files: yes · idempotent: no · spends money: no

### `compare-crawls`

MCP name: `seo_compare_crawls`

Diff two audit documents (dict, JSON path, or scan.v1 SQLite path) into four disjoint sets per finding: entered (new problem on a page that existed before), left (the page is still crawled and no longer matches — a real fix), appeared (a genuinely new page with a finding), disappeared (the page is not in this crawl at all, so a missing finding proves nothing). "left" and "disappeared" look identical in a naive diff and mean opposite things. Warns when the two runs used different results-affecting settings, since part of the difference may be the configuration rather than the site.

| Argument | Type | Default |
|---|---|---|
| `before` | `Any` | `required` |
| `after` | `Any` | `required` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

### `keywords-expand`

MCP name: `seo_keywords_expand`

Expand a seed phrase via Yandex Wordstat: refinements (left column) plus similar queries (right column), each with its base frequency. Frequency here is BASE, not exact — the API has no !/+/[] operators and base runs roughly 9x higher than exact; good enough for a first cut, then top up exact via seo_keywords_exact. A multi-region request SUMS frequency, so query regions one at a time. Paid and quota-bound: 100 Wordstat requests per hour.

| Argument | Type | Default |
|---|---|---|
| `phrase` | `str` | `required` |
| `limit` | `int` | `300` |
| `regions` | `list[str] | None` | `None` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: yes, external provider quota

### `keywords-seasonality`

MCP name: `seo_keywords_seasonality`

Demand over time for one phrase (Yandex Wordstat dynamics). Dates are RFC3339, e.g. 2026-01-01T00:00:00Z. Use it to tell a dead query from a seasonal one before deciding a page is not worth building.

| Argument | Type | Default |
|---|---|---|
| `phrase` | `str` | `required` |
| `from_date` | `str` | `required` |
| `to_date` | `str` | `required` |
| `period` | `str` | `'PERIOD_MONTHLY'` |
| `regions` | `list[str] | None` | `None` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: yes, external provider quota

### `keywords-exact`

MCP name: `seo_keywords_exact`

Exact frequency (!W) for a list of phrases via Arsenkin — the number Wordstat's API will not give you. Paid, spends account limits. The charge and task_id are journaled the moment the task is created, so a paid result is never lost: pass wait=false to get the task_id and collect the result later for free.

| Argument | Type | Default |
|---|---|---|
| `keywords` | `list[str]` | `required` |
| `region` | `int` | `225` |
| `wait` | `bool` | `True` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: yes, external provider quota

### `serp-fetch`

MCP name: `seo_serp_fetch`

Yandex search results for one query or a batch. Async only: the synchronous endpoint is materially more expensive and deliberately absent. A batch launches all operations at once and polls them together, so N queries take one batch's time, not N times one. Use it to see who actually ranks before promising a client a position.

| Argument | Type | Default |
|---|---|---|
| `query` | `str | None` | `None` |
| `queries` | `list[str] | None` | `None` |
| `region` | `str` | `'225'` |
| `top` | `int` | `10` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: yes, external provider quota

### `google-keywords`

MCP name: `seo_google_keywords`

Google demand via DataForSEO: pass `keywords` for search volume and competition on a list, or `seed` to expand semantics from one phrase (the Google counterpart of Wordstat's left column). `difficulty=true` returns keyword difficulty instead of volume.

| Argument | Type | Default |
|---|---|---|
| `keywords` | `list[str] | None` | `None` |
| `seed` | `str | None` | `None` |
| `location_code` | `int` | `2840` |
| `language` | `str` | `'en'` |
| `country` | `str | None` | `None` |
| `limit` | `int` | `100` |
| `difficulty` | `bool` | `False` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: yes, external provider quota

**Behavior and failure modes**

IMPORTANT: DataForSEO does not support locations in Russia or Belarus.
A request with such a country does NOT go out — it returns a refusal naming what to use
instead (Wordstat, Arsenkin). Use seo_keywords_expand / seo_keywords_exact for Yandex
and the Russian-speaking market; this tool is for Google and everywhere else.

Default environment is `sandbox`: real response shape, fake data, nothing charged.
Production is an explicit opt-in via DATAFORSEO_ENV=prod.

### `google-serp`

MCP name: `seo_google_serp`

Google organic results for a query — who actually ranks. Same geo rules as seo_google_keywords: Russia and Belarus are not covered, use seo_serp_fetch (Yandex) for those. Default environment is sandbox.

| Argument | Type | Default |
|---|---|---|
| `query` | `str` | `required` |
| `location_code` | `int` | `2840` |
| `language` | `str` | `'en'` |
| `depth` | `int` | `10` |
| `country` | `str | None` | `None` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: yes, external provider quota

### `metrika-counters`

MCP name: `seo_metrika_counters`

List the Yandex Metrika counters this token can see (id, name, site). Start here to get the counter_id the report tools need. Requires a Metrika OAuth token.

Takes no arguments.

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `metrika-setup`

MCP name: `seo_metrika_setup`

How a counter is configured: goals, filters, data operations. Check this BEFORE drawing conclusions from traffic. Operations can silently reshape reports (URL parameter trimming, for instance), and with no goals configured there are no conversions in the data at all — so "zero conversion" in a report would be a consequence of setup, not a fact about the site.

| Argument | Type | Default |
|---|---|---|
| `counter_id` | `str` | `required` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `metrika-report`

MCP name: `seo_metrika_report`

What visitors actually did, as flat records. metrics and dimensions are comma-separated in API notation (ym:s:visits, ym:s:startURL); dates accept relative forms like 30daysAgo. This is the missing half of an audit: a page can be technically perfect and get no visits at all. paginate=true walks every page but stops at 100 000 rows, and says so via "capped".

| Argument | Type | Default |
|---|---|---|
| `counter_id` | `str` | `required` |
| `metrics` | `str` | `required` |
| `dimensions` | `str | None` | `None` |
| `date1` | `str` | `'30daysAgo'` |
| `date2` | `str` | `'today'` |
| `filters` | `str | None` | `None` |
| `sort` | `str | None` | `None` |
| `limit` | `int` | `100` |
| `paginate` | `bool` | `False` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `regions-tree`

MCP name: `seo_regions_tree`

Authoritative tree of Yandex region IDs for the regions[] parameter. This is the only FREE Wordstat method, so it is not journaled. Note that a multi-region request SUMS frequency rather than reporting per region — query regions one at a time.

| Argument | Type | Default |
|---|---|---|
| `save_to` | `str | None` | `None` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: no

### `spend-report`

MCP name: `seo_spend_report`

What the paid sources have actually charged: totals by source, by operation and by day, read from the local journal. Estimating spend by eye has already missed the provider usage was recorded, so check here before and after a large run. since is YYYY-MM-DD.

| Argument | Type | Default |
|---|---|---|
| `since` | `str | None` | `None` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

### `sources-doctor`

MCP name: `seo_sources_doctor`

Which external data sources are ready to use: whether each secret is present, where it is read from, and where the spend journal lives. Call this before planning a paid run — a missing key is cheaper to find now than mid-collection.

Takes no arguments.

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

### `wayback-history`

MCP name: `seo_wayback_history`

Every recorded Wayback Machine snapshot of a URL, oldest first: timestamp, HTTP status, and MIME type at capture time. Free and keyless. Answers what a crawl cannot — *when* a page started returning its current status, and what preceded it. A URL the archive never captured is not an error; it comes back with an empty snapshot list.

| Argument | Type | Default |
|---|---|---|
| `url` | `str` | `required` |
| `limit` | `int | None` | `None` |
| `from_date` | `str | None` | `None` |
| `to_date` | `str | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `crtsh-subdomains`

MCP name: `seo_crtsh_subdomains`

Subdomains discovered from public Certificate Transparency logs (crt.sh). Free and keyless. Every TLS certificate ever issued for a domain is public record, so this finds hosts no page links to — the gap seo_mirror_check and seo_regions_check both currently rely on being told about by hand. crt.sh is a free public service without an SLA; a slow or unavailable response is reported as a failure, never as "zero subdomains".

| Argument | Type | Default |
|---|---|---|
| `domain` | `str` | `required` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `gsc-query`

MCP name: `seo_gsc_query`

Google Search Console: search performance for a verified property (mode=search_analytics: clicks, impressions, position, CTR) or Google's own indexing verdict for one URL (mode=inspect_url).

| Argument | Type | Default |
|---|---|---|
| `site_url` | `str` | `required` |
| `mode` | `str` | `'search_analytics'` |
| `start_date` | `str` | `'28daysAgo'` |
| `end_date` | `str` | `'today'` |
| `dimensions` | `list[str] | None` | `None` |
| `row_limit` | `int` | `1000` |
| `inspection_url` | `str | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

**Behavior and failure modes**

Requires an OAuth2 bearer token for an own, verified property — see seo_sources_doctor
and docs/SETUP.md for how to obtain one. A missing token returns an explicit failure
naming what to configure; it never fabricates a result.

### `crux-report`

MCP name: `seo_crux_report`

Field Core Web Vitals (LCP, INP, CLS) as real Chrome users experienced them, at the 75th percentile — the honest counterpart to seo_render_check's synthesized-score-free design (issue #59). Pass exactly one of url/origin. Requires a Chrome UX Report API key. A target with too little real-user traffic is not an error; CrUX has nothing to report for it, which comes back here as an empty metrics object.

| Argument | Type | Default |
|---|---|---|
| `url` | `str | None` | `None` |
| `origin` | `str | None` | `None` |
| `form_factor` | `str | None` | `None` |
| `metrics` | `list[str] | None` | `None` |

**Cost** — network: yes · writes files: no · idempotent: yes · spends money: no

### `indexnow-submit`

MCP name: `seo_indexnow_submit`

Push up to 10,000 changed URLs to Bing, Yandex, Naver, and Seznam in one call.

| Argument | Type | Default |
|---|---|---|
| `urls` | `list[str]` | `required` |
| `host` | `str` | `required` |
| `key_location` | `str | None` | `None` |

**Cost** — network: yes · writes files: yes · idempotent: yes · spends money: no

**Behavior and failure modes**

IMPORTANT: Google has not joined IndexNow as of 2026 — this does not affect Google's
crawl schedule. Requires a self-generated key published at https://<host>/<key>.txt
before the first call; see docs/SETUP.md. Natural pairing: submit exactly the URLs
seo_compare_crawls reports as new or changed.

---

## Crawl-audit tools (Screaming Frog)

### `sf_audit_run`

Run an SF audit and write audit.json plus audit.md.

| Argument | Type | Default |
|---|---|---|
| `mode` | `str` | `required` |
| `input` | `str` | `required` |
| `profile` | `str` | `'full'` |
| `out` | `str` | `'report'` |
| `config` | `str | None` | `None` |
| `sitemap` | `str | None` | `None` |

**Cost** — network: yes · writes files: yes · idempotent: no · spends money: no

**Behavior and failure modes**

Use ``parse-exports`` with a directory of existing CSV/XLSX exports;
this mode does not require Screaming Frog to be installed. The
``load-crawl``, ``crawl``, and ``crawl-list`` modes invoke the separately
installed, licensed SF CLI. ``input`` is respectively an exports
directory, .seospider path, start URL, or URL-list file. Returns a compact
summary and absolute file paths instead of embedding the large reports.

A live crawl can run for a long time; this call runs off the server's
event loop, so other sf_* calls stay answerable while it is in
progress, and cancelling the request (an MCP CancelledNotification)
stops the underlying Screaming Frog process instead of leaving it
running. A cancelled or failed run never leaves a completed-looking
``audit.json``/``audit.md`` behind; on either outcome, or a lost
response, recover by pointing sf_list_exports/sf_audit_summary at the
same ``out`` path.

### `sf_audit_summary`

Read a compact health summary from audit.json or a scan.v1 SQLite artifact.

| Argument | Type | Default |
|---|---|---|
| `json_path` | `str` | `required` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

**Behavior and failure modes**

Returns the project, health score, severity counts, the first 15 ranked
checks, and sitemap statistics without loading the complete issue list
into the agent context.

### `sf_audit_issues`

Return filtered issues from audit.json or a validated scan.v1 SQLite artifact.

| Argument | Type | Default |
|---|---|---|
| `json_path` | `str` | `required` |
| `check` | `str | None` | `None` |
| `severity` | `str | None` | `None` |
| `limit` | `int` | `50` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

**Behavior and failure modes**

The requested limit is clamped to 1..1000 to keep MCP payloads bounded.
Omit both filters to inspect the first issues in report order.

### `sf_list_exports`

Discover recognized SF exports in a directory.

| Argument | Type | Default |
|---|---|---|
| `exports_dir` | `str` | `required` |

**Cost** — network: no · writes files: no · idempotent: yes · spends money: no

**Behavior and failure modes**

Returns the matched files by logical export name and the logical exports
that are absent, allowing an agent to explain which checks may be skipped
before running the audit.

### `sf_audit_tasks`

Build tasks.json and tasks.md from audit.json or a scan.v1 SQLite artifact.

| Argument | Type | Default |
|---|---|---|
| `json_path` | `str` | `required` |
| `out` | `str` | `'report'` |
| `config` | `str | None` | `None` |

**Cost** — network: no · writes files: yes · idempotent: no · spends money: no

**Behavior and failure modes**

Priority, severity inclusion, grouping, effort estimates, and URL caps
come from the configured ``tasks_pipeline``. Returns a compact summary
and absolute paths to both backlog files.
