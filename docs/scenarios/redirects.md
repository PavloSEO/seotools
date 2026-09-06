# Scenario 5 — Redirects: the loop, the chain, and the hop nobody needed

## The question

> Half the URLs in the crawl are 301s. Is that a problem, and which ones do we actually fix?

A redirect is not a defect. A redirect that another page on the same site links to, a redirect
that takes three hops to arrive, and a redirect that never arrives at all are three different
defects, and only one of them is urgent.

## Covers

- **Response Codes** — Internal Redirect Loop · Internal Redirect Chain · Internal Redirection (3XX) · Internal Redirection (Meta Refresh) · Internal Redirection (HTTP Refresh)

## The chain

**1. Crawl, and let the crawler follow redirect targets.** It does by default
(`discovery.redirects.crawl`), which is what makes the destination's status part of the record
instead of an assumption.

```bash
seohead crawl-site --url https://example.com --config ./crawl.json --out-dir ./run
```

**2. Scan the run.**

```bash
seohead log-scan --run ./run
```

**3. Read the four findings apart from each other.**

| Check | What it means | Severity |
|---|---|---|
| `REDIRECT_LOOP` | the path cycles and never terminates | critical |
| `REDIRECT_CHAIN` | two or more hops before the destination | warning |
| `INTERNAL_LINK_TO_REDIRECT` | a page here links to a URL that redirects | warning |
| `BAD_REDIRECT_TYPE` | 302, 303 or 307 where a permanent move is meant | notice |
| `META_REFRESH_REDIRECT` | the move is implemented in markup, not in the response | warning |
| `HTTP_REFRESH_REDIRECT` | the move is implemented in a `Refresh` response header, not a real redirect status | warning |

Chains and loops are resolved as a second pass over the finished crawl's own records, so no URL
is fetched twice to establish them.

**4. Walk one chain by hand before you believe the count.**

```bash
seohead redirects-check --url https://example.com/old
```

```json
{"chain": [{"url": "https://example.com/old", "status": 404, "location": null, "ok": false}]}
```

Each hop comes back with its status and its `Location`, which is how a loop stops being a
number and becomes a specific pair of rules pointing at each other.

**5. Turn the flattened map into rules for the server you actually run.**

```bash
seohead redirects-generate --input '{"redirects": [{"from": "/Old-Page/", "to": "/old-page/"}]}' --format nginx
```

Apache rewrite rules, Apache `Redirect` lines, nginx and a custom template are the supported
forms. The point is that the source and the destination come from the crawl, not from somebody
retyping a spreadsheet.

**6. Report it.**

```bash
seohead report-build --audit ./run/audit.json --format xlsx --out ./redirects.xlsx
```

## What comes out

A loop list, a chain list with the hop count and final URL on each, and the list of internal
links that should simply be edited to point at the destination.

The last one is usually the whole job. On one live blog, 1450 of 3387 crawled URLs were 301s
and 1448 of those were a plain missing trailing slash: one template's link format, spending 42%
of the crawl budget.

That number also carries a warning. When that rate was first measured, part of it was the
crawler's own doing — the sitemap seeder normalised the trailing slash away before fetching, so
the tool created the redirects it then reported (issue #115). A redirect rate above a third of
the crawl is a reason to check the tool before writing the ticket.

## What it costs

No extra requests beyond the crawl: the chains are resolved from records already collected.
`redirects-check` is one request per hop for the URLs you verify by hand. Nothing paid.

## What it cannot answer

- **Whether a 302 should have been a 301.** A temporary redirect during a migration is correct.
  The check reports the type; the intent is yours.
- **Redirects performed by JavaScript.** A `location.assign` after load is a navigation, and
  the render mode reports the resulting DOM rather than the navigation that produced it.
- **A `Refresh` response header on an SF-export-only run.** `HTTP_REFRESH_REDIRECT` reads a
  native crawl's own captured response headers; an SF export carries no such column, so the
  check skips by name there rather than reading a page that used one as clean.
- **Where an off-host redirect ends.** A redirect that leaves the host is recorded and never
  followed: a crawl of your site must not quietly become a crawl of somebody else's.
- **Whether a loop happens for everyone.** Redirects driven by cookies, language or geography
  can be a loop for the crawler and invisible in your own browser.
