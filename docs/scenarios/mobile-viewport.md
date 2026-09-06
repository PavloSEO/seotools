# Scenario 53 — The mobile viewport: one tag, and what it takes to see it

## The question

> Half the site looks fine on a phone and half of it comes up zoomed out. The developers say
> nothing changed.

Something did: some templates carry a viewport declaration and some do not. It is one meta tag,
it is the cheapest fix in mobile SEO, and it goes missing in exactly the places nobody reviews —
print templates, landing pages built outside the CMS, a checkout that predates the redesign.

## Covers

- **Mobile** — Viewport Not Set · Contains Unsupported Plugins

## The chain

**1. Crawl with this toolkit's own crawler.** That matters here more than anywhere else: the
viewport check needs a viewport column, and a native crawl records one per page.

```bash
seohead crawl-site --url https://example.com --config ./crawl.json --out-dir ./run
```

**2. Scan the run.**

```bash
seohead log-scan --run ./run
```

**3. Read `VIEWPORT_MISSING`, and read the reason on each finding.** One check name covers
three distinct states, and the `details.reason` field says which:

| Reason | The markup |
|---|---|
| `no viewport meta tag` | there is none |
| `initial-scale below 1` | present, but starts the page zoomed out |
| `no width or initial-scale` | present, but declares neither of the two things that matter |

The second and third are the ones that surprise people, because the tag is right there in the
head and looks like a solved problem.

**4. Confirm one page's actual markup.**

```bash
seohead parse --url https://example.com/page
```

**5. Look at the page the way a phone would be given it.**

```bash
seohead render-check --url https://example.com/page --viewport mobile --wait load
```

This is the step that separates a missing declaration from a broken layout. The tag is a
declaration; whether the page then works is something you have to look at.

**6. Check for plugin-dependent content while you're here.** `UNSUPPORTED_PLUGIN` fires when a
page contains an `<object>`, `<embed>` or `<applet>` element that is not a benign image
fallback (an `<object type="image/...">` used for an inline SVG or PDF is excluded — that
renders in every browser, mobile included). No phone runs a plugin, so content behind one of
these tags is not degraded on mobile, it is simply absent.

**7. Group it by template before reporting it.** Findings on 200 URLs are usually four
templates. The deliverable is four edits, not two hundred.

```bash
seohead report-build --audit ./run/audit.json --format md --out ./viewport.md
```

## What comes out

A list of URLs with the reason each one failed, which collapses into a short list of templates.
The fix on each is the same single line in the document head, declaring the device width and an
initial scale of 1.

If the run came from a Screaming Frog export rather than a native crawl, this check does not
report an absence — it reports itself as skipped, with the reason: no viewport column, which
needs a native crawl or a custom extraction configured in Screaming Frog. A skipped check and a
passing check are not the same thing, and `summary.check_coverage` is where the difference is
recorded.

## What it costs

Nothing beyond the crawl. `render-check` costs one browser session per page and is an order of
magnitude more expensive than a fetch, so it is for one page per template, never for the site.

Nothing paid.

## What it cannot answer

- **Whether the page is actually usable on a phone.** Content wider than the screen, text too
  small to read, and tap targets too close together all need a rendered mobile layout measured
  against the device, and none of them is checked here.
- **A viewport set by JavaScript.** A tag injected after load is invisible to a static crawl.
  The render step above is how you find out, one page at a time.
- **Mobile alternate annotations.** A separate mobile URL declared through `rel="alternate"`
  with a media attribute is not read.
- **What the phone actually rendered.** A headless browser at a mobile viewport is an
  approximation of a device, not the device.
- **Whether anyone visits these pages on a phone.** That is analytics, and it decides the
  priority of everything above.
