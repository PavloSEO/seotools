# How SEOHEAD fits into a technical SEO stack

SEOHEAD Tools is the local evidence-processing and audit-automation layer between data collection
and specialist judgement. It complements crawlers and commercial data providers; it does not
pretend to replace infrastructure that requires a web-scale index, field telemetry, or a hosted
product.

## Canonical product description

> Screaming Frog collects the crawl for web-scale sites; SEOHEAD's own bounded `crawl-site`
> collects it when no SF licence is installed. SEOHEAD analyzes that evidence (SF exports or its
> own crawl), adds the bounded live, infrastructure, Schema.org, log, and optional provider
> evidence the audit needs, and gives a specialist or tool-calling agent one tested CLI/MCP
> surface for producing traceable findings, prioritized tasks, and reports.

This is a division of labour, not a feature-by-feature contest with Screaming Frog:

| Stage | Tool or owner | Result |
|---|---|---|
| Collect | Screaming Frog exports, SEOHEAD's own bounded `crawl-site`, logs, supplied files, or explicit providers | Raw evidence |
| Analyze and organize | SEOHEAD core through CLI or local MCP | Structured results, explicit gaps, audit documents, and task artifacts |
| Interpret and approve | SEO specialist, optionally supported by an agent | Business-aware priorities and a reviewed deliverable |

The short workflow is: **collect -> analyze -> enrich deliberately -> review -> deliver**.

The built-in 139-check importer targets Screaming Frog CSV/XLSX exports. Another crawler may still
belong in a team's stack, but its exports are not claimed to be a drop-in input for the SF analyzer.

## Where it is strong

### One local interface for an agent

The CLI and MCP server share the same 54 handlers, and five additional MCP tools cover the
Screaming Frog audit workflow. A registration test prevents a command from existing in only one
interface.

### Deep analysis of existing crawl data

Export mode evaluates Screaming Frog CSV/XLSX data against a 139-check registry without crawling
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
| Crawl a large site from scratch | Screaming Frog, Sitebulb, or another production crawler | SEOHEAD intentionally has no general-purpose crawler |
| Discover a domain's full backlink profile | Ahrefs, Majestic, Semrush, GSC, or another index | `backlinks-check` verifies a donor list; it owns no web index |
| Field Core Web Vitals | CrUX, Search Console, or PageSpeed Insights | `render-check` records one lab run and labels it as lab data |
| Search volume, rankings, and SERP history | Wordstat, Arsenkin, DataForSEO, or another provider | These are external datasets, not facts code can derive |
| Machine translation | A reviewed translation model or professional localization workflow | SEOHEAD audits international structure and hreflang; it does not claim a translation engine |
| A hosted multi-user dashboard | A SaaS SEO platform | SEOHEAD is deliberately headless and local |
| Automatic production changes | A reviewed deployment/CMS workflow | SEOHEAD produces evidence and files; it does not deploy fixes |

## Screaming Frog boundary

Export analysis works with files you already have. Live crawl mode launches a separately installed
Screaming Frog CLI and requires an active paid SEO Spider licence. The toolkit does not bundle,
activate, bypass, or replace Screaming Frog.

## Interpretation boundary

Heuristics are labelled as heuristics. Lab data is not field data. A missing provider row is not
zero demand, and a failed tool is not a clean result. Final recommendations still require a
specialist to understand business intent, templates, release risk, and the cost of implementation.
