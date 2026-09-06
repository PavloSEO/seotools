# Skill map

22 skills in `.claude/skills/`, in two tiers.

**Method skills** — 21 of them. Each covers one thing well: when to apply it, in what
order, how to read the result, and where the boundary is beyond which the tool starts to lie.

**The controller** — `control/`, which decides *which* method skill to run on a site nobody has
looked at yet, and whether to believe the answer. It routes rather than restating, and it is
the only skill that carries its own sub-skills and a reference archive:

```
.claude/skills/control/
  SKILL.md                     the entry point: the loop, the decision points, the cost
  subskills/scoping.md         how big is this site, what runs on it, what can be skipped
  subskills/rate-and-load.md   what rate a host tolerates, and whose fault the errors are
  subskills/reading-an-audit.md  fired vs skipped vs silent; when a score is not earned
  subskills/verifying.md       confirming a finding live before it reaches anybody
  subskills/deliverables.md    turning findings into a task, an archive, a document
  reference/defects.md         bugs found on live sites, and what gave each one away
  reference/populations.md     which set each check describes, and its invalid comparisons
  reference/limits.md          what this toolkit cannot answer at all
```

Each sub-skill is loadable on its own: a reader who needs only the rate lesson should not have
to read the deliverables section. The reference archive matters as much as the sub-skills —
every defect found on a live site so far was recognisable by a pattern, and writing those down
is what lets the next run catch one in minutes instead of an afternoon.

## How to choose

```
given a domain, what to do?
   └─ control ────────── the entry point for any unscoped audit request: scope,
        │                 crawl, scan, read the audit honestly, verify live,
        │                 produce the deliverable
        ├─ seo-deep-audit ─ delegate here for the crawl step instead of native
        │                    crawl-site only when SF (licensed CLI or exports)
        │                    is available and full-registry depth is wanted
        └─ audit-roadmap ─ when the domain is new: scout the minimum
                            first and decide what to collect
```

Then by the layer of the task.

## Orchestrators

| Skill | When |
|---|---|
| **control** | The single entry point for an unscoped "audit this site" request, or when you are about to write a one-off script to check pages. The whole loop: scope, crawl, `log-scan` the run, read `audit.json`'s honesty fields before its findings, verify criticals live, build the deliverable. Routes to the method skills below rather than restating them; carries its own sub-skills and reference archive. Written against a 4 260-URL run over three live sites |
| **seo-deep-audit** | Not a second unscoped-audit entry point — `control` delegates its crawl step here when a licensed SF CLI or supplied exports are available and full-registry depth is wanted, and it is also fine to call directly once that decision is already made (SF/exports named or already in hand) |
| **audit-roadmap** | Unfamiliar domain: 5 minutes of recon to decide what to collect next |
| **sf-boundaries** | The fork "does Screaming Frog cover this, or does it need an agent?" — a router |

## Screaming Frog crawl audit

| Skill | When | Tool |
|---|---|---|
| **sf-analyzer** | There is a crawl or exports — produce a machine-readable audit | `sf run` |
| **sf-config** | Configure SF once to maximize applicable coverage from the 139-check registry | — |
| **sf-report** | Turn the export into a human-readable report | `sf run --out` |
| **sf-tasks** | Build a prioritized backlog from `audit.json` | `sf tasks` |

## Recon and technical hygiene

| Skill | When | Tools |
|---|---|---|
| **seo-recon** | Domain age, hosting, CDN, caching — everything SF does not give | `domain-profile`, `cdn-check` |
| **tech-audit** | What the site is made of: CMS, framework, analytics, pixels | `tech-detect` |
| **security-audit** | Security headers through an SEO lens | `security-check` |
| **robots-audit** | robots.txt dissected for harmful directives | `robots-check` |
| **js-render-check** | What appears only after JavaScript + lab metrics | `render-check` |
| **regional-audit** | Regions: subdomains, folders, satellites, branches across Russia | `regions-check` |

## Content and structure

| Skill | When | Tools |
|---|---|---|
| **schema-graph** | Structured data: dissect, validate, build a `@graph` | `schema-check`, `schema-build` |
| **duplicate-audit** | Near-duplicates and thin pages | `duplicate-check` |
| **heading-outline** | The H1–H6 structure and its hierarchy | `parse` |
| **silo-audit** | Is the structure silo-like, hubs, interlinking, orphans | `links-check`, `sitemap-crawl` |
| **backlinks-check** | Verify links against your own donor list | `backlinks-check` |
| **geo-aeo-audit** | Visibility in AI answers: crawlers, llms.txt, citability | `ai-bots-check`, `llms-txt-check`, `citability-check` |

## The audit as a whole and reports

| Skill | When | Tools |
|---|---|---|
| **site-report** | The whole site dissected and a ready file — Excel, Word, CSV | `site-audit`, `report-build` |

## Analytics consoles and exports

| Skill | When | Tools |
|---|---|---|
| **analytics-console-review** | A user-authorized signed-in console or aggregate export is available, but no provider API is configured | Host browser or user export; optional `sources-doctor`, `metrika-report`, and page/SF checks |

## Tools without a skill of their own

26 of the 55 commands are not named in any skill's own body (a mention inside
another tool's Markdown table above does not count) — used inline as plumbing
inside a workflow's write-up, or not yet needed by one at all — and have no
skill of their own, deliberately: a skill per single command is noise.
`tests/test_docs_drift.py` recomputes this list by scanning every skill file
for each command name, so it cannot silently rot the way this line once did.

Page-level utilities: `asset-weight-check` · `boilerplate-report` ·
`crawl-describe-settings` · `facts-export` · `hreflang-check` · `images-download` ·
`images-optimize` · `keywords-cluster` · `log-analyze` · `mirror-check` ·
`redirects-check` · `redirects-generate` · `soft404-check`

External data sources (`data_sources/` layer): `crtsh-subdomains` ·
`crux-report` · `google-keywords` · `google-serp` · `gsc-query` ·
`indexnow-submit` · `keywords-exact` · `keywords-expand` ·
`keywords-seasonality` · `regions-tree` · `serp-fetch` · `spend-report` ·
`wayback-history`

Two of them are candidates for a skill if the work becomes regular:
`log-analyze` (log parsing is its own genre with its own method) and
`redirects-generate` (site migrations).

## Skill rules

**Where a skill lives.** A general method applicable to any project ->
`~/.claude/skills/`. Knowledge about this repository -> `.claude/skills/`
here.

**Skills must age together with the code.** A new tool appears — the skill
that used to teach doing the same by hand gets rewritten. That is how
`js-render-check` stopped explaining `curl` and headless Chrome and started
documenting `render-check`.

**Every skill has a "Boundaries" section.** What the tool does not do and
what cannot be claimed from its output. Without it a skill turns into an
ad for the tool.

**Consistency check** — `tests/test_docs_drift.py`: every `seohead` command
mentioned in any skill must exist in the CLI. A skill referencing a
non-existent command fails the suite.
