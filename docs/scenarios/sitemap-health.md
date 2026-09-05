# Scenario 30 — Sitemap health: the entries that should not be in it

## The question

> Search Console says our sitemap has errors, but it does not say which URLs. Can you tell me
> what is in there that should not be?

A sitemap is a recommendation, and the only way to make a recommendation worthless is to fill
it with URLs that redirect, 404, carry a `noindex`, or point somewhere other than their own
canonical. Every one of those tells a crawler that the list is not maintained.

## Covers

- **Sitemaps** — Non-Indexable URLs In Sitemap · XML Sitemap With Over 50k URLs · XML Sitemap Over 50mb · URLs In Multiple Sitemaps

## The chain

**1. Expand the sitemap, including any index files.**

```bash
seohead sitemap-crawl --url https://example.com/sitemap.xml
```

The result carries `count` and, per entry, the raw `loc`, a normalized form and `lastmod`. Read
`count` first: a sitemap that declares far fewer URLs than the site has pages is a different
problem from one that declares too many.

**2. Check the two limits that make a sitemap invalid rather than merely large.**

The protocol caps one file at 50,000 URLs and 50 MB uncompressed. Over either, the file is not
"big" — it is invalid, and a search engine may take the first 50,000 entries and discard the
rest, silently, with nothing the site owner can see. `SITEMAP_TOO_MANY_URLS` and
`SITEMAP_TOO_LARGE` fire against the individual child sitemap rather than the index, because
"your sitemap is too big" is not actionable when the index has forty children. The size is
measured after decompression, so gzipping an over-long file does not fix it.

A third one lives here too: `SITEMAP_URL_DUPLICATED` names a URL declared in more than one
sitemap, with both document URLs in the details. It is usually a generator that ran twice, and
it distorts every count taken from the declared set.

**3. Confirm the site actually points at the sitemap you just read.**

```bash
seohead robots-check --url https://example.com
```

The `sitemaps` array is what `robots.txt` declares. A sitemap that is generated but never
declared, or an old path still declared after a migration, both show up as a mismatch here.

**4. Crawl, seeded from the sitemap, so every declared URL is fetched and judged.**

```bash
seohead crawl-site --url https://example.com --sitemap https://example.com/sitemap.xml --out-dir ./run
```

**5. Scan the run.**

```bash
seohead log-scan --run ./run
```

**6. The native chain above stops at judged pages, not at the dedicated finding.**
`SITEMAP_URL_NON_INDEXABLE` reads Screaming Frog's own `Sitemaps: Non-Indexable URLs In
Sitemap` comparison export directly; a native `crawl-site` run supplies no equivalent frame, so
this run skips it by name (`missing export: sitemap_non_indexable`) rather than reporting a
clean sitemap. To get the dedicated finding, run the export-mode route instead, over the same
crawl and the exports Screaming Frog wrote:

```bash
seohead sf run --exports-dir ./exports --out report --tasks
```

Without that export, judge the same four mistakes yourself by joining what you already have:
`sitemap-crawl`'s declared URLs against the per-page `Indexability` and `Indexability Status`
from the `crawl-site` run above. It is a manual join, not the registry check, and it does not
name which sitemap a duplicate came from or assert the protocol limits — see "What it cannot
answer" below.

| What the URL does | Where the fix belongs |
|---|---|
| returns 3xx | the generator: declare the destination |
| returns 4xx or 5xx | the generator, or the page |
| carries `noindex` | one of the two is wrong; decide which |
| canonicalises elsewhere | declare the canonical instead |

**7. Walk one of the redirecting entries to see where it now lands.**

```bash
seohead redirects-check --url https://example.com/old
```

**8. Export the list.**

```bash
seohead report-build --audit ./run/audit.json --format csv --out ./sitemap-health.csv
```

## What comes out

A per-URL list with the reason each entry does not belong, which is what makes it actionable:
"non-indexable URLs in sitemap: 61" is a number, and "these 61 URLs redirect, and here is where
each one goes" is a regeneration rule.

The common cause is worth naming: the generator emits the URL the CMS stores, while the site
serves a normalized form of it. Every entry then redirects, and the sitemap becomes a list of
addresses the site itself has stopped using.

## What it costs

One request for the sitemap plus its children, and one per declared URL during the crawl.
Nothing paid. `sf run --exports-dir` is a local read of exports you already have from Screaming
Frog; it adds no request of its own.

## What it cannot answer

- **`SITEMAP_URL_NON_INDEXABLE` from a native crawl alone.** The check reads Screaming Frog's
  `Sitemaps: Non-Indexable URLs In Sitemap` comparison export; without it, `sf run` skips the
  check by name and a native `crawl-site` run never raises it either. Step 6's manual join
  covers the same four mistakes, but it is not the registry check and does not distinguish them
  from each other in one output.
- **The protocol limits.** Neither the 50,000-URL ceiling nor the 50 MB file-size ceiling is
  asserted, so a sitemap that exceeds either is not reported as invalid here.
- **Which sitemap a URL came from.** URLs appearing in more than one child sitemap are counted,
  not named individually.
- **Whether `lastmod` is true.** The date is read as declared. A generator that stamps today on
  every entry produces a valid sitemap that means nothing, and nothing here detects that.
- **Whether Google fetched it.** Submission, fetch status and the errors Search Console counts
  are on Google's side of the fence.
- **Whether a missing page should be in it.** This chain judges the entries that exist — the
  other direction is [sitemap reconciliation](sitemap-reconciliation.md).
