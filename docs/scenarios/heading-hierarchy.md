# Scenario 20 — Heading hierarchy: the outline nobody can see from a crawl column

## The question

> Are the headings on these pages in a sensible order, or is the theme picking heading levels
> for their font size?

A crawl column can tell you a page has one H1 and three H2s. It cannot tell you that the H2s
come *before* the H1, or that the page jumps H2 to H4 because H3 looked too big. Order is a
property of the document, and only a document-order parse recovers it.

## Covers

- **H1** — Non-sequential · Alt Text in h1
- **H2** — Missing · Multiple · Non-sequential · Duplicate · Over 70 Characters

## The chain

**1. Get the URL list from a crawl you already have.**

```bash
seohead crawl-site --url https://example.com --out-dir ./run --max-urls 200
```

Do not crawl again for headings. The point of `pages.jsonl` is that the population is already
decided: fetched, 2xx, HTML.

**2. Read the levels present on a page.**

```bash
seohead parse --url https://example.com/page
```

`headings` comes back grouped by level — every H1, every H2, down to H6, with their text. That
answers "is there an H2 at all" and "how many are there", and it does **not** answer "in what
order", because the grouping discards it.

**3. Build the real outline.** The `heading-outline` skill in this repository does the part the
grouping cannot: it fetches the page and walks `//h1|//h2|//h3|//h4|//h5|//h6` in DOM order, then
checks that the level never increases by more than one step. H4 after H2 is an error; H2 after
H4 is a legitimate return to a higher level. That skill runs `curl` plus a local `lxml` parse —
no `seohead` command, one request per URL, nothing paid.

**4. Decide whether "H2 missing" is a finding on this site.** `H2_MISSING` is **off by default**.
It only fires when a config sets `requirements.require_h2` to true, because a short page with a
single H1 and no subheadings is normal, and a check that fires on every landing page is noise.
Turn it on for a site whose content type genuinely needs sections; leave it off for a brochure.

**5. Know what is judged and what is deliberately not.** `H2_DUPLICATE` and `H2_TOO_LONG` (the
same length threshold as H1, 70 characters by default — see `thresholds.h2_max_chars`) now run
against `H2-1`, the same column an SF export or a native crawl already carries. Multiple H2s on
one page are still recorded and **not** counted against it — several H2s are what a sectioned
document looks like, and the issue that asked for this row supplied no defensible count past
which that stops being true.

**5b. Know when an H1 is not really missing text — it is only missing *visible* text.**
`H1_ALT_TEXT_ONLY` fires when an H1 has no text of its own and its only content is an image's
`alt` attribute — `<h1><img alt="Acme Pumps"></h1>`. `H1_MISSING` still fires alongside it,
because the heading genuinely has no text a search engine reads, matching how Screaming Frog's
own H1-1 column also reads it as empty; the alt-only finding is the more specific fact about
*why*. A logo sitting beside real heading text — `<h1><img alt="Logo"> Foundation Repair
Guide</h1>` — is normal and triggers neither.

**6. Put the outline in the deliverable.**

```bash
seohead report-build --audit ./run/audit.json --format docx --out ./headings.docx
```

## What comes out

From `parse`, the levels a page uses:

```json
{
  "headings": {
    "h1": ["Fixture Widget: what it is and why it exists"],
    "h2": ["Specifications", "Related pages"],
    "h3": ["Frequently confirmed facts"]
  }
}
```

From the outline pass, the thing a developer can act on — an indented tree plus the break:

```
H1: Foundation repair
  H2: What it costs
      H4: Per metre           <- HEADING_SKIP (H2 to H4)
  H2: Book a survey
```

## What it costs

One request per page for the crawl, one more per page for the outline pass. Local parsing,
no paid API. Restrict the outline pass to one page per template first — a theme repeats its
mistake, and paying for four hundred pages to learn what one page would have said is waste.

## What it cannot answer

- **Whether the order is wrong on purpose.** A designer's section break and a mistagged widget
  produce the same jump.
- **Whether an alt-only H1's image is even the right image.** `H1_ALT_TEXT_ONLY` reports that the
  heading has no visible text, not whether the alt text describes the page accurately.
- **Headings inserted by JavaScript.** A raw fetch of an app shell returns no headings, which
  reads identically to a page with none. Check the [rendering scenario](rendering.md) first when the body
  text is also thin.
- **Whether a heading says anything.** "Read more" and a lone icon are structurally valid
  headings and editorially useless.
- **Anything about pages the crawl did not reach.** Read `run.crawl_partial` first.
