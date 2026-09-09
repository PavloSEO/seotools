# Scenario 47 — URL hygiene at scale: the shapes that quietly split a site

## The question

> Somebody said our URLs are "bad for SEO". Which ones, and does any of it matter enough to
> change?

URL shape is the easiest thing in technical SEO to have an opinion about and the easiest to
waste a quarter on. This chain turns the opinion into a counted list, so the argument is about
a hundred URLs with a repeated path segment rather than about style.

## Covers

- **URL** — Multiple Slashes · Contains A Space · Non ASCII Characters · Uppercase · Repetitive Path · Underscores · Over 115 Characters

## The chain

**1. Crawl once. Every URL check reads the record the crawl already made.**

```bash
seohead crawl-site --url https://example.com --config ./crawl.json --out-dir ./run
```

**2. Scan the run.**

```bash
seohead log-scan --run ./run
```

**3. Read the URL family in `summary.by_check`.**

| Check | What fires it |
|---|---|
| `URL_MULTIPLE_SLASHES` | `//` inside the path |
| `URL_CONTAINS_SPACE` | a literal space or `%20` |
| `URL_NON_ASCII` | a non-ASCII character in the decoded path |
| `URL_UPPERCASE` | the path is not equal to its own lowercase form |
| `URL_REPETITIVE_PATH` | a non-numeric path segment appears twice |
| `URL_UNDERSCORES` | an underscore in the path |
| `URL_TOO_LONG` | the URL is longer than the configured limit, 115 characters by default |

Two of those deserve their definitions read carefully. The repetitive-path rule ignores purely
numeric segments, so a dated permalink like `/2024/01/01/` and an id pair like `/catalog/12/12/`
are not reported — the pattern it is looking for is `/shop/shop/` or `/en/products/en/`, which
is a duplicated prefix or a crawl trap. And uppercase is judged on the path only, because the
host is case-insensitive and reporting it would be noise on every URL.

`URL_CONTAINS_SPACE` is a URL-hygiene heuristic, not proof that Google cannot index the URL.
[Google's URL structure guide](https://developers.google.com/search/docs/crawling-indexing/url-structure)
requires percent encoding where necessary. The check reads the full URL, so a functional query
such as `?q=red%20shoes` can trigger it even though removing `%20` would change the query value.
Encode literal spaces in links, but do not mechanically rewrite percent-encoded values. Change a
canonical path only after verifying equivalent content and planning the redirect/canonical route.

**4. Apply the dominance rule before you write anything down.** If one of these checks is above
roughly half of all findings, stop trusting it for this report, verify five of its hits by hand,
and file a bug. Notices scale with page count, and a notice on every page is a template
property, not a hundred separate problems.

**5. Export the list a developer can work from.**

```bash
seohead report-build --audit ./run/audit.json --format csv --out ./url-hygiene.csv
```

**6. If URLs do change, generate the redirects from the crawl rather than by hand.**

```bash
seohead redirects-generate --input '{"redirects": [{"from": "/Old-Page/", "to": "/old-page/"}]}' --format nginx
```

## What comes out

A counted list per shape, with the URLs. What it is worth is a separate judgement, and the
honest ordering is: fix what creates duplicate addresses for one page, ignore what is merely
untidy.

The case for taking that seriously is arithmetic, not taste. On one live blog, 1450 of 3387
crawled URLs were redirects, and 1448 of them differed from their destination by a single
trailing slash — one template emitting one variant, 42% of a crawl budget.

## What it costs

Nothing beyond the crawl. Every check here reads a URL string that was already recorded; not
one of them makes a request.

## What it cannot answer

- **Whether a URL should change at all.** A rename costs a redirect, a re-crawl and whatever
  the old URL had accumulated. "Untidy" is not a reason on its own.
- **Whether non-ASCII is wrong.** Localized paths in a non-Latin script are legitimate, and the
  check is a notice for exactly that reason: it flags a property, not a defect.
- **Which variant is canonical.** Two URLs serving one page is a canonical question, and the
  answer belongs in the markup before it belongs in a redirect rule.
- **URLs the crawl never reached.** Depth limits, budgets and blocked paths all leave URLs out
  of the record, so a clean report describes the URLs that were crawled.
- **How URLs read to a person.** Nothing here judges whether a path is comprehensible, which is
  usually the only thing that mattered.
