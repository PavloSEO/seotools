---
name: robots-audit
description: >-
  Perform an in-depth analysis of robots.txt for redundant and harmful directives:
  what is actually blocked, whether rendering is impaired, Allow/Disallow conflicts,
  overly broad rules, and a missing Sitemap. Cross-check Disallow rules against live
  200-status pages from sf-analyzer and against the sitemap, then return a list of
  issues plus a corrected robots.txt as a diff. Use when asked to "check robots.txt,"
  "audit robots.txt," investigate whether "robots.txt blocks" something, analyze
  Disallow/Allow, or determine "what is blocked by robots.txt." Triggers: robots.txt,
  redundant robots directives, robots.txt blocks, disallow, allow, check robots.txt,
  robots.txt audit, blocked in robots.txt, blocked by robots.
---

# robots-audit

An agentic `robots.txt` audit built on top of a Screaming Frog crawl. SF shows
"blocked by robots" page by page, but it does not explain WHY a rule is harmful or
HOW to rewrite it. This skill fills that gap.

## Trigger

- The user asks to analyze `robots.txt`, verify that it does not block important content,
  or find redundant directives.
- An `sf-analyzer` audit reports "Blocked by robots.txt" or unusual index coverage gaps.
- A crawl/indexing section is needed before delivering an `sf-report` report.

## Anti-trigger

- The question is about **indexing**, not crawling — "is this page indexed",
  "should this be `noindex`". Robots.txt controls crawling only; go to
  `seo-recon` / page-level `parse` for meta robots and canonical instead of
  reading intent into `Disallow` rules it does not have.
- No live site and no `sf-analyzer` audit exist yet, and the user has not
  supplied a `robots.txt` file directly — there is nothing to audit. Run
  `sf-analyzer` (or fetch the file per step 1 below) first.
- The ask is "generate a robots.txt for a new site" rather than auditing an
  existing one — that is authoring, not auditing; this skill's checks assume
  a file already exists and needs review.

## Preconditions

- [ ] `https://SITE/robots.txt` resolves with `200` (see step 1) — a 404/5xx
  is itself the finding, not a reason to stop, but note it before running the
  heuristic checks below. Google's robots parser temporarily treats a `5xx`
  robots.txt response as a full disallow ([Google's robots specification](https://developers.google.com/crawling/docs/robots-txt/robots-txt-spec)).
- [ ] At least one of: an `sf-analyzer` `audit.json` with live 200-status
  pages, or a fetchable `sitemap.xml`, or a page list supplied by the user —
  without a reference set, "blocks live pages" cannot be checked, only
  syntax and structure.

## Workflow

1. **Download it.** `curl -sL -A 'Mozilla/5.0' https://SITE/robots.txt -o /tmp/robots.txt`.
   Check the HTTP status: `curl -sIL https://SITE/robots.txt | head -1`. If it is not `200`,
   that is already a problem (404 means no rules are present; 5xx may cause crawlers to treat
   the entire site as blocked).
2. **Parse it by `User-agent` group.** Collect the `Disallow`/`Allow`/`Sitemap`/`Crawl-delay`
   directives within each group. Remember that rules apply to their own `User-agent` group,
   not globally; `*` is the fallback. An empty `Disallow:` means "allow everything."
3. **Collect reference data.** Take the list of live 200-status pages from the `sf-analyzer`
   audit (`audit.json`, the internal/200 field) and URLs from the sitemap:
   `curl -sL https://SITE/sitemap.xml | grep -oE '<loc>[^<]*</loc>' | sed -E 's#</?loc>##g'`
   (POSIX `-E`/`sed`, not GNU-only `-P`, so it also runs on macOS's stock BSD grep). This represents
   "what should be indexed."
4. **Checks (heuristics):**
   - **Blocks live, important URLs.** For each `Disallow`, check whether it matches 200-status
     pages and/or URLs from the sitemap. A conflict where a page appears in the sitemap but is
     blocked by `Disallow` is a red flag.
   - **Blocks rendering resources.** A `Disallow` for `*.js`, `*.css`, `/_next/`, `/static/`,
     `/assets/`, `/wp-content/`, or `/wp-includes/` can prevent Google from rendering or
     analyzing a dependent page as expected ([Google's JavaScript SEO basics](https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics)).
     Severity: high.
   - **Rules are too broad.** `Disallow: /*?` or `Disallow: /*?*` blocks EVERY URL with
     parameters. This often also blocks pagination (`/blog?page=2`), filters, and UTM landing
     pages. `Disallow: /` in the `*` group blocks the entire site; check whether it was left
     over from development or staging.
   - **Allow/Disallow conflicts.** An `Allow` and `Disallow` in the same group match overlapping
     paths. State the rule clearly: Google applies the longer, more specific matching rule, not
     the rule that appears first ([Google's robots specification](https://developers.google.com/crawling/docs/robots-txt/robots-txt-spec)).
   - **No `Sitemap:`** Add the sitemap's absolute URL.
   - **`Crawl-delay`** Google does not support this directive in its robots parser
     ([Google's robots specification](https://developers.google.com/crawling/docs/robots-txt/robots-txt-spec)).
     Do not present it as a Googlebot throttle; retain it only if another crawler needs it.
   - **Duplicates and typos.** Repeated `Disallow` directives, `Dissallow`/`Disalow`, `Useragent`
     without the hyphen, a relative `Sitemap:` URL (it must be absolute), and paths without a
     leading `/`.
5. **Create a corrected `robots.txt`** and show a **diff** against the original
   (`diff -u /tmp/robots.txt /tmp/robots_fixed.txt`). Make minimal changes; do not reformat it
   merely for the sake of tidiness.

A small Python script using the standard-library `urllib.robotparser` is convenient for checking
whether paths match rules:
`python -c "import urllib.robotparser as r; rp=r.RobotFileParser(); rp.parse(open('/tmp/robots.txt').read().splitlines()); print(rp.can_fetch('*','/blog?page=2'))"`.

## Decision points

- **A `Disallow` matches sitemap/200-status URLs.** Do not assume it is a bug —
  check whether the blocked section is genuinely non-public (checkout, admin,
  search-result pages) before flagging it as high severity. Ask the user when
  the intent is unclear rather than guessing.
- **`Disallow: /` under the `*` group.** Before flagging this as a launch-
  blocking incident, check whether a more specific `User-agent` group above it
  already re-opens the site for the crawlers that matter — the `*` line is not
  automatically the effective rule for every bot.
- **Conflicting `Allow`/`Disallow` of equal specificity.** The spec is
  ambiguous here and crawlers differ; say so explicitly instead of picking a
  winner silently, and recommend making one of the two rules more specific.

## Definition of done

- [ ] Every `Disallow`/`Allow` directive has been checked against both the
  rendering-resource list and the live/sitemap reference set from Preconditions.
- [ ] Every finding carries a severity, the exact offending line, and the
  affected URLs (or a stated reason none could be enumerated).
- [ ] A corrected `robots.txt` exists and its diff against the original is
  minimal — no reformatting unrelated to a finding.
- [ ] The crawling-vs-indexing distinction has been stated in the conclusion
  delivered to the user (see "What to deliver to the user").

## Cost

Two `curl` requests (robots.txt, its HTTP-status check) plus one for the
sitemap if no `sf-analyzer` audit is already available — under 5 requests,
sub-second each, no paid API involved. The bulk of the time is the heuristic
review itself, not network I/O.

## What to deliver to the user

- **Issue list** by severity (high: rendering, important sections, or the entire site are blocked;
  medium: broad rules or a missing Sitemap; low: typos or an unnecessary Crawl-delay), including
  the exact line and the URLs it affects.
- A **corrected `robots.txt`** plus a diff.
- **The main rule to state in the conclusion:** `canonical` and `noindex` (meta/HTTP) control
  indexing, while `robots.txt` controls crawling ONLY. You cannot "remove a page from the index"
  with `Disallow`: a page blocked by robots.txt will not receive the `noindex` directive because
  the bot cannot read it, and the URL may remain in search results without a snippet
  ([Google's noindex guidance](https://developers.google.com/search/docs/crawling-indexing/block-indexing)).

See also: `sf-analyzer`, the source of the 200-status page list and sitemap data; `sf-report` /
`sf-tasks`, which place the discovered issues into the report and backlog.
