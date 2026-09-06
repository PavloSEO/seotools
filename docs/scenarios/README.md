# Usage scenarios

The rest of the documentation lists what this toolkit *has*: 56 commands, 60 MCP tools, 121
checks, 22 skills, each described on its own. This directory describes what it **does** — the
chains that run several of them in order and end in something a person can act on.

The distinction matters. One command is a measurement. A chain is a deliverable:

> You will save 61% of your image weight. Here are the 82 files, already re-encoded, in an
> archive. Here is the task, with the numbers in it.

Nobody can assemble that from a list of tool names. Every scenario below is written so that an
agent — or a person — can run it start to finish without inventing the sequence.

## How to read one

Each scenario has the same five parts:

| Part | What it answers |
|---|---|
| **The question** | what somebody actually asked, in their words |
| **The chain** | every command in order, with real flags, and what each one adds |
| **What comes out** | the artifact, with a real excerpt |
| **What it costs** | requests, wall time, whether anything is paid |
| **What it cannot answer** | the limits of this chain, named |

That last part is not modesty. A scenario that does not say what it cannot answer is
marketing, and an agent that trusts it will report a confident wrong answer.

Every command shown in these files is executed against a fixture site by
`tests/test_docs_commands_execute.py` on every CI run. A scenario that stops working fails the
build rather than sitting here misleading its next reader.

## The scenarios

Fifty-six chains, grouped by the question you arrived with. Every issue this toolkit can find
appears in at least one of them — `tests/test_scenario_coverage.py` asserts that against
[COVERAGE_SF_ISSUES.md](../COVERAGE_SF_ISSUES.md), so the catalogue is decided by what the code
does rather than by what somebody thought of.

### Getting a picture of the site

| # | Scenario | Start here when |
|---:|---|---|
| 1 | [Structure](structure.md) | what is unreachable, buried, or missing from the sitemap |
| 2 | [Metadata and thin pages](metadata.md) | which pages actually need writing |
| 3 | [Content extraction](content.md) | how much of this page is actually the page |


### Response codes and redirects

| # | Scenario | Start here when |
|---:|---|---|
| 4 | [Broken pages](broken-pages.md) | what 404s, what 500s, and what never answered at all |
| 5 | [Redirects](redirects.md) | the loop, the chain, and the hop nobody needed |
| 6 | [Blocked by robots.txt](robots-blocked.md) | the pages, and the resources, a crawler never gets |
| 7 | [External links](external-links.md) | where we point, and what answers |
| 8 | [Soft 404s](soft-404s.md) | pages that say "not found" with a 200 |


### Indexability: canonicals, directives, pagination

| # | Scenario | Start here when |
|---:|---|---|
| 9 | [Canonical basics](canonical-basics.md) | the defects that are a typo, not a strategy |
| 10 | [Conflicting canonicals](canonical-conflicts.md) | two answers to a question that takes one |
| 11 | [Canonicalised pages](canonicalised-pages.md) | how much of the site is deliberately not itself |
| 12 | [The canonical nobody links to](unlinked-canonical.md) | a preferred URL with no way in |
| 13 | [Robots directives](robots-directives.md) | what the site is currently asking search engines to do |
| 14 | [The noindex audit](noindex-audit.md) | proving a migration did not take the site offline |
| 15 | [Pagination](pagination.md) | whether page 2 onward is reachable and allowed to exist |


### Titles, descriptions and headings

| # | Scenario | Start here when |
|---:|---|---|
| 16 | [Titles that are missing, doubled or shared](titles-missing.md) | one list, ordered by blast radius |
| 17 | [Title length](title-length.md) | characters we count, pixels we only carry |
| 18 | [Title and H1](title-and-h1.md) | two fields, one CMS field behind them |
| 19 | [Meta descriptions](meta-descriptions.md) | the cheapest column on the site, and the one nobody owns |
| 20 | [Heading hierarchy](heading-hierarchy.md) | the outline nobody can see from a crawl column |


### Content

| # | Scenario | Start here when |
|---:|---|---|
| 21 | [Thin content](thin-content.md) | what "thin" means once the template stops counting |
| 22 | [Duplicate and near-duplicate content](duplicate-content.md) | which pages are the same page |
| 23 | [Readability, spelling and grammar](readability.md) | four columns we read, none we compute |


### Images

| # | Scenario | Start here when |
|---:|---|---|
| 24 | [Images](images.md) | from "the site feels slow" to an archive a developer can deploy |
| 25 | [Image alt text](image-alt-text.md) | missing, empty, and the images no crawler sees |
| 26 | [Image geometry and weight](image-dimensions.md) | what the browser has to guess |


### Links

| # | Scenario | Start here when |
|---:|---|---|
| 27 | [Crawl depth and dead ends](crawl-depth.md) | how far in the site buries its own pages |
| 28 | [Anchor text and inlink composition](anchor-text.md) | who links here, and what they call it |


### Sitemaps

| # | Scenario | Start here when |
|---:|---|---|
| 29 | [Sitemap reconciliation](sitemap-reconciliation.md) | what the sitemap forgot, and what nothing links to |
| 30 | [Sitemap health](sitemap-health.md) | the entries that should not be in it |


### Hreflang

| # | Scenario | Start here when |
|---:|---|---|
| 31 | [Hreflang return links](hreflang-return-links.md) | whether the other side of the pair agrees |
| 32 | [Hreflang codes and x-default](hreflang-codes.md) | the annotation that is only nearly right |


### Structured data

| # | Scenario | Start here when |
|---:|---|---|
| 33 | [Structured data](structured-data.md) | from "we have markup" to a rich result |
| 34 | [Structured data at site scale](schema-validation.md) | valid, eligible, and neither |


### JavaScript and rendering

| # | Scenario | Start here when |
|---:|---|---|
| 35 | [Rendering](rendering.md) | does a crawler see what a visitor sees |
| 36 | [Content only JavaScript produces](js-content.md) | what a non-rendering crawler receives |
| 37 | [Directives under rendering](js-directives.md) | a noindex only one copy of the page carries |
| 38 | [Title, description and H1 that JavaScript writes](js-metadata.md) | two versions of one page |
| 39 | [The canonical only rendering reveals](js-canonical.md) | an indexing signal a script decides |
| 40 | [Blocked resources](js-blocked-resources.md) | the stylesheet and the bundle the crawler was refused |
| 41 | [The old AJAX crawling scheme](js-legacy-ajax.md) | a site still answering a retired contract |
| 42 | [JavaScript errors](js-errors.md) | the exception that stops a page halfway through building |


### Speed and delivery

| # | Scenario | Start here when |
|---:|---|---|
| 43 | [Server response time](speed-server-response.md) | the wait before anything can start |
| 44 | [The blocking head](speed-render-blocking.md) | stylesheets, scripts and fonts that hold up first paint |
| 45 | [The bundle audit](speed-javascript-bundles.md) | legacy transpilation, duplicates and unminified files |
| 46 | [Delivery and weight](speed-delivery-and-weight.md) | caching, images, document size and DOM complexity |


### URLs, security and infrastructure

| # | Scenario | Start here when |
|---:|---|---|
| 47 | [URL hygiene at scale](url-hygiene.md) | the shapes that quietly split a site |
| 48 | [Parameters, facets and the crawl budget they eat](url-parameters.md) |  |
| 49 | [HTTPS](https.md) | the leftover HTTP URLs and the resources that undo the padlock |
| 50 | [Security headers, read as a crawling problem](security-headers.md) |  |
| 51 | [Infrastructure](infrastructure.md) | what this site runs on, and whether it is safe |
| 52 | [Oversized documents, and the responses the crawler had to truncate](oversized-documents.md) |  |
| 53 | [The mobile viewport](mobile-viewport.md) | one tag, and what it takes to see it |


### AI visibility, operating, delivering

| # | Scenario | Start here when |
|---:|---|---|
| 54 | [AI visibility](ai-visibility.md) | will an assistant cite this site |
| 55 | [Comparing two crawls](comparison.md) | what changed since the release |
| 56 | [From audit to deliverable](deliverable.md) | the last mile |


## The rule underneath all of them

Run the crawl once; run everything else against what it collected. Every scenario here starts
from one `crawl-site` run and reuses its `audit.json` and `pages.jsonl`, because a second crawl
of the same site to answer a second question is a second load on somebody's server for an
answer that is already on disk.

After any chain, `seohead log-scan --run <dir>` reports whether the run contradicts itself
before you act on its numbers. Every defect this toolkit has had on live sites was an impossible
number that nobody checked, so checking is one command.
