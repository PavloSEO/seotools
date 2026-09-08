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
order", because the grouping discards it. `heading_outline` beside it does: the same headings as
a sequence, each with its level, its text and the page region it sits in.

**2b. Read the two checks that judge that sequence.** A native crawl stores the outline, so the
registry answers both questions the grouping cannot. `HEADING_BEFORE_H1` fires when a heading of
any level stands before the page's first H1 in DOM order, with how many, which levels, and the
first few texts. `HEADING_IN_PAGE_CHROME` fires when a heading sits in the header, nav, sidebar
or footer — the same regions link positions use — because a menu label is a label on furniture
repeated on every page, and it is what pushes the real H1 down the outline. Both are skipped by
name on a Screaming Frog export, which carries no outline at all.

**2c. Know when the region is not knowable.** A heading is placed by a position rule, or against
the content area when the document names one — a `<main>`, a `[role=main]`, an `<article>`, or a
selector you configured. On a page with none of those, "not chrome" would be true of every
heading and would say nothing, so the region is recorded as unmeasured and the page is named
among the run's skipped checks with that reason rather than reported clean. Order does not need
a region, so `HEADING_BEFORE_H1` still answers for such a page. If a site's menus are styled
`<div>`s the default rules do not recognise, name them in `link_position.rules` and both the
link positions and the heading regions improve together.

**3. Build the rest of the outline's logic.** The `heading-outline` skill in this repository does the part the
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
- **Which region a heading is in on a page with no landmarks.** `HEADING_IN_PAGE_CHROME` reports
  that page as unevaluated rather than clean; the answer is to give the template a `<main>`, or
  to name its blocks in `link_position.rules`.
- **Whether an alt-only H1's image is even the right image.** `H1_ALT_TEXT_ONLY` reports that the
  heading has no visible text, not whether the alt text describes the page accurately.
- **Headings inserted by JavaScript.** A raw fetch of an app shell returns no headings, which
  reads identically to a page with none. Check the [rendering scenario](rendering.md) first when the body
  text is also thin.
- **Whether a heading says anything.** "Read more" and a lone icon are structurally valid
  headings and editorially useless.
- **Anything about pages the crawl did not reach.** Read `run.crawl_partial` first.
