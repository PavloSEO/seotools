---
name: control
description: >-
  The single entry point for an unscoped audit of a site nobody has looked at yet: decide
  what to run, in what order, and how to read what comes back. Routes to the specialised
  method skills rather than restating them, and carries its own sub-skills for scoping,
  reading an audit honestly, verifying a finding live, producing a deliverable, and
  choosing a crawl rate. Use this before any other skill in this repository when the ask
  is "audit this site", "what is wrong with this site", "run everything on this site",
  "stress-test the toolkit on a real site", or when you would otherwise write a one-off
  script to check pages — `seo-deep-audit` is not an alternative entry point for this same
  request; it is the SF-based collector this skill delegates to when that is specifically
  what is wanted. Triggers: audit site, full analysis, crawl and report, stress test the
  toolkit, what should I run. Localized Russian trigger examples: audit this site, full
  audit, run everything, stress test.
---

# control — what to run, and how to read what comes back

## What this repository is for

Checking every page of a site by hand, as an agent, produces the best analysis and costs the
most. A script is cheap and usually stupid. This repository exists to close that gap: **encode
what an agent would do by hand into configs and scripts, test them on real sites, and get
hand-inspection quality at any number of pages.** The output has to be trusted enough that you
use it *instead of* writing a throwaway script — and if it is not, the fix is a test and an
issue, not a throwaway script.

Everything in these files was run against real sites. Every command is one that ran; every
number is one that came back.

## Two tiers of skill

| Tier | Where | What it knows |
|---|---|---|
| **Method** | the other 22 skills in `.claude/skills/` | how to do one thing well: robots, rendering, schema, silos, headings, regions, backlinks, security |
| **Controller** | this file and `subskills/` | which of them to run, in what order, and whether to believe the answer |

This skill routes; it does not restate. When a step below names a method skill, load that skill
rather than reimplementing it here.

## Trigger

A site, a domain, or a crawl, with no scope stated. "Audit this", "what is wrong with it",
"run everything", "check the whole site".

## Anti-trigger

A stated narrow scope goes straight to the method skill: "only robots" → `robots-audit`, "just
the markup" → `schema-graph`, "does JS matter here" → `js-render-check`, "read this Screaming
Frog export" → `sf-analyzer`. Do not run the whole loop to answer one question.

## Preconditions

- [ ] The venv exists: `~/Projects/seohead-seotools-public-full/.venv/bin/seohead --version`
- [ ] A scratch directory outside the repository for artifacts
- [ ] A crawl rate decided — see [rate-and-load](subskills/rate-and-load.md) **before** the
      first request, not after the host starts refusing
- [ ] Permission, if this is somebody else's site

## The loop

**1. Scope it.** Size, stack, what can be skipped, what will be needed.
→ [scoping](subskills/scoping.md), and the `audit-roadmap` skill for a written plan.

**2. Crawl once, with a config file, never with flags alone.** The config is the record of what
was measured; `crawl-site --config-help` lists every setting, and the results-affecting ones go
into the run manifest. → [rate-and-load](subskills/rate-and-load.md)

```bash
seohead crawl-site --url https://example.com --config ./crawl.json --out-dir ./run
```

A licensed Screaming Frog CLI or supplied SF exports are not a precondition for this step —
native `crawl-site` needs neither. Delegate this step to `seo-deep-audit`'s pipeline instead
only when SF (CLI or exports) is already available *and* full-registry depth is specifically
wanted; both collectors feed the same `audit.json` shape, so nothing downstream changes.

**3. Scan the run before reading it.**

```bash
seohead log-scan --run ./run
```

Exit 2 means the run's own numbers disagree with each other. Every defect this toolkit has had
reached a report before anybody noticed; this is the twenty seconds that stops the next one.

**4. Read the audit honestly.** Coverage before findings, and never a health score without the
sentence that qualifies it. → [reading-an-audit](subskills/reading-an-audit.md)

**5. Point the method skills at what the crawl surfaced.** The crawl says *where*; each skill
says *what*. `js-render-check` for rendering, `schema-graph` for markup, `robots-audit` for
directives, `silo-audit` for structure, `heading-outline` for hierarchy, `security-audit` for
headers, `duplicate-audit` for near-duplicates, `geo-aeo-audit` for AI visibility. One page per
template first; do not pay for the whole site to learn what one page would have told you.

**6. Verify every serious finding live.** → [verifying](subskills/verifying.md)

**7. Produce the thing that was actually asked for.**
→ [deliverables](subskills/deliverables.md), and `docs/scenarios/` for 59 chains end to end.

## Decision points

- **Rate.** Own site → fast. Somebody else's → 3 URL/s unless the owner sets otherwise, and the
  circuit breaker's verdict overrides the owner's number.
- **Render or not.** `render-check` on one page per template. If raw and rendered are
  equivalent, do not pay to render the site.
- **Which findings go in the report.** Critical and warning, each verified live. Notices only
  when no single check dominates them.
- **Stop and file.** If one check is above ~50% of all findings, stop trusting it for this
  report, verify five of its hits, and file the bug. → [reference/defects](reference/defects.md)
- **Native crawl vs. `seo-deep-audit`.** Default to step 2's native `crawl-site`. Delegate to
  `seo-deep-audit`'s SF-based pipeline instead only when a licensed SF CLI or exports are
  already available and full-registry depth is worth the extra setup — not because a scope is
  unstated; an unstated scope alone always means "run this loop," never "switch collectors."

## Definition of done

- [ ] `audit.json` exists and `crawl_finish_reason` is `finished`, or the reason is stated in
      the deliverable.
- [ ] `log-scan` exits 0, or every anomaly it reported is explained.
- [ ] The coverage sentence (`health_score_basis`) appears next to any score.
- [ ] Every critical in the deliverable was reproduced live.
- [ ] Any check dominating `by_check` was verified or filed.
- [ ] The config file used is attached to the deliverable.
- [ ] The limits are stated out loud. → [reference/limits](reference/limits.md)

## Cost

- **Network:** one request per URL, at the configured rate, same host only. External links are
  recorded and never fetched; a redirect off-host is recorded and never followed.
- **Money:** none. Every tool in this loop is free; paid sources sit behind `sources-doctor`
  and a spend log and are not part of it.
- **Time:** 387 pages at 20 req/s worst case took under a minute; 3387 pages at 3 req/s took
  about twenty. Rendering is an order of magnitude more per page.
- **Writes:** only under `--out-dir` and the scratch directory you chose.
