# Scenario 19 — Meta descriptions: the cheapest column on the site, and the one nobody owns

## The question

> The agency's report says "meta descriptions need work" on 300 pages. What does that actually
> mean, and how many are there really?

"Needs work" collapses four separate states: absent, shared with other pages, too long to
survive a snippet, and too short to be one. They are written by different people at different
speeds, so counting them together is how a two-day job becomes a two-week one.

## Covers

- **Meta Description** — Missing · Multiple · Duplicate · Over 155 Characters · Below 70 Characters · Over 985 Pixels · Below 400 Pixels · Outside <head>

## The chain

**1. One crawl.**

```bash
seohead crawl-site --url https://example.com --out-dir ./run --max-urls 200
```

`meta_description` is recorded per page beside the title, so all four states fall out of the
same record.

**2. Scan before reading.**

```bash
seohead log-scan --run ./run
```

**3. Separate the states.** `audit.json` reports them as distinct checks with distinct
severities: `DESC_MISSING` (there is nothing to edit), `DESC_MULTIPLE` (there is more than one
`<meta name="description">` element on the page, live and non-empty), `DESC_DUPLICATE` (one
text, several pages, grouped), `DESC_TOO_LONG` and `DESC_TOO_SHORT` (there is something, and it
is the wrong size). `DESC_MULTIPLE` only ever fires from a native crawl's own evidence — an SF
export never carries a count of occurrences, only the one value it kept.

`meta_description` itself is unaffected: it stays the first occurrence, exactly as before
`DESC_MULTIPLE` existed, so every check above still measures the same string it always has.
`DESC_MULTIPLE` is purely additive evidence about *how many* live tags there are, not a change
in *which* one the rest of this chain reads.

The character thresholds are configuration. The published catalogue names 155 characters as the
upper bound; this toolkit's default is **160**, and 70 for the lower bound. If a report is going
to quote a threshold, quote the one that ran.

**4. Read one page whole.**

```bash
seohead parse --url https://example.com/page
```

**5. Add pixel width, if a Screaming Frog export exists.**

```bash
seohead sf run --exports-dir ./exports --out report --tasks
```

`Meta Description 1 Pixel Width` is **read from the export, never computed**. As with titles,
only the upper bound participates in a check: a width over 985 pixels fires `DESC_TOO_LONG`
alongside the character count, while "below 400 pixels" has no threshold behind it — the column
is carried as evidence and left unjudged. A native crawl names that column unmeasured rather
than treating an absent value as zero.

**6. Add `DESC_OUTSIDE_HEAD`, which the crawl in step 1 already resolved.** A browser closes
`<head>` at the first element that does not belong there and reads everything after that point
from `<body>` instead — the description is still on the page, just not where a search engine
looks for it. That is the parser's own answer, from the parse tree the native crawl built;
neither Screaming Frog's own crawl nor an export from it has a notion of this at all.

**7. Ship the backlog.**

```bash
seohead report-build --audit ./run/audit.json --format xlsx --out ./descriptions.xlsx
```

## What comes out

The duplicate group is the row that changes the estimate, because it names the shared text:

```json
{
  "group_id": "GRP-DESC-0002",
  "check": "DESC_DUPLICATE",
  "value": "A sample description over seventy characters that reliably meets the configured audit threshold.",
  "urls": ["https://example.com/", "https://example.com/page-b"],
  "count": 2
}
```

And the shape of the split, once the single "300 pages" number is broken apart:

```
| check         | severity | what a writer does with it            |
| DESC_MISSING  | warning  | writes one from scratch               |
| DESC_DUPLICATE| warning  | writes one per group, not per page    |
| DESC_TOO_LONG | notice   | trims an existing sentence            |
| DESC_TOO_SHORT| notice   | extends an existing sentence          |
```

## What it costs

One request per page. Nothing paid. Pixel width rides along with an export you already made.

## What it cannot answer

- **Whether Google will use the description at all.** It frequently writes its own snippet from
  the page. A perfect description is an input, not an outcome.
- **Whether a duplicate is deliberate.** A template that emits one description across a product
  family is indistinguishable, from the outside, from a decision.
- **Which of several meta descriptions a search engine will actually use.** `DESC_MULTIPLE`
  says how many live tags exist; it does not resolve the ambiguity a second tag creates.
  `DESC_OUTSIDE_HEAD` still reads the position of whichever one parsing kept — the first.
- **A second description on an SF-export-only run.** `DESC_MULTIPLE` needs a native crawl's own
  per-tag count; an export never carries it, so the check skips by name there instead of reading
  a page with two tags as clean.
- **Pixel width on a native crawl.** It does not exist there, and "below 400 pixels" is not
  evaluated even where the column does.
- **Whether the sentence is any good.** Structural only, like everything else here.
