# How SEOHEAD fits into a technical SEO stack

SEOHEAD Tools is a local Python SEO crawler and scan-analysis toolkit. Its own native engine
collects website evidence; the same toolkit retains, analyzes, reanalyzes, compares, and reports
on scans through a CLI and local stdio MCP server.

## Canonical product description

> SEOHEAD crawls websites, retains scan evidence for offline reanalysis, analyzes native scans
> and Screaming Frog exports, and combines them with explicit live and provider evidence to
> produce traceable SEO findings, prioritized tasks, and reports for specialists and agents.

The development direction is full headless crawling and analysis, including JavaScript-heavy
sites. Complete parity with another crawler, at equivalent speed and scale, requires feature
acceptance tests and reproducible benchmarks. It is not a verified capability claim today.
Resource budgets protect runs and expose incomplete coverage; they do not define the product
as a small-site collector. The supported interfaces remain CLI and local MCP.

The short workflow is: **collect -> analyze -> enrich deliberately -> review -> deliver**.

The built-in 162-check importer targets Screaming Frog CSV/XLSX exports. Another crawler may still
belong in a team's stack, but its exports are not claimed to be a drop-in input for the SF analyzer.

## Where it is strong

### One local interface for an agent

The CLI and MCP server share the same 97 handlers, and five additional MCP tools cover the
Screaming Frog audit workflow. A registration test prevents a command from existing in only one
interface.

### Deep analysis of existing crawl data

Export mode evaluates Screaming Frog CSV/XLSX data against a 162-check registry without crawling
again. It is useful when the crawl was taken by another specialist, came from CI, or must remain
offline. Missing exports become explicit skipped checks rather than silent zeroes.

### Evidence beyond a crawler

Live tools add DNS/RDAP/TLS, cache behavior, technology markers, security headers, mirror
canonicalization, AI crawler access, regional structure, rendering differences, log analysis,
Schema.org graphs, and optional demand/traffic data.

### Structured deliverables

`site-audit` runs a bounded sitemap-based pass: ten site-level tools and three page-level tools,
with 25 selected pages by default. It assembles one document and records individual tool failures;
it is not an exhaustive run of the catalog or a link-graph crawl. `report-build` formats existing
evidence as XLSX, DOCX, CSV, Markdown, or JSON without recalculating findings.

## Where another tool is the right choice

| Need | Use instead or alongside SEOHEAD | Reason |
|---|---|---|
| Crawl a large or JavaScript-heavy site | Evaluate native `crawl-site` against the required discovery, rendering and resource policy; use another collector where a verified gap remains | Full feature and scale parity requires acceptance evidence; no universal size or speed claim is made |
| Discover a domain's full backlink profile | Ahrefs, Majestic, Semrush, GSC, or another index | `backlinks-check` verifies a donor list; it owns no web index |
| Field Core Web Vitals | CrUX, Search Console, or PageSpeed Insights | `render-check` records one lab run and labels it as lab data |
| Search volume, rankings, and SERP history | Wordstat, Arsenkin, DataForSEO, or another provider | These are external datasets, not facts code can derive |
| Machine translation | A reviewed translation model or professional localization workflow | SEOHEAD audits international structure and hreflang; it does not claim a translation engine |
| A hosted multi-user dashboard | A SaaS SEO platform | SEOHEAD is deliberately headless and local |
| Automatic production changes | A reviewed deployment/CMS workflow | SEOHEAD produces evidence and files; it does not deploy fixes |

## Screaming Frog boundary

Export analysis works with files you already have. Live crawl mode launches a separately installed
Screaming Frog CLI and requires an active paid SEO Spider licence. The toolkit does not bundle,
activate, or bypass Screaming Frog. Native crawling works independently of its licence; full
Screaming Frog feature parity is not claimed.

## Interpretation boundary

Heuristics are labelled as heuristics. Lab data is not field data. A missing provider row is not
zero demand, and a failed tool is not a clean result. Final recommendations still require a
specialist to understand business intent, templates, release risk, and the cost of implementation.
