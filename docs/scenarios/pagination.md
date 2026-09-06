# Scenario 15 — Pagination: whether page 2 onward is reachable and allowed to exist

## The question

> The category has 40 pages of products. Google has the first one. Where did the other 39 go?

A pagination series is a chain, and a chain has two independent failure modes: a link somewhere
in it does not resolve, or every page past the first is told not to be indexed. Both look
identical from the front page.

## Covers

- **Pagination** — Non-200 Pagination URLs · Unlinked Pagination URLs · Pagination Loop · Non-Indexable · Pagination URL Not In Anchor Tag · Multiple Pagination URLs · Sequence Error

## The chain

**1. Crawl the site, which answers the indexability half immediately.**

```bash
seohead crawl-site --url https://example.com --out-dir ./run
```

`PAGINATION_NONINDEXABLE` covers both published issue names at once, because they are the same
question asked twice: a pagination URL that answers 3xx/4xx/5xx and a pagination URL that
answers 200 with a `noindex` are both pages a crawler will not keep. A series where page 1 is
indexable and pages 2..n are not is the most common shape, and it is usually a plugin default
nobody chose.

**2. Notice which pagination checks the crawl declares it cannot run.**

`run.checks_skipped` will contain all three of these:

```json
[
  {"id": "PAGINATION_LOOP", "reason": "no rel=\"next\" column in Internal:All"},
  {"id": "UNLINKED_PAGINATION_SERIES", "reason": "no rel=\"next\" column in Internal:All"},
  {"id": "PAGINATION_SEQUENCE_ERROR", "reason": "no rel=\"next\" column in Internal:All"}
]
```

The native spider records hyperlinks; it does not extract `rel="next"` / `rel="prev"` into a
column of its own. Rather than reporting a clean series it never examined, all three checks
declare themselves absent by name.

**3. Run the audit over a Screaming Frog export to close that half.**

```bash
seohead sf run --exports-dir ./exports --out report --tasks
```

`PAGINATION_LOOP` walks the `rel="next"` graph and flags a series that cycles instead of
terminating — page 5 pointing back to page 2 is a crawler walking in circles for as long as it
has budget. `UNLINKED_PAGINATION_SERIES` flags a series reachable *only* by following
`rel="next"`, never by an ordinary hyperlink: a discovery path that depends entirely on an
annotation search engines have said they no longer use for indexing.

**4. Export All Inlinks too, because two of these are about the page's own markup.**

Screaming Frog writes one All Inlinks row per link *and* one per `rel="next"`/`rel="prev"`
declaration, typed as such. That export is the only place the complete declaration list
exists — `Internal:All` keeps the first of each and drops the rest — so both of these read it:

`PAGINATION_MULTIPLE` fires when a page declares two *different* `rel="next"` URLs (or two
`rel="prev"` URLs). The same URL declared twice is untidy markup with one successor and is not
reported; two different ones leave the series genuinely ambiguous.

`PAGINATION_URL_NOT_IN_ANCHOR` fires when a declared pagination URL is not also linked from the
same page with an ordinary `<a href>`. Google stopped using these annotations for indexing in
2019, so a declaration with no anchor beside it is a route only some crawlers still take. This
one is provable on a partial crawl, unlike `UNLINKED_PAGINATION_SERIES`: the page was fetched,
so its own links are all in the export.

Without that export both declare themselves absent by name rather than reading clean:

```json
[
  {"id": "PAGINATION_MULTIPLE", "reason": "no all_inlinks export (needed for every rel=\"next\"/rel=\"prev\" declaration and the anchors beside them)"}
]
```

**5. Read the sequence finding as a statement about a run, not about `1..n`.**

`PAGINATION_SEQUENCE_ERROR` walks the same `rel="next"` graph and reads each URL's own page
number, from a `page`/`paged`/`pg` token and nothing else — a bare number in a path is a year
or a product id as often as a page index. It reports a break in a run that increments by one
somewhere and then does not: 1, 2, 3, 7 leaves pages 4 to 6 in nobody's chain.

What it deliberately does not report: a series starting at a number other than one (a crawl of
a subsection looks exactly like that), a series with a stride such as `?page=0,10,20`, and any
series where one URL does not state its number at all. Each of those is left unevaluated rather
than reported against a numbering that would have had to be invented, and when no series in the
crawl can be judged the check says so by name.

**6. Confirm the links themselves resolve.**

```bash
seohead links-check --url https://example.com --internal-only
```

**7. Scan the run, because an unlinked finding is a claim about the whole site.**

```bash
seohead log-scan --run ./run
```

`UNLINKED_PAGINATION_SERIES` is withheld and re-declared as a named skip when the crawl is
partial, for the same reason as `ORPHAN_PAGE` and `UNLINKED_CANONICAL`: the missing hyperlink
may simply be in the part nobody fetched.

## What comes out

```json
{
  "check": "PAGINATION_NONINDEXABLE",
  "severity": "warning",
  "target_url": "https://example.com/catalog/page/2",
  "message": "Pagination page is non-indexable",
  "fix_hint": "Pagination pages should generally remain crawlable and indexable unless a deliberate alternative architecture is in place."
}
```

Read the fix hint as written. "Generally" is doing real work there: a site with a proper
view-all page, or one that loads the rest of the catalogue from a feed, has a defensible reason
for non-indexable pagination. A site with 39 unreachable pages of products does not.

## What it costs

- One crawl for step 1, one destination fetch per internal link for step 6.
- Steps 3, 4, 5 and 7 read files already on disk. Nothing paid.
- On a large catalogue, `links-check` is the expensive step; scope it before running it.

## What it cannot answer

- **The order of a series whose URLs do not number themselves.** `/catalog/2/` states nothing a
  page-number token can be read from, and guessing produces sequence "errors" on ordered series.
- **Whether a stride is intentional.** `?page=0,10,20` is an offset scheme to one site and a
  broken run to another, and nothing here can tell which; it is left unevaluated.
- **Infinite scroll.** A series assembled by JavaScript has no `rel` annotations to read and no
  hyperlinks to follow; the [rendering scenario](rendering.md) comes first.
- **Whether the products on page 2 are indexed.** Reachability is not indexation, and nothing
  here reads the index.
