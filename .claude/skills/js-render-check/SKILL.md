---
name: js-render-check
description: >-
  Compares raw HTML (what a bot receives without rendering) with the rendered DOM to show
  what appears on the page ONLY after JavaScript: text, internal links, title/canonical,
  h1, and Schema.org. This is critical when content, internal linking, or directives arrive
  only during rendering: Google renders with a delay, Yandex handles it less reliably, and
  AI crawlers almost never render. It also captures lab metrics (TTFB, FCP, LCP, CLS).
  Triggers: JS rendering, view-source vs rendered, content only after JS, client-side
  rendering, CSR, SPA SEO, Next.js rendering, check rendering, raw vs rendered, hydration,
  empty HTML, Core Web Vitals, LCP, CLS.
---

# JS Render Check — What Is Visible on the Page Only After JavaScript

Screaming Frog measures **one** snapshot—raw or rendered, depending on whether JS
Rendering is enabled—but does not compare "before JS" with "after JS." This skill
performs exactly that diff.

Google can render JavaScript and parse crawlable links from the rendered HTML, but
rendering may be delayed and other crawlers may not render. Read the result as one
diagnostic snapshot, then verify material cases with [Google's JavaScript SEO
basics](https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics)
or URL Inspection rather than treating JavaScript dependence alone as proof that
Google cannot crawl or index a page.

## Trigger
- "Do content / links / metadata appear only after JS?"
- SPA / Next.js / Nuxt / CSR: "Will it be indexed?" or "Is SSR required?"
- An SF crawl differs with JS Rendering enabled versus disabled, and you need to determine
  which count is correct.
- A regional or silo audit found zero links where links are known to exist.
- Triggers from the frontmatter: JS rendering, view-source vs rendered,
  content only after JS, client-side rendering, CSR, SPA SEO, Next.js
  rendering, check rendering, raw vs rendered, hydration, empty HTML, Core
  Web Vitals, LCP, CLS.

## Anti-trigger
- Auditing rendering behavior across an entire site, not one page — that is
  an SF crawl with JS Rendering enabled (see "Boundaries" below), not a
  per-URL loop of this skill.
- Playwright is not installed and there is time pressure — do not
  approximate the answer from raw HTML alone. The tool intentionally returns
  `ok: false` with an install command rather than guessing; install it first
  or report the check as blocked.
- The real question is field Core Web Vitals (what real users experience),
  not a raw-vs-rendered diff — `metrics_lab` is a single local Chromium run
  and cannot stand in for CrUX/Search Console field data; use the
  `pagespeed-insights` skill for that instead.
- The page is already known to be plain server-rendered HTML (no framework,
  no CSR) — there is nothing to diff; go straight to the relevant on-page
  skill (`heading-outline`, `schema-graph`, …) instead of running a
  rendering diff that will trivially show no difference.

## Preconditions
- [ ] A specific URL to check (this is a one-page tool, not a site crawl).
- [ ] `pip install 'seohead[render]'` and `python -m playwright install
  chromium` completed, or acceptance that the command will fail fast with an
  install prompt if not.
- [ ] A concrete reason to suspect JS-dependent content — an SPA/CSR
  framework, an SF count that differs with/without JS Rendering, or missing
  links found during another audit — otherwise this is a speculative check
  with no expected finding.

## Workflow

**One command instead of manually using curl and headless Chrome:**
```bash
seohead render-check --url https://example.com
```
Use a mobile viewport and device emulation where the layout differs:
```bash
seohead render-check --url https://example.com --viewport mobile
```

Mobile mode uses a stable smartphone diagnostic identity for both the raw and
browser requests, alongside its 390×844 viewport and touch/mobile flags. It is
not Googlebot or a crawler-impersonation profile. For a deliberately chosen
dynamic-serving representation, pass one single-line identity to both sides:
```bash
seohead render-check --url https://example.com --viewport mobile --user-agent "ExampleMobileAudit/1.0"
```

Returned fields:

| Field | Meaning |
|---|---|
| `raw` / `rendered` | identically calculated snapshots of both documents: `words`, `links` (internal link count), `title`, `h1`, `canonical`, `jsonld_types`, and `html_bytes` |
| `empty_shell` | ID of an empty single-page application container, if present |
| `js_dependent` | whether the page depends on scripts in any way |
| `metrics_lab` | TTFB, FCP, **LCP**, **CLS**, load, and weight—**lab data**, not field data |
| `wait` / `wait_reached` | the load milestone requested, and the one the snapshot actually came from — they differ when a requested `networkidle` timed out and the DOM was read at `domcontentloaded` instead |
| `findings` | ready-to-use written conclusions |
| `dual_crawl` | schema `dualcrawl.v1` — per-URL image/link evidence found by only the raw pass or only the rendered pass, so "this page changed" (the fields above) stays distinct from "raw and rendered disagree about what this page contains" |

## How to Interpret Findings

The severity labels and percentage thresholds below are specialist triage heuristics, not Google
indexing requirements. Verify a material defect in the intended crawler and representation before
reporting it as an implementation problem.

| Finding | Severity | Action |
|---|---|---|
| "empty `<div id="root">` container" | critical | use SSR or prerendering: without rendering, the bot sees an empty page |
| "N% of text appears only after JS" | critical when >50% | move the primary content into the server response |
| "links appear only after JS" | triage | confirm the rendered links are crawlable `<a href>` elements and whether static navigation or a sitemap already covers their targets; JS insertion alone does not prove Google cannot discover them |
| "title is changed by a script" | critical | an unpredictable title may appear in search results |
| "canonical is injected by a script" | conditional | critical when it rewrites a source-HTML canonical to a conflicting target; when source HTML has no canonical and JavaScript adds one, validate the rendered target and crawlability instead of treating injection alone as critical |
| "Schema.org appears only after JS" | warning | rich results are uncertain |
| "rendering changes nothing" | okay for this URL and snapshot | no material raw/rendered difference was observed; this does not prove SSR or whole-site health |
| `ok: false` with `reason: "incomplete_render"` | not a finding | the render did not capture the page: report the check as blocked and re-run it, do not read the snapshots as a diff |

## Alert Threshold
The tool raises an alert at **30% and above**; a 5% increase is a widget, not a problem.
Zero words in raw HTML versus hundreds after rendering produces a separate, stronger finding.

## Metrics: Lab Data ≠ Field Data
`metrics_lab` is one run by one Chromium instance from this machine over this connection.
Field Core Web Vitals come from the Chrome UX Report and Search Console; one cannot substitute
for the other. A lab LCP on a fast Mac says nothing about a user's LCP over a mobile connection.
Lab numbers are useful **comparatively**: before versus after a fix, and page versus page.

## Why Wait for `load` Instead of `networkidle`
`networkidle` waits for 500 ms of network silence. A live commercial site never becomes
silent: analytics, chat widgets, and advertising keep connections open. In a measurement
on a production retail site, `load` returned a result in 6.4 seconds, while `networkidle`
timed out after 26 seconds. Google does not wait for network silence while rendering either.
Change this only for a specific case:
```bash
seohead render-check --url https://example.com --wait networkidle
```
If the requested milestone times out anyway, the check no longer dies with it: the DOM
is read at `domcontentloaded` after a short settle, and `wait_reached` says so — read that
field before comparing two runs, because they were captured at different milestones.

## Boundaries
- **One run, one page.** Rendering an entire site is an SF crawl with JS Rendering
  enabled, not a job for this tool.
- **No Playwright means no rendering.** The tool returns `ok: false` and an installation
  command instead of misrepresenting the result:
  ```bash
  pip install 'seohead[render]' && python -m playwright install chromium
  ```
- **It does not fix the issue.** The finding "content is client-rendered" is a diagnosis;
  the solution (SSR, SSG, or prerendering for bots) depends on the stack.

## Decision points
- **Diff sits between "widget" (~5%) and "alert" (30%+).** The skill only
  names the two extremes explicitly; for anything in between, judge by
  *what* changed, not just the percentage — a 15% text diff that includes
  the main product description matters more than a 25% diff made of a
  repeated footer.
- **`load` vs `networkidle` wait mode.** Default to `load` (see "Why Wait
  for `load`" below) even though it is less exhaustive, because
  `networkidle` reliably times out on real sites with live analytics, chat,
  or ads. Switch to `networkidle` only for a specific page already suspected
  of firing content late — not as a first-pass setting.
- **Title/canonical changed by a script.** Before flagging this as critical,
  check what it changed *to*: a script normalizing a trailing slash or
  protocol is cosmetic, while a script rewriting a source-HTML canonical to a
  different page (or every page to the homepage) is the critical case this
  check exists to catch. When source HTML has no canonical and JavaScript adds
  one, record the JS-only canonical and validate the rendered target and
  crawlability; do not treat injection alone as critical. Google permits this
  fallback when the canonical cannot be set in source HTML; see [Google's
  canonical guidance](https://developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls).
- **`empty_shell` present but the rest of the findings look mild.** An empty
  root container combined with a small raw/rendered diff usually means the
  fetch failed before JS executed (timeout, bot-block, redirect) rather than
  a healthy page — re-run before reporting a clean result.
- **`ok: false` with `reason: "incomplete_render"` is not a site finding.**
  The tool detects the wholly failed case for you: a rendered document with
  no title, no `h1`, no canonical and no internal links, far smaller than the
  raw response, is reported as unavailable with a named reason instead of the
  false "the title changes after JavaScript" and "the canonical is injected
  by JavaScript" it used to emit (#623). `js_dependent` is `null` there — the
  run does not know, which is neither clean nor a defect. Re-run it (a longer
  `--timeout`, or `--wait domcontentloaded`) and report the check as blocked
  until it completes. Never quote the truncated snapshot as evidence.

## Definition of done
- [ ] `render-check` has been run for the URL(s) in scope, with `--viewport
  mobile` added where layout differs by device.
- [ ] The recorded `user_agent` and `viewport_size` match the representation
  being interpreted; a raw/browser comparison never mixes two identities.
- [ ] The `raw` vs `rendered` diff has been read for words, internal links,
  title, h1, canonical, and JSON-LD — not just the overall percentage.
- [ ] Every finding at or above the 30% threshold, or any critical-severity
  finding regardless of percentage, is called out explicitly with its
  interpretation from "How to Interpret Findings."
- [ ] Lab metrics are labeled as lab data, not presented as field/CrUX data,
  in the conclusion delivered to the user.
- [ ] If Playwright was missing, that is reported as a blocked precondition,
  not silently skipped.
- [ ] Any URL that returned `reason: "incomplete_render"` is reported as an
  unmeasured page and re-run, never written up as a JavaScript finding.

## Cost
One `seohead render-check --url ...` invocation per page (two if both mobile
and desktop viewports are checked). No other `seohead` command is involved
and no paid API is touched — rendering runs a local headless Chromium via
Playwright. Time is a few seconds to roughly 30 seconds per page (browser
boot plus wait for `load`); forcing `--wait networkidle` can run far longer
on a live commercial site (up to timeout — 26 seconds was observed in one
measurement in this file). This is a per-page tool: auditing many pages
means many browser launches, so rely on an SF crawl with JS Rendering
enabled for site-wide coverage instead of looping this skill over a page list.

## Related Skills
`sf-config` (enable JS Rendering in the crawl if the diff reveals client-rendered content) ·
`regional-audit` (`--render` when the city selector is rendered by a script) ·
`schema-graph` (markup found only in the DOM) · `silo-audit` (internal linking that is
absent from the raw HTML).
