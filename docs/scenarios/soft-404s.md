# Scenario 8 — Soft 404s: pages that say "not found" with a 200

## The question

> Search Console keeps reporting soft 404s and I cannot reproduce any of them. The URLs load
> fine when I click them.

That is the symptom, not a contradiction. A soft 404 is a URL that *loads fine* — it returns 200,
renders a template, and contains nothing. The browser cannot tell you; the status code is the
thing that is wrong.

## Covers

- **Content** — Soft 404 Pages

## The chain

**1. Ask the server about a URL that cannot exist.**

```bash
seohead soft404-check --url https://example.com/this-page-cannot-exist
```

The probe is the whole method: two deterministic root paths that are requested and observed. They
avoid special prefixes such as `/.well-known/`, because static middleware can own those prefixes
before an application's fallback route sees them. A 200 is evidence that the host served one of
these unpublished paths as content; the printed URLs make that evidence repeatable.

**2. Read the verdict and the evidence together.**

```
{
  "verdict": "warning",
  "probes": [
    {"url": "https://example.com/seo-audit-not-found-9afcba6dff63-1",
     "status": 200, "redirected": false}
  ],
  "findings": [
    "soft 404: nonexistent URLs return 200/3xx responses and may create indexable low-value pages"
  ]
}
```

A 3xx counts too: a catch-all redirect to the home page is the same defect wearing a different
status. What is being asserted is narrow and checkable — "this host answered 200 for a path that
does not exist" — and the probe URLs are printed so anybody can repeat it in a browser.

**3. Find how far it spreads.** A soft-404 handler manufactures pages, and manufactured pages
are near-identical to each other. Crawl and look at the thin and duplicate populations together:

```bash
seohead crawl-site --url https://example.com --out-dir ./run --max-urls 200
```

```bash
seohead log-scan --run ./run
```

`THIN_CONTENT` clustering on paths that share a prefix, plus `LOW_TEXT_RATIO` on the same URLs,
is what a catch-all template looks like from the outside. The [duplicate content scenario](duplicate-content.md)
groups them properly.

**4. Check whether the sitemap is publishing them.**

```bash
seohead sitemap-crawl --url https://example.com/sitemap.xml
```

A soft-404 route plus a CMS that lists every route is how a site ends up declaring thousands of
empty URLs as canonical content.

**5. Hand it over.**

```bash
seohead report-build --audit ./run/audit.json --format docx --out ./soft404.docx
```

## What comes out

One verdict per host, with the probe URLs, plus the population it created:

```
verdict: warning
probes:  2 nonexistent URLs, both 200
spread:  the thin/near-duplicate pages sharing the handler's template
```

The fix is a server change — return 404 or 410 for unrouted paths — so the deliverable is a
developer task, not a content task.

## What it costs

Two requests for the probe. The spread analysis reuses a crawl you were already running. Nothing
paid.

## What it cannot answer

- **Which specific URLs Google considers soft 404s.** That list lives in Search Console and
  nothing here reads it. This chain explains *why* the list exists.
- **Whether a 200-with-no-content page is intentional.** A search-results page or an empty
  category legitimately returns 200 and looks identical to a soft 404 from the outside.
- **Soft 404s produced only after rendering.** A shell that returns 200 and writes "not found"
  with JavaScript passes this probe. See the [rendering scenario](rendering.md).
- **How many exist.** The probe answers "does this host do it", not "on how many URLs", because
  the set of nonexistent URLs is infinite by definition.
- **Whether any of them are indexed.** Reachability is not indexation.
