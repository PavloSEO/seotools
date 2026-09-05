# Scenario 51 — Infrastructure: what this site runs on, and whether it is safe

## The question

> We are taking over this site. What is it, where is it, and what is going to bite us?

## Covers

- **Security** — HTTP URLs · Missing HSTS Header · Missing Content-Security-Policy Header
- **Response Codes** — Internal Blocked by Robots.txt

## The chain

**1. What it is built with.**

```bash
seohead tech-detect --url https://example.com
```

CMS, framework, analytics, pixels, widgets, payment, fonts, CDN — from one request, by
signature over the HTML, headers, cookies and script sources.

**2. How it is delivered.**

```bash
seohead headers-check --url https://example.com
seohead cdn-check --url https://example.com
```

Compression, cache lifetimes, HTTP version, whether a CDN is in front and whether it is
actually caching rather than passing everything through.

**3. What it exposes.**

```bash
seohead security-check --url https://example.com
```

HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, with a
grade; plus version disclosure (`Server`, `X-Powered-By`) and cookie flags. From an SEO
standpoint these matter because a browser-blocked resource is a resource the crawler did not
get either.

**4. What it tells crawlers.**

```bash
seohead robots-check --url https://example.com
```

**5. Whether this one origin consolidates to a single address.**

```bash
seohead mirror-check --url https://example.com
```

`mirror-check` takes one `url` (or `{"url": ...}` through `--input`) and derives every variant
of *that* host itself — it does not compare two hostnames you supply. From the single origin it
builds the bare/www × HTTP/HTTPS matrix, plus `/index.php`/`/index.html`, trailing-slash and
case variants of the given path, and reports which ones 200 as live duplicates instead of
redirecting to one canonical address. A bare host answering separately from its `www` twin,
without a canonical between them, is the oldest duplicate-content problem there is, and it never
shows up in a single-host crawl. Comparing two genuinely different hostnames (a staging donor
domain against production, say) is not implemented — run `mirror-check` once per host and
compare the results yourself.

## What comes out

A stack description, a delivery profile, and a security grade — the three things that decide
whether the next month is about content or about plumbing.

## What it costs

A handful of requests total. `domain-profile` additionally performs RDAP/DNS lookups against
real infrastructure, so it needs the network to work and is the one step that cannot run
offline.

## What it cannot answer

- **Whether the server is configured correctly for load.** Signatures describe what answered
  one request, not capacity.
- **Anything from inside the server.** No filesystem, no config, no logs — unless you feed
  logs in yourself with `log-analyze`.
- **Whether a missing header is a decision.** No CSP on a static brochure site is a different
  problem from no CSP on a checkout.
