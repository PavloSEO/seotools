# Scenario 13 — Robots directives: what the site is currently asking search engines to do

## The question

> Somebody added meta robots tags to this site years ago and nobody remembers why. What do
> they say now, and is any of it still what we want?

`robots.txt` controls crawling. Robots *directives* — the `meta name="robots"` element and the
`X-Robots-Tag` response header — control what happens after the page has been fetched. They are
different files, different scopes, and a surprising number of sites have exactly one of them
under version control.

## Covers

- **Directives** — NoImageIndex · Nofollow · None · NoSnippet · NoTranslate · Unavailable_After · Outside <head>

## The chain

**1. Read `robots.txt` first, so you do not confuse the two.**

```bash
seohead robots-check --url https://example.com
```

Nothing this command reports is a directive in the sense below. It is here so the distinction
is made out loud before the audit is read.

**2. Look at one page's directives literally.**

```bash
seohead parse --url https://example.com/page
```

The `robots` and `robots_meta` fields print what the page says. Two forms defeat a naive
substring search and are handled explicitly: `none`, which is shorthand for `noindex, nofollow`
and is expanded into both tokens before any check runs, and the `<user-agent>: <directive>`
prefix an `X-Robots-Tag` header may carry.

**3. Crawl, and let the audit read the header and the element together.**

```bash
seohead crawl-site --url https://example.com --out-dir ./run
```

Each directive becomes its own finding, so the report never says "robots problems":

| Finding | What it actually does |
|---|---|
| `NOSNIPPET` | suppresses the search-result snippet for that page |
| `NOIMAGEINDEX` | keeps images *on that page* out of image search |
| `NOTRANSLATE` | opts out of translation-related Google Search features; see [Google's translated-results guide](https://developers.google.com/search/docs/appearance/translated-results) |
| `NOFOLLOW_PAGE` | page-level nofollow: no link on the page passes signals |
| `UNAVAILABLE_AFTER` | a date after which the page leaves the index by itself |

`none` on a page raises `NOINDEX` and `NOFOLLOW_PAGE` together, because that is what it means.

**4. Read `DIRECTIVES_OUTSIDE_HEAD` as the one that makes every other directive here moot.** A
browser closes `<head>` at the first element that does not belong there, and a meta robots tag
after that point is read from `<body>` instead — it does not apply, and nothing in the source
looks wrong. Step 3's crawl already resolved this from the parse tree; a directive believed to
be suppressing indexing, and silently not doing so, is worth checking before anything else on
this list.

**5. Read `UNAVAILABLE_AFTER` before anything else in the list.**

It is the only directive here that is a timer. Every other one describes a state somebody can
see today; this one describes a page that will remove itself on a date, and it is a warning
rather than a notice for that reason. Check the date is in the future and intended.

**6. Confirm the run, then export.**

```bash
seohead log-scan --run ./run
```

```bash
seohead report-build --audit ./run/audit.json --format md --out ./directives.md
```

## What comes out

An illustrative directive census, in the form `summary.by_check` prints:

```json
"by_check": {
  "NOSNIPPET": 4,
  "NOIMAGEINDEX": 2,
  "NOTRANSLATE": 1,
  "UNAVAILABLE_AFTER": 1
}
```

Each of these is a `notice` except `UNAVAILABLE_AFTER`, and every one of their fix hints starts
with the same word: *confirm*. That is deliberate. A directive is not a defect — it is an
instruction somebody wrote, and the only useful question is whether the person who wrote it is
still around to agree with it.

## What it costs

- One request per crawled page. `robots-check` and `parse` are one request each.
- Nothing paid.
- The whole chain fits inside the crawl you were going to run anyway.

## What it cannot answer

- **Whether the directive is being obeyed.** There is no index data in this loop. `nosnippet`
  on the page and no snippet in the results are two separate facts, and only the first is
  measured here.
- **Whether the directive was intentional.** Every fix hint says "confirm" because the tool
  genuinely cannot tell. A `noimageindex` on a stock-photo landing page is policy; the same tag
  on a product gallery is a bug.
- **A directive added by JavaScript.** The audit reads the served HTML and the response
  headers. See the [rendering scenario](rendering.md).
- **What `robots.txt` blocks.** Crawling and indexing are different questions; step 1 answers
  the first one and nothing else here does.
- **Directives on a page the crawl skipped.** A URL blocked by `robots.txt` was never fetched,
  so its directives are unknown rather than absent.
- **`DIRECTIVES_OUTSIDE_HEAD` on a plain Screaming Frog export.** It needs the parse tree a
  native crawl builds; an export-only run names it skipped rather than reporting it clean.
