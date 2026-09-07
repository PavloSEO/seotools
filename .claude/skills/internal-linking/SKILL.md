---
name: internal-linking
description: >-
  Judges whether a site is linked well and where it is linked badly, from the crawl's own
  link graph rather than from inlink counts. Reads summary.internal_linking in audit.json:
  click depth from the crawl's start URL as a histogram and a maximum, internal edges by
  page position with unclassified ones kept separate, and how many edges are repeats of
  another edge. Then the four placement and graph checks — DEEP_CLICK_DEPTH,
  DUPLICATE_INTERNAL_LINK, LINK_INSIDE_HEADING, IMAGE_LINK_WITHOUT_TEXT — beside the
  existing ORPHAN_PAGE, INLINK_BOILERPLATE_ONLY, GENERIC_ANCHOR_TEXT and LOW_LINK_SCORE.
  Use when asked about internal linking, click depth, how deep a site is, orphan pages,
  whether pages are reachable, whether links live in body copy or only in the template,
  link equity distribution, or why indexed pages get no impressions. Triggers: internal
  linking, click depth, how many clicks from the home page, orphan pages, unreachable
  pages, link graph, anchor distribution, links in navigation vs content, perelinkovka,
  indexed but no traffic.
---

# Internal Linking — Is This Site Linked Well, and Where Is It Linked Badly?

The registry reports link defects one edge at a time. This skill asks the question those
findings cannot answer on their own, and it asks it in one order, because the numbers
qualify each other: **how deep is the site, where do its links live, and how much of the
graph is a copy of itself.**

## Read this before running anything

Two crawl properties decide which half of this method can run at all. Both are stated in
`audit.json`; neither is negotiable, and neither may be worked around by reading a
different number that happens to be present.

1. **Positions need `link_position.classify`.** It defaults to off (`seohead crawl-site
   --config-help`, `link_position.classify`), and a crawl run without it records an empty
   position on every edge. That is *unclassified*, not `content`. A run in that state
   cannot say whether the site is held together by body copy or by chrome, and the
   summary will say `unclassified: <every edge>` rather than pretend otherwise. Re-crawl
   with the flag on; do not infer positions from URL patterns.
2. **Depth needs a start URL and a finished frontier.** The walk starts at the URL the
   crawl actually began from, and `click_depth.seed` names it. The toolkit cannot know
   whether that URL is the site's root, so **you** have to look: a crawl seeded from a
   section page, or from a URL list with no start URL at all, describes the depth of
   *that* starting point and nothing else. Quote the seed with the histogram, always, or
   re-crawl from the root. A crawl with no recorded start URL and more than one page
   claiming `Crawl Depth 0` refuses to walk at all and says so. Separately, a crawl that
   stopped early (`run.crawl_partial: true` — a URL budget, a duration limit, an error
   streak, an interruption) cannot prove any page's shortest route, because the part it
   never fetched may hold a shorter one; there `DEEP_CLICK_DEPTH` is withheld by name,
   whether or not it had fired, and `click_depth.measured` is `false` with the reason.

**`pages.crawl_depth` is not click depth.** It records the depth at which the crawler
happened to reach a URL. On the crawl that produced this method's field measurements,
33 471 of 40 920 pages recorded `0` because the sitemap seeded them. Reading that column
as click depth reports a linked-list archive as a flat site. The toolkit walks the `links`
graph from the start URL instead; the column is not an acceptable substitute anywhere in
this skill.

## Trigger
- "Is the internal linking any good?", "how well is this site linked?"
- "How many clicks from the home page is this page / the deep archive?"
- "Find the orphan pages", "which pages are unreachable?"
- "Are links in body copy or only in the menu and footer?"
- "These pages are indexed and get no impressions — why?"
- Frontmatter triggers: internal linking, click depth, how many clicks from the home page,
  orphan pages, unreachable pages, link graph, anchor distribution, links in navigation vs
  content, perelinkovka, indexed but no traffic.

## Anti-trigger
- The question is whether the URL tree forms topical silos with hubs and clusters — that
  is `silo-audit`, which reads structure and coverage. This skill reads the link graph and
  says nothing about topics.
- The question is which links are broken and where they sit in the DOM — `sf-analyzer`
  already localizes `BROKEN_INTERNAL_LINK` and its siblings. Depth and position do not
  make a 404 more or less broken.
- The question is whether a heading hierarchy is correct — `heading-outline`.
  `LINK_INSIDE_HEADING` here is about the link, not about the outline; a page can have a
  flawless H1–H6 sequence and still have every heading pointing away from itself.
- No crawl exists yet. There is no link graph to walk. Run `control`'s step 2 first.
- The deliverable is a client-facing narrative or a backlog — hand these numbers to
  `sf-report` / `sf-tasks` rather than presenting the histogram as the report.

## Preconditions
- [ ] An `audit.json` from a finished crawl (`seohead crawl-site`, or `seohead sf run`
      over a Screaming Frog export that includes **All Inlinks** — without that bulk
      export the whole graph half of this skill skips by name).
- [ ] For the position half: the crawl was run with `link_position.classify` on.
- [ ] For the depth half: the crawl started at the site root and `run.crawl_partial` is
      `false`.
- [ ] The site's own notion of a "hub" is known well enough to say where a deep page
      *should* have been linked from — otherwise step 5 has advice but no destination.

## Workflow

1. **Crawl with classification on.** Positions are the one input that cannot be
   reconstructed afterwards, so turn them on for the run that answers this question:
   ```bash
   seohead crawl-site --url https://example.com --set link_position.classify=true --out-dir ./run
   ```
   Classification is not what makes a crawl slow — it is a per-link DOM ancestry test on
   a page already parsed — but it is off by default because most crawls never read it.
   From a Screaming Frog export instead, `Link Position` comes from the **All Inlinks**
   bulk export, and the same audit is produced by:
   ```bash
   seohead sf run --exports-dir ./exports --out ./report --tasks
   ```

2. **Read the block, in this order.** Every number this method needs is in it:
   ```bash
   python3 -c "import json; print(json.dumps(json.load(open('./run/audit.json'))['summary']['internal_linking'], indent=2))"
   ```
   - `click_depth.within` — pages within 3, 5 and 10 clicks of the start URL;
   - `click_depth.max` — the deepest page's shortest route;
   - `click_depth.reachable` / `unreachable` — of the crawl's own HTML pages;
   - `by_position` and `unclassified` — internal edges by where they sit;
   - `duplicate_edges` / `duplicate_fraction` — edges that repeat another edge.

   If `measured` is `false`, stop and read the `reason`. It is the answer to "why can I
   not have this number", and it is the sentence to report — not a smaller number found
   somewhere else.

3. **Judge the depth shape.** A healthy site puts most of its pages within 3–5 clicks and
   its maximum in the low teens. Two shapes are defects, and they are different:
   - **A long tail past 10 with a large maximum** is a chain, not a tree. Pagination that
     redirects, "next post" links doing the work of a rubric index, a category that lists
     only its first page. The archive is linked and effectively invisible.
   - **A large `unreachable` count** is the classic orphan problem, and it is usually the
     *smaller* of the two. On the site this method was measured against, 2 912 pages were
     unreachable and 23 742 were linked but past ten clicks. A report that names the
     orphans and stops confirms the site is fine.
   Pair this with search-console or analytics data if you have it: pages indexed (because
   a sitemap lists them) and drawing no impressions are the population depth explains and
   nothing else in this toolkit connects.

4. **Judge the position distribution.** Content edges as a small share of the total means
   the site is held together by chrome; a page whose only inlinks are chrome is
   `INLINK_BOILERPLATE_ONLY`, which is the per-page form of the same finding. Two reading
   rules, both mandatory:
   - **`unclassified` is not `content` and not a defect.** Report it as its own number.
     A run whose edges are all unclassified has no position answer at all.
   - **A position absent from `by_position` is not absent from the site.** Rules are tried
     in order and the first match wins, so a menu inside `<header>` classifies as `nav`
     and `header` never appears. Say "no edges classified as header", never "the site has
     no header".

5. **Read the four checks, then say what to link and from where.**

   | Check | Fires when | The fix it implies |
   |---|---|---|
   | `DEEP_CLICK_DEPTH` | a page's shortest route from the start URL exceeds `thresholds.click_depth_max` | link it from a hub, rubric index or related page nearer the root |
   | `DUPLICATE_INTERNAL_LINK` | one page writes the same destination and anchor more than once | emit the block once; a layout element rendered twice duplicates every link in it |
   | `LINK_INSIDE_HEADING` | an anchor sits inside an `h1`–`h6` | let the heading name this page; move the link into the copy below it |
   | `IMAGE_LINK_WITHOUT_TEXT` | an image link has no anchor text, no `alt`, no `aria-label`/`title` | describe the image, or add anchor text beside it |

   Read `GENERIC_ANCHOR_TEXT` together with `by_position`, never alone: a bare "read more"
   in a footer is furniture, the same anchor in body copy is a lost signal.

6. **Report the numbers with their thresholds.** Every depth verdict carries
   `floor_used` — the configured `thresholds.click_depth_max`, default 10 — in the summary
   and in each finding's `details`. Quote it. "23 742 pages deeper than 10 clicks" is a
   measurement; "23 742 pages are too deep" is that measurement with the number that
   produced it deleted.

## Decision points
- **Depth measured but the crawl is a subset.** A crawl narrowed by
  `scope.include_patterns` finishes its frontier and is not `crawl_partial`, yet its depth
  describes the subset. Say which population the histogram covers before quoting it.
- **`duplicate_fraction` is high.** Do not report it as "half the links are wasted". It is
  a template fact first: find the one block rendered twice (`DUPLICATE_INTERNAL_LINK`'s
  `repeats` names the destinations and anchors) before drawing any conclusion about link
  equity. On the measured site 44.9% of edges were repeats and they were one masthead
  emitted twice, the second copy removed by JavaScript.
- **A deep page that should be deep.** Archive pagination past page 20 is genuinely far
  from the root and that is not always wrong. Judge by whether anything *else* links the
  page — a rubric index, a related-posts block, a tag page — not by depth alone.
- **`unreachable` on a rendering-dependent site.** A menu built by JavaScript leaves every
  page it links unreachable in a raw crawl. Check `run.requires_rendering` and
  `render_escalation` before reporting orphans; `js-render-check` settles it.
- **A threshold that fires on most of the site.** `summary.implausible_checks` names any
  check covering more than half the crawled pages. On a genuinely chain-shaped archive
  `DEEP_CLICK_DEPTH` belongs there and is correct; look at it anyway before believing the
  count, because that is what the list is for.

## What a bad answer looks like
Each of these has been produced by a real report, and each is wrong in a way the numbers
above make avoidable:

- **"66% of the site is unreachable by internal links"**, derived by subtracting a crawl's
  URL count from the sitemap's. That measures "the crawler did not get there", not
  "nothing links to it". The reachable count comes from walking the graph, and on that
  site the real figure was 91.9% reachable.
- **"Maximum depth 4"**, read from `pages.crawl_depth`. The column records where the
  crawler reached a URL, not the shortest link route; the real maximum was 3 005.
- **A position distribution quoted from a crawl that never classified anything.** Every
  edge carries an empty position there, and an empty position is not `content`. The block
  reports the whole graph under `unclassified`; the answer is "not measured, re-crawl with
  `link_position.classify`", never a percentage.
- **"The site has no header links"**, from a distribution where `header` does not appear.
  First match wins, and a menu inside `<header>` is `nav`.
- **"No orphans, so internal linking is healthy"**, on a site with 23 742 pages past ten
  clicks. Zero inlinks is the rare case; the expensive case is linked and unreachable in
  practice.
- **A depth histogram from a partial crawl.** The toolkit withholds it; a report that
  quotes one has read a number the crawl could not prove.

## Definition of done
- [ ] `summary.internal_linking` was read, and either every sub-block is `measured: true`
      or each `false` one's reason is stated in the report.
- [ ] The four depth numbers (within 3, within 5, within 10, maximum) and the
      reachable/unreachable split are quoted, with the population they describe.
- [ ] The position distribution is quoted with `unclassified` as its own number, never
      folded into `content`.
- [ ] Every threshold-dependent verdict names the number it used
      (`thresholds.click_depth_max`).
- [ ] The four checks in step 5 were read, and any that are in `run.checks_skipped` are
      reported as skipped with their reason rather than omitted.
- [ ] The recommendation names specific pages to link *and* specific pages to link them
      from, not "improve internal linking".

## Cost
No `seohead` command beyond the crawl that produced `audit.json`, and no paid API. Within
that crawl, `link_position.classify` adds a per-link DOM ancestry test on a page already
parsed; on a 2 500-page, 1.1 M-edge crawl of a real site the run averaged well inside the
configured request cap, so the rate limit rather than classification set the pace. The
click-depth walk is one breadth-first pass over the stored graph — no requests, and it
re-runs against a saved scan for free.

## What to Deliver to the User
- **The depth table**: pages within 3 / 5 / 10 clicks, the maximum, and reachable versus
  unreachable, with the seed URL named.
- **The position table**: internal edges by position, with `unclassified` on its own row.
- **The duplicate figure**: surplus edges and their share, plus the template block behind
  it if one explains most of them.
- **A ranked fix list**: which deep pages to link, from which hubs; which template block
  is duplicated; which headings and image links to repair.
- **What could not be measured**, with the crawl property that prevented it.

## Integrations
- `control` — routes here as part of an unscoped audit and owns the crawl step.
- `silo-audit` — reads the same `audit.json` for structure; run it beside this, not
  instead of it, when the question is topical rather than graph-shaped.
- `heading-outline` — the other half of `LINK_INSIDE_HEADING`: whether the outline is
  sound at all.
- `js-render-check` — settles whether "unreachable" means "linked only by JavaScript".
- `sf-report` / `sf-tasks` — turn these numbers into a narrative and a backlog.
