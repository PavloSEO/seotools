# Scenario 31 — Hreflang return links: whether the other side of the pair agrees

## The question

> We launched three language versions and the wrong one keeps showing up in each country. The
> tags are on every page. What else is there to check?

An hreflang annotation is not a property of a page. It is an edge between two pages, and an
edge only counts when both ends declare it. Half the annotations on a multilingual site are
correct in isolation and useless as a pair.

## Covers

- **Hreflang** — Missing Return Links · Inconsistent Language & Region Confirmation Links · Non-Canonical Return Links · Non-200 Hreflang URLs

## The chain

**1. Read one page's set, to see the shape before you judge the graph.**

```bash
seohead hreflang-check --url https://example.com/page
```

One request, one page. It prints every `rel="alternate"` annotation with its resolved absolute
`href`, plus the problems visible from that page alone. It says nothing about whether the
targets answer, or whether they point back — those are questions about the other end.

**2. Run the audit over a crawl export that carries the annotation graph.**

```bash
seohead sf run --exports-dir ./exports --out report --tasks
```

The graph checks read Screaming Frog's `Bulk Export -> Links -> All Hreflang` report: one row
per source page, destination and language. Without it, `run.checks_skipped` names each of them
with the export they need, rather than reporting a clean multilingual site nobody examined.

**3. Read the four findings as four different jobs.**

| Finding | What broke |
|---|---|
| `HREFLANG_MISSING_RETURN_LINK` | A names B; B never names A |
| `HREFLANG_INCONSISTENT_CONFIRMATION` | A calls B "fr"; B calls itself "de" |
| `HREFLANG_NOT_CANONICAL` | the annotation points at a duplicate that canonicalises elsewhere |
| `HREFLANG_BROKEN_TARGET` | the target answers 3xx, 4xx or 5xx |

The first is a content-management problem, usually one language being published on a different
schedule from the others. The second is the same pair being reciprocal and still discarded,
because Google reads a pair as valid only when both sides name the same code — a counterpart's
own self-referencing annotation is what it calls itself, and a counterpart that declares nothing
about itself is passed over rather than guessed at. The third is an architecture problem. The
fourth is a link that has simply rotted, and it is the cheapest of the four to fix.

**4. Confirm the targets are really what the audit thinks they are.**

```bash
seohead crawl-site --url https://example.com --out-dir ./run
```

```bash
seohead log-scan --run ./run
```

A redirecting hreflang target is a common false alarm on a site that serves two slash forms of
every URL: the annotation names one, the crawl fetched the other. `log-scan` is the mechanical
version of that doubt, and the check family it protects has been wrong this exact way before.

**5. Hand over the pairs, not the pages.**

```bash
seohead report-build --audit ./run/audit.json --format xlsx --out ./hreflang.xlsx
```

## What comes out

```json
{
  "check": "HREFLANG_MISSING_RETURN_LINK",
  "severity": "warning",
  "target_url": "https://example.com/de/produkte",
  "message": "Another page's hreflang points here, but this page does not point back",
  "fix_hint": "Add a reciprocal hreflang annotation back to every page that names this one."
}
```

## What it costs

- One request for step 1, one crawl for step 4, local reads for the rest.
- Nothing paid.
- The All Hreflang export is produced by Screaming Frog, so the cost of this chain is mostly
  the cost of having crawled once with the right export enabled.

## What it cannot answer

- **Whether a counterpart that never self-references is in the right locale.**
  `HREFLANG_INCONSISTENT_CONFIRMATION` measures an incoming claim against what the counterpart
  declares about itself, so a counterpart carrying no self-referencing annotation has nothing to
  disagree with and is passed over here. `HREFLANG_MISSING_SELF_REFERENCE` names that page
  instead — see the [hreflang codes scenario](hreflang-codes.md).
- **Whether an hreflang target is indexable.** The target's status is checked; its `noindex` is
  not cross-referenced. A reciprocal, canonical, 200-answering annotation pointing at a page
  nobody may index still reads clean here.
- **Whether the annotations are in `<head>`, from the graph checks in this chain.** They read the
  export's annotation list, not the parse tree; `HREFLANG_OUTSIDE_HEAD` answers this instead, from
  a native crawl — see the [hreflang codes scenario](hreflang-codes.md).
- **Whether the right language is being served.** Nothing here reads a search result. The chain
  proves the annotations are structurally sound; which page a search engine then shows in
  Austria is outside it.
- **Codes and self-references.** Those are the [hreflang codes scenario](hreflang-codes.md).
