# Tool reference

63 + 5 tools, reachable identically from the CLI and from MCP. One
implementation, two faces: `seohead <command>` in the terminal and
`seo_<command>` on the MCP server (`seohead mcp`). Five more `sf_*` tools cover
the Screaming Frog crawl audit workflow specifically — see that section below
and the generated [CHECKS.md](CHECKS.md) for the 155 checks it runs.

This page is hand-written orientation: what each group is for, and the calling
conventions shared across it. The generated [TOOL_REFERENCE.md](TOOL_REFERENCE.md)
carries the part that must never drift by hand — every argument with its type
and default, and each tool's cost (network/writes/idempotent/spend) and its own
docstring's failure-mode notes — read straight from the MCP tool definitions.

The shared contract: JSON out; when a source is unreachable the tool returns
`{"ok": false, "error": "..."}` instead of raising. An unreachable site is
data, not an accident.

## How to read the tables

- **Network** — whether the tool goes out to the internet.
- **Side effects** — whether it writes to disk or makes requests the site
  does not expect.
- "—" in side effects means "read-only".

---

## Domain and infrastructure recon (`seohead/recon/`)

| Command | What it does | Network |
|---|---|---|
| `domain-profile` | Who owns the domain and where it lives: RDAP/whois, registration and expiry dates, DNS (A/AAAA/MX/NS/TXT), ASN and hoster, country, TLS certificate and its lifetime | yes |
| `cdn-check` | Is there a CDN and **does the cache actually work**: `cache-control`, `age`, `x-cache` headers, 20+ network signatures, a repeat request to confirm the MISS -> HIT transition | yes |
| `tech-detect` | The site's stack from HTML and headers: CMS, framework, server, analytics, chats, payment widgets. An external fingerprint database plugs in via an environment variable | yes |
| `security-check` | Security headers (HSTS, CSP, X-Frame-Options, Referrer-Policy, …) with a grade. With `--probe-paths` it also probes whether `.git/`, `.env` and friends are exposed | yes |
| `backlinks-check` | Verify a list of donor pages: is the link there, its anchor, `rel`, does it pass weight, is the donor indexable. Donors come as a list or a file | yes |
| `ai-bots-check` | Which AI crawlers robots.txt lets in: GPTBot, ClaudeBot, PerplexityBot, Google-Extended and a dozen and a half more, each with its role | yes |
| `mirror-check` | Mirror glue: scheme x www x index files x letter case x trailing slash; step-by-step redirect chains per variant, live 200-duplicates, http downgrades; www resolved via DoH | yes, read-only |
| `regions-check` | Regional structure: subdomains, folders, satellites. See the [regional-audit](../.claude/skills/regional-audit/SKILL.md) skill | yes |

**Recon boundaries.** Geo is resolved to country only — no invented cities.
No RDAP and no system `whois` -> `source: none`, not an empty answer passed
off as fact. Without the `h2` package the HTTP version is not measured and
the report says `http_version_measurable: false`.

---

## Live URL tools (`seohead/tools/`)

### Crawling basics and server responses

| Command | What it does | Side effects |
|---|---|---|
| `parse` | Page breakdown: title, description, headings, canonical, meta robots, OG, counters | — |
| `headers-check` | The full response header set with the SEO-relevant ones decoded | — |
| `asset-weight-check` | Fetches a page's linked CSS/JS and reports render-blocking resources, oversized files, duplicate libraries (by content hash), missing minification, missing `font-display`, legacy polyfilled JS, and missing compression/long-lived caching. Unused-code and cross-page outlier checks need rendering/multiple pages and are reported under `skipped` | — |
| `redirects-check` | The live redirect chain to the final URL: hop count, codes, where the loop is | — |
| `redirects-generate` | Ready redirect rules from URL pairs: Apache (rewrite/redirect), nginx, a custom format | writes to stdout |
| `links-check` | Broken links on a page; `--internal-only` — own links only | — |
| `robots-check` | Parse robots.txt and test specific paths for a specific user-agent | — |
| `sitemap-crawl` | Walk a sitemap (index/urlset, `.txt`, gzip), verify response codes | — |
| `soft404-check` | Soft 404: requests a deliberately non-existent URL; a 200 answer means the page lies to the robot about its status | requests a non-existent URL |
| `hreflang-check` | hreflang annotations: x-default, self-reference, duplicates, malformed language codes | — |

`robots-check` parses a successful robots.txt response. A 404 and every other 4xx response except
429 report no robots.txt and a permissive crawl policy; a 429 or 5xx response returns `ok: false`
because the rules could not be read, so the command never claims crawling is allowed.

### Content and markup

| Command | What it does |
|---|---|
| `schema-check` | Schema.org validation **in two layers**: the vocabulary (1010 types, `domainIncludes`/`rangeIncludes`, inheritance, deprecated terms) and Google rich-result eligibility |
| `schema-build` | Builds a suggested connected `@graph` with `@id`s for the page type; `--type` sets the type manually when the classifier is unsure |
| `social-meta-check` | Open Graph and Twitter Card: what is there, what is missing, what contradicts the content |
| `citability-check` | How quotable a text is for an AI answer: direct answers, facts, structure. Fetching a URL scores the resolved content area's Markdown (nav/footer excluded), not the raw whole-document text |
| `llms-txt-check` | Is there a `/llms.txt`, how useful it is to a model, is the brand mentioned |
| `duplicate-check` | Near-duplicates via simhash + LSH: finds almost-identical texts in a large set without comparing all pairs; exact duplicates (by content hash) are reported separately and excluded from near-duplicate clusters. `--all-pages` also compares non-indexable items (default: indexable only) |
| `markdown-extract` | Renders a page as Markdown in two scopes: `content_markdown` (boilerplate stripped, structure kept — worth diffing, scoring, or feeding to a model) and `full_markdown` (header/footer included, for reading — Markdown has already lost the tag structure `boilerplate-report` hashes, so it is not a valid input there) |
| `boilerplate-report` | Hashes header/nav/footer *markup* per page across a crawled corpus and reports minority template groups (fraction + sample URL), answering whether boilerplate is actually the same everywhere; each page needs the original `html` or a precomputed `hash`, never Markdown |
| `keywords-cluster` | Keyword clustering; the algorithm and parameters come via `--input` |
| `render-check` | Raw HTML vs the rendered DOM + lab metrics. See the [js-render-check](../.claude/skills/js-render-check/SKILL.md) skill |

### Logs and images

| Command | What it does | Side effects |
|---|---|---|
| `log-analyze` | Web server log analysis (Apache Common/Combined, IIS W3C): which bots crawl, where, which codes they get, how much budget is wasted. `--verify-bots` checks authenticity via forward-confirmed reverse DNS | reads a file |
| `log-scan` | Reads a finished run's own artifacts (`audit.json`, `pages.jsonl`, an `images-download` directory) and reports claims that cannot all be true at once: a recorded size that disagrees with the file, a check firing more often than there are pages to fire on, a finding about a URL the run never fetched, a summary that disagrees with its own rows. Not a second audit — only contradictions, each naming both values | reads files |
| `images-download` | Download a list of images into a directory | writes files |
| `images-optimize` | Recompress, resize, or convert raster images and conservatively minify SVG | requires `--output-dir`; source mutation needs explicit `--in-place` and backs up by default |

---

## Bounded site audit and reports (`seohead/audit/`, `seohead/reports/`)

| Command | What it does |
|---|---|
| `site-audit` | Runs a bounded live pass: 10 site-level tools once and 3 page-level tools per selected URL (from the sitemap by default; 25 pages by default). Returns one `seohead.site-audit/1` document. It is not a full crawl or an exhaustive run of the catalog; site-level failures remain in `summary.tools_failed`, while page-level failures remain in that page's issues |
| `report-build` | Document -> file: `xlsx`, `docx`, `csv`, `md`, `json` |
| `scan-reanalyze` | Reparse retained HTML/DOM and run existing checks offline into a new SQLite artifact, preserving source evidence and provenance |
| `facts-export` | Zero-network comparison: reads crawl/site audits you already produced for several domains and returns one `facts.v1` document — measured/absent/partial/unavailable/not_requested facts per site, never a score, rank, or ratio |

```bash
seohead site-audit --url https://example.com --limit 50 --report xlsx --out audit.xlsx
seohead report-build --audit audit.json --format docx --out client.docx
seohead facts-export --input '{"sites": [{"label": "site-a.test", "crawl_audit": {"schema_version": "2.0", "run": {"source": "https://site-a.test/"}, "summary": {"totals": {"urls_crawled": 10}}, "issues": [], "pages": [], "groups": []}}]}'
```

The document contract and skeletons to fill in — [`examples/reports/`](../examples/reports/README.md).

The finding level (`critical`/`warning`/`notice`) is assigned by **aggregator
rules**, not measured by a tool; the document says so itself in
`summary.severity_note`. The rules table is `SEVERITY_RULES` in
`seohead/audit/site.py`.

---

## Crawl your own site (`seohead/crawl/`)

No Screaming Frog licence required: the toolkit drives its own spider and audits the
result through the same check registry as a Screaming Frog export. See
[SETUP.md](SETUP.md#crawling-without-a-screaming-frog-licence) for the module
details (adaptive back-off, which checks come back `skipped` and why) and
[SETUP.md](SETUP.md#comparing-two-crawls) for the four-way diff `compare-crawls` makes.

| Command | What it does | Side effects |
|---|---|---|
| `crawl-site` | Follows links from a start URL on the same host, respects `robots.txt`, and audits the result. Not full Screaming Frog parity — checks needing evidence a native crawl cannot produce (redirect chains, near-duplicates, readability, ...) come back `skipped`, never a false clean | writes `pages.jsonl` and the audit document under `--out-dir` |
| `compare-crawls` | Diffs two audit documents into `entered` / `left` / `appeared` / `disappeared` findings, so a fix is distinguished from a page that simply dropped out of the crawl | — |
| `segment-diff` | Answers "which pages exist in one segment and not in another" from one crawl, using the site's own hreflang declarations as the authority. Mirrored paths are a fallback only where the site's declared pairs prove it mirrors them; a partially crawled target segment yields no absences at all, because a page nobody fetched is not a page that is missing. Reads a native crawl whose config declared `scope.segments`, not an SF export | — |
| `crawl-describe-settings` | Lists every `crawl-site` config setting — dotted path, type, default, description, and whether it is results-affecting — generated from `seohead/crawl/settings.py`. Same source as `crawl-site --config-help`, reachable over MCP for an agent with no filesystem access | — |

```bash
seohead crawl-site --url https://example.com/ --max-urls 200 --out-dir ./report
seohead compare-crawls --before old-audit.json --after new-audit.json
seohead segment-diff --audit ./multilingual/audit.json --source en --target pl
seohead crawl-describe-settings
```

---

## Saved scan history (`seohead/storage/`)

These local operations work only on validated `scan.v1` SQLite artifacts. They
do not create a background catalog, migrate an older schema, or delete a body
without deleting its scan. The exact arguments and defaults are in the generated
[TOOL_REFERENCE.md](TOOL_REFERENCE.md).

| Command | What it does | Side effects |
|---|---|---|
| `scan-list` | Validates and lists metadata for `*.sqlite` files in one existing directory without reading retained body BLOBs. It stops at 10,000 files and 64 MiB of metadata, and reports unreadable candidates under `errors` rather than treating them as scans. | — |
| `scan-inspect` | Reads one allowed table (`pages`, `links`, `forms`, `decisions`, `frontier`, `query_variants`, `context_items`, `responses`, `documents`, `resource_refs`, or `audit`) as a paginated view. At most 1,000 rows and 8 MiB of row payload are returned; `has_more`/`truncated` says when the caller must narrow or continue. | — |
| `scan-snapshot` | Makes a validated, portable single-file SQLite copy. `--out` may name a new file or an existing directory; a directory receives a UTC timestamp, host, and short scan UUID filename. Existing destinations are never overwritten. | writes a new file |
| `scan-pin` | Explicitly pins a scan, or unpins it with `--unpin`, so retention will not select it. | changes scan metadata |
| `scan-prune` | Produces a retention plan by default. Deletion needs `--apply` and the exact reviewed plan. | deletes only with `--apply` |
| `scan-body-diff` | Compares matching retained body hashes from two validated scans; optional text output is bounded and only applies to compatible textual evidence. A changed body is not an SEO score or verdict. | — |

```bash
# metadata-only directory view; no retained body BLOBs are read
seohead scan-list --directory . --limit 100

# inspect a whitelisted table with a smaller total row-payload budget
seohead scan-inspect --input native.sqlite --table documents --limit 100 --max-bytes 1048576

# no-clobber snapshot: either a new filename or an existing directory
seohead scan-snapshot --input native.sqlite --out snapshot.sqlite
seohead scan-snapshot --input native.sqlite --out .

# pin before retaining a comparison baseline; use --unpin to reverse only the pin
seohead scan-pin --input native.sqlite
seohead scan-pin --input native.sqlite --unpin

# inspect the JSON preview, then retain its stdout envelope for explicit review
seohead scan-prune --directory . > plan.json

# hash-first comparison; text materialization is explicit and bounded
seohead scan-body-diff --left before.sqlite --right after.sqlite --url https://example.com/ --text --max-bytes 5242880 --max-lines 10000
```

The default retention plan selects only scans that are finished, unpinned,
complete in both crawl and corpus state, older than 30 days, outside the five
newest scans for the same host and configuration, and free of a live writer
lock. Before any unlink it revalidates every reviewed candidate and recomputes
the current retention rank. `crawl_partial` and `corpus_partial` scans are
always protected by automatic selection. The preview's stdout envelope is an
accepted `--plan` JSON file; a changed directory, identity, metadata, or rank
invalidates it. After reviewing `plan.json`, run `seohead scan-prune --directory .
--plan plan.json --apply` to perform that exact deletion plan. The flat commands
also have the nested `scan list`, `scan inspect`, `scan snapshot`, `scan pin`,
`scan prune`, and `scan body-diff` forms.

Pinning acquires the writer lock and writes the artifact in SQLite DELETE journal
mode. It changes only the `pinned` field, so the SQLite container hash changes,
while the audit, body records, and evidence revision stay intact.

---

## External data sources (`seohead/data_sources/`)

Technical checks describe what a site exposes; **demand** and traffic evidence lives behind
external APIs. This layer keeps one client per provider with common credential, retry, quota,
and spend-journal rules.

| Command | What it does | Money |
|---|---|---|
| `keywords-expand` | Expand a phrase via Yandex Wordstat: refinements (left column) + similar queries (right column) with base frequency | RUB 20/1000 GetTop requests at the first snapshot; quota **100/hour**; verify current tariff |
| `keywords-seasonality` | Demand over months/weeks/days — tell a dead query from a seasonal one | same |
| `keywords-exact` | Exact `!W` frequency via Arsenkin — what the Wordstat API will not give you | Arsenkin account limits |
| `serp-fetch` | Yandex SERP for a query or a batch. Async only | metered; synchronous search is intentionally absent; verify current tariff |
| `spend-report` | What was actually charged: by source, operation and day, from the local journal | free |
| `sources-doctor` | Which sources have their secret and where it lives | free |
| `regions-tree` | The authoritative Yandex region tree via `getRegionsTree` | **free** — the only free Wordstat method |
| `metrika-counters` | Metrika counters visible to the token — this is where `counter_id` comes from | free |
| `metrika-setup` | How a counter is configured: goals, filters, data operations | free |
| `metrika-report` | What visitors actually did: any metrics and dimensions, auto-pagination | free |
| `google-keywords` | Google: search volume for a keyword list, semantic expansion from a seed phrase, keyword difficulty | DataForSEO price list; RUB 0 in the sandbox |
| `google-serp` | Google organic results for a query | same |
| `wayback-history` | Every Internet Archive snapshot of a URL: when it changed, what status it returned, what MIME type it was | free, no key |
| `crtsh-subdomains` | Hosts named in public TLS certificates for a domain — subdomains nothing links to | free, no key |
| `gsc-query` | Search Console: clicks, impressions, position and CTR per query or page, plus Google's own indexing verdict for one URL | free; needs OAuth against a property you own |
| `crux-report` | Core Web Vitals as real Chrome users measured them, at origin or URL level | free; needs a Google Cloud API key |
| `indexnow-submit` | Push changed URLs to Bing, Yandex, Naver and Seznam. **Google has not joined IndexNow** | free; needs a self-generated key hosted on the site |

```bash
seohead sources-doctor                                     # what is ready to run
seohead keywords-expand --phrase "underfloor heating" --limit 100
seohead keywords-exact --keywords "underfloor heating,floor screed" --region 225
seohead serp-fetch --queries "underfloor heating,floor screed" --region 213 --top 10
seohead spend-report --since 2026-08-01
```

The last five answer the question a crawl cannot: what actually happens in search. Two of
them need nothing at all — `wayback-history` and `crtsh-subdomains` read public archives and
public certificate logs. The other three need a credential this repository cannot obtain for
you, and say so rather than guessing: with no key configured they return
`{"ok": false, "error": "..."}` naming what is missing, which since #155 is a non-zero exit.
That is the command working, not failing.

**Layer rules.** Wordstat frequency is **base**, not exact: the API has no
`!`/`+`/`[]` operators and the base runs roughly 9x higher than exact. A
multi-region request **sums** frequency — query regions one at a time. Every
charge is journaled to `~/.config/seohead/spend.jsonl` the moment the task
is created, together with the `task_id`: a paid result can be re-fetched for
free even if parsing crashed. Secrets come only from `~/.config` files and
the environment; a missing-secret message carries the path, never the value.

**Google and the split of territories.** Wordstat and Arsenkin cover Yandex,
i.e. the Runet. Everything outside it — English-language projects, India,
the Gulf — lives in Google, and that is `google-keywords` and `google-serp`
via DataForSEO.

Warning: **DataForSEO does not support locations in Russia or Belarus across its services.**
Hence a geo guard: a request
with `--country` set to an uncovered country **does not go out to the
network** — it returns a refusal naming what to use instead. Without the
guard such a request charges money and returns an empty result — paying for
a zero hurts most.

Warning: **the default environment is `sandbox`**: real response shape, fake
data, nothing charged. Production mode is enabled deliberately:
`DATAFORSEO_ENV=prod`. It never switches automatically on purpose: a pipeline can be validated
against the sandbox, while production remains an explicit decision with financial impact.

**Why Metrika belongs here.** A crawl shows what a site has; Wordstat shows
the demand; Metrika shows what people did on the site. Without the third, a
report rests on guesses: a page can be technically perfect and get zero
visits. Order of work: `metrika-setup` **before** any traffic conclusions —
with no goals configured, "zero conversions" in a report is a consequence
of setup, not a fact about the site; data operations can silently reshape
reports.

Warning: **Logs API exports contain raw `ClientID`** — visitors' personal
data. Never commit them, never show them to a client. The loader returns
text; the caller picks the output path and it must stay under `.gitignore`.

**No project-state accumulation here.** Which keyword was queried and why it survived filtering
belongs to the caller's project dataset, not to a provider transport client.

---

## Screaming Frog crawl audit (`seohead/sf/`)

A subcommand with its own argument parser:

```bash
seohead sf run --exports-dir examples/exports --out report --tasks   # mode B
seohead sf run --crawl https://example.com --out report --tasks         # mode A
seohead sf doctor                                                    # diagnostics
seohead sf tasks --json report/audit.json                            # backlog from a report
```

Note: `sf tasks` takes the audit path via the required `--json` flag, not as
a positional argument (`seohead/sf/cli.py`).

**155 checks**: 12 critical, 73 warnings, 70 notices. Sources: SF exports,
derived metrics, inlink exports, the sitemap module, and heuristics.

**Two modes.** A crawls by itself through the SF CLI (license required). B
works from ready exports and is self-sufficient. Mode B is covered by tests
and must not be broken for the sake of mode A.

No data for a check -> it is marked skipped with a reason, not a silent
zero. This is a principle: "0 problems" and "the check never ran" are
different things.

---

## Calling conventions

**Input.** The primary form is `--input '<json>'`; the object is mapped onto
the handler's arguments. Frequent parameters are duplicated as flags:

```bash
seohead schema-check --url https://example.com/product/nasos
seohead backlinks-check --target example.com --donors-file donors.txt
seohead duplicate-check --input '{"items":[{"id":"a","text":"..."}],"threshold":0.9}'
seohead markdown-extract --url https://example.com/product/nasos
seohead boilerplate-report --input '{"pages":[{"url":"https://example.com/a","html":"..."}]}'
echo '{"url":"https://example.com"}' | seohead parse
```

**Side effects only behind an explicit flag.** `--probe-paths` in
`security-check` and the sitemap live-recheck are off by default: a recon
tool must not knock where it was not asked to.

**MCP.** The same set under the `seo_*` names plus the `sf_*` audit tools
(60 + 5):

```bash
seohead mcp        # stdio
```

## Where to go next
- [TOOL_REFERENCE.md](TOOL_REFERENCE.md) — every tool's arguments, types, defaults, cost, and failure modes, generated from the MCP definitions
- [CHECKS.md](CHECKS.md) — the 155 checks the SF crawl audit runs, generated from the registry
- [ARCHITECTURE.md](ARCHITECTURE.md) — layers, invariants, where new code goes
- [SKILLS.md](SKILLS.md) — which skill drives which tool
- [DECISIONS.md](DECISIONS.md) — why it was decided this way and not another
