# Scenario 6 — Blocked by robots.txt: the pages, and the resources, a crawler never gets

## The question

> Our new section is not in Google at all. Nothing is broken, the pages load fine. What is
> going on?

The most expensive line in technical SEO is one `Disallow` that matched more than its author
meant. It leaves no error, no slow page and no failing test — only a section of a site that
nothing outside can read.

## Covers

- **Response Codes** — Internal Blocked by Robots.txt · Internal Blocked Resource

## The chain

The question above names Google specifically, so match every step to Googlebot's own token
rather than this crawler's default (`robots.user_agent_token`, default `SEOHEAD-Tools`).
`robots.txt` groups can and do single one agent out for a rule that leaves every other agent
untouched, so a run under the wrong token can report a clean crawl for a site that blocks
Googlebot outright.

**1. Read the file itself, parsed the way Googlebot groups it.**

```bash
seohead robots-check --input '{"url": "https://example.com", "user_agent": "Googlebot"}'
```

Groups come back per user-agent with their `allow`, `disallow` and `crawl_delay`, plus every
sitemap the file declares. Reading `robots.txt` by eye is how a rule that applies only to one
agent gets mistaken for a site-wide block, and the other way round.

**2. Save the following JSON as `./config.json`, and crawl respecting it — the honest baseline.**

```json
{"robots": {"user_agent_token": "Googlebot"}}
```

```bash
seohead crawl-site --url https://example.com --config ./config.json --out-dir ./run
```

**3. Crawl your own site again in report-only mode, from the same config, to see what the rules
cost.**

```bash
seohead crawl-site --url https://example.com --config ./config.json --robots report_only --out-dir ./run
```

`report_only` fetches `robots.txt`, reports every URL it would have blocked under the configured
token, and crawls anyway. It is a mode for a site you are responsible for. On somebody else's
site the answer to "what is behind the block" is to ask them.

Keeping the two commands on the same config is what makes the diff in step 4 mean something:
matching them against different tokens would make "the block cost N pages" a number about two
different agents, not one. A generic run without an agent token still answers a different,
narrower question — what `SEOHEAD-Tools` itself can reach — and is fine labelled as that.

**4. Diff the two runs, so the block is a measured difference and not an impression.**

```bash
seohead compare-crawls --before ./old-audit.json --after ./new-audit.json
```

**5. Separate the two kinds of block in the findings.**

| Check | What it means |
|---|---|
| `BLOCKED_BY_ROBOTS` | a URL matched a `Disallow` rule |
| `IMPORTANT_URL_BLOCKED_BY_ROBOTS` | a live, internally linked page is blocked anyway |
| `ROBOTS_BLOCKS_RESOURCES` | JavaScript or CSS the page needs to render is blocked |

The middle one is the finding worth waking somebody for: the site links to the page, so it is
meant to be found, and the rules stop discovery from reaching it. Its most common shape is
pagination — `/blog?page=N` caught by a broad `Disallow: /*?` written to keep facets out.

The third matters even when every page is crawlable: a renderer that cannot fetch the stylesheet
and the bundle sees a different page than the visitor does.

**6. Report it with the rule that caused it.**

```bash
seohead report-build --audit ./run/audit.json --format md --out ./robots.md
```

## What comes out

A list of blocked URLs, split into the ones nothing links to (usually deliberate) and the ones
the site links to itself (usually not), plus the blocked render-critical resources.

The fix is rarely "remove the rule". It is a more specific `Allow` beside the existing
`Disallow`, and indexing controlled where indexing is actually controlled — a canonical or a
`noindex` — because `robots.txt` governs crawling, not indexing.

## What it costs

`robots-check` is one request. The report-only crawl costs a full crawl again, so run it when
the first crawl's coverage was visibly short, not by reflex. Nothing paid.

## What it cannot answer

- **Whether a blocked URL is indexed.** Blocking crawling does not remove a URL from an index;
  it removes the crawler's ability to see what is on it, including a `noindex` you added.
- **What is on the blocked page.** Under the default policy it is never fetched, and that is
  the point of the default.
- **How another crawler reads the same file, in the same run.** Matching is done for one
  configured `robots.user_agent_token` at a time — `Googlebot` above, `SEOHEAD-Tools` by default
  — so a rule aimed at a third agent is reported as text in step 1's groups, not applied, unless
  you rerun steps 1–3 with that agent's own token.
- **Another host's rules.** A blocked resource served from a CDN or a third-party domain is not
  detected: this crawler does not go and ask somebody else's server about its own rules.
- **Whether the block is intentional.** A staging path or a checkout should be blocked. The
  finding says a rule matched a linked page; the intent is a conversation.
