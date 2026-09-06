# SEO audit — example.com

- **Generated:** 2026-09-06T13:33:13Z
- **Input mode:** parse-exports  ·  **Profile:** full
- **Source:** examples/exports
- **Exports used:** internal_all, inlinks_4xx

## Health summary

**Health score: 2 / 100**

_75 of 149 checks could run; the score is not comparable to a run with full evidence_

- Checks: **16 fired**, 74 skipped, 59 silent, 0 disabled (of 149 total)

- URLs crawled: **6** (HTML: 4, indexable: 4)
- Total issues: **21**

| Severity | Count |
|---|---:|
| 🔴 Critical | 3 |
| 🟡 Warning | 10 |
| ⚪ Notice | 8 |

**Most frequent issues:**

| Check | Count | Severity |
|---|---:|---|
| `H2_DUPLICATE` | 3 | notice |
| `DESC_DUPLICATE` | 2 | warning |
| `TITLE_DUPLICATE` | 2 | warning |
| `TITLE_TOO_SHORT` | 2 | notice |
| `BROKEN_INTERNAL_LINK` | 1 | critical |
| `BROKEN_PAGE_4XX` | 1 | critical |
| `CANONICAL_MISSING` | 1 | warning |
| `DESC_MISSING` | 1 | warning |
| `DESC_TOO_SHORT` | 1 | notice |
| `H1_MULTIPLE` | 1 | warning |
| `HTML_BLOAT` | 1 | notice |
| `LARGE_HTML` | 1 | warning |

**HTML size:** median 76 KB, p90 229 KB, p95 261 KB, max 293 KB.

## Look at these before trusting the rest

Each check below describes more than half the crawled pages. That can be true -- a site really may have no meta description anywhere -- but it is also what a broken check looks like, and it is worth one minute of checking against the live site before the rest of this report is acted on.

| Check | Pages | Share of crawl |
|---|---:|---:|
| `H2_DUPLICATE` | 3 | 75% |

## 🔴 Critical (3)

### `BROKEN_INTERNAL_LINK` — Internal link points to a 4xx URL (1)

| Destination | Status | Source page | Anchor | Position | XPath |
|---|---:|---|---|---|---|
| https://example.com/old-page | 404 | https://example.com/ | Legacy Page | Content | `/html/body/main/article/p[3]/a` |
| https://example.com/old-page | 404 | https://example.com/page-a | view pump specifications | Footer | `/html/body/footer/nav/a[2]` |

> _How to fix:_ Update the link to the current URL or add an appropriate 301 redirect; if it appears in the footer or navigation, fix the shared template.

### `BROKEN_PAGE_4XX` — Page returns a 4xx response (broken page) (1)

| URL | Details |
|---|---|
| https://example.com/old-page | status=Not Found, inlinks=4 |

> _How to fix:_ Restore the page or redirect it with a 301 to a relevant URL; remove or update links that point to it.

### `TITLE_MISSING` — Title element is missing (1)

- https://example.com/no-title

> _How to fix:_ Add a unique, descriptive title element.

## 🟡 Warning (10)

### `CANONICAL_MISSING` — Indexable page has no canonical URL (1)

- https://example.com/no-title

> _How to fix:_ Add a valid <link rel="canonical"> element.

### `DESC_DUPLICATE` — Duplicate meta description (2)

- **"A sample description over seventy characters that reliably meets the configured audit threshold."** — 2 URLs:
    - https://example.com/
    - https://example.com/page-b

> _How to fix:_ Write a unique meta description for each page.

### `DESC_MISSING` — Meta description is missing (1)

- https://example.com/no-title

> _How to fix:_ Add a useful meta description, typically up to about 160 characters.

### `H1_MULTIPLE` — Multiple H1 headings on the page (1)

| URL | H1 text |
|---|---|
| https://example.com/page-a | Pump Models ⏐ Second H1 Heading |

> _How to fix:_ Keep one primary H1 and demote the remaining headings to H2 or H3 as appropriate.

### `LARGE_HTML` — HTML document is large in absolute terms or relative to the site (1)

| URL | Size | × median | Rank |
|---|---:|---:|---:|
| https://example.com/no-title | 293 KB | ×3.87 | 1 |

> _How to fix:_ Reduce HTML size by removing unnecessary markup, extracting inline styles or scripts, and avoiding embedded base64 assets.

### `SLOW_RESPONSE` — Slow server response (1)

| URL | Details |
|---|---|
| https://example.com/no-title | response_time=2.0, max_s=1.5 |

> _How to fix:_ Improve TTFB by profiling the application and origin, then optimizing caching and infrastructure.

### `THIN_CONTENT` — Thin content (low word count) (1)

| URL | Details |
|---|---|
| https://example.com/page-b | word_count=50, threshold=200 |

> _How to fix:_ Add substantial, useful content or exclude the page from indexing when it has no standalone search value.

### `TITLE_DUPLICATE` — Duplicate title element (2)

- **"Industrial Pumps Product A"** — 2 URLs:
    - https://example.com/page-a
    - https://example.com/page-b

> _How to fix:_ Give each page a unique title element.

## ⚪ Notice (8)

### `DESC_TOO_SHORT` — Meta description falls below the configured length threshold (1)

| URL | Details |
|---|---|
| https://example.com/page-a | length=7, min_chars=70 |

> _How to fix:_ Expand the description with specific, useful page information.

### `H2_DUPLICATE` — H2 is duplicated across multiple URLs (3)

| URL | Details |
|---|---|
| https://example.com/ | value=Section, duplicate_count=3 |
| https://example.com/no-title | value=Section, duplicate_count=3 |
| https://example.com/page-a | value=Section, duplicate_count=3 |

> _How to fix:_ Use a unique, page-specific H2 on each URL, or accept it for a shared boilerplate subheading that is genuinely meant to repeat.

### `HTML_BLOAT` — HTML bloat: high document size relative to text content (1)

| URL | Details |
|---|---|
| https://example.com/page-b | bytes_per_word=1600.0, site_median_bpw=375.0, word_count=50, size_bytes=80000 |

> _How to fix:_ Reduce bytes per word by extracting styles and scripts, removing embedded base64 assets, and simplifying markup.

### `LOW_TEXT_RATIO` — Low text-to-HTML ratio (1)

| URL | Details |
|---|---|
| https://example.com/page-b | text_ratio=8.0, threshold=10 |

> _How to fix:_ Increase the proportion of meaningful visible content or reduce unnecessary markup.

### `TITLE_TOO_SHORT` — Title falls below the configured length threshold (2)

| URL | Details |
|---|---|
| https://example.com/page-a | title=Industrial Pumps Product A, length=26, min_chars=30 |
| https://example.com/page-b | title=Industrial Pumps Product A, length=26, min_chars=30 |

> _How to fix:_ Expand the title to an informative length without padding it with boilerplate.

## Sitemap & robots

- Declared in robots.txt: **None**
- URLs in sitemap: **0**  ·  indexable URLs in crawl: **4**
- In sitemap but not in crawl: **0**  ·  in crawl but not in sitemap: **0**
- Non-200 URLs in sitemap: **0**  ·  non-indexable URLs in sitemap: **0**

## Appendix: skipped checks

| Check | Reason |
|---|---|
| `IMG_MISSING_ALT` | missing export: images_missing_alt |
| `IMG_OVER_KB` | missing export: images_over_kb |
| `IMG_MISSING_DIMENSIONS` | missing export: images_missing_size |
| `MIXED_CONTENT` | missing export: security_mixed |
| `MISSING_HSTS` | missing export: security_hsts |
| `STRUCTURED_DATA_MISSING` | missing export: structured_data_missing |
| `HREFLANG_ERROR` | missing export: hreflang |
| `HREFLANG_BROKEN_TARGET` | missing export: all_hreflang |
| `SITEMAP_URL_4XX_5XX` | missing export: sitemap_non_200 |
| `SITEMAP_URL_3XX` | missing export: sitemap_redirects |
| `SITEMAP_URL_NON_INDEXABLE` | missing export: sitemap_non_indexable |
| `DESC_MULTIPLE` | no meta description count evidence (native crawl only) |
| `H1_ALT_TEXT_ONLY` | no H1 alt-text evidence (native crawl only) |
| `CONTENT_IN_IFRAME` | no iframe inventory in this evidence |
| `SCHEMA_VALIDATION_ERROR` | no Structured Data validation columns in Internal:All |
| `STRUCTURED_DATA_PARSE_ERROR` | no JSON-LD found/parsed block counts (native crawl only) |
| `READABILITY_DIFFICULT` | no Readability/Flesch column |
| `LONG_SENTENCES` | no Average Words Per Sentence column |
| `SPELLING_ERRORS` | no Spelling Errors column (enable spell-check in SF) |
| `GRAMMAR_ERRORS` | no Grammar Errors column (enable grammar-check in SF) |
| `HTTP_REFRESH_REDIRECT` | no Refresh response header evidence (native crawl only) |
| `PAGINATION_LOOP` | no rel="next" column in Internal:All |
| `UNLINKED_PAGINATION_SERIES` | no rel="next" column in Internal:All |
| `LOREM_IPSUM_PLACEHOLDER` | no Lorem Ipsum evidence (native crawl only) |
| `UNSUPPORTED_PLUGIN` | no plugin-element evidence (native crawl only) |
| `IMG_MISSING_ALT_ATTRIBUTE` | no per-image evidence (native crawl only) |
| `IMG_ALT_TOO_LONG` | no per-image evidence (native crawl only) |
| `MISSING_CHARSET` | no Meta Charset column, so a page without a header charset cannot be distinguished from one declaring <meta charset> (needs a native seohead crawl or Custom Extraction in SF) |
| `MISSING_DOCTYPE` | no Doctype column (needs a native seohead crawl or Custom Extraction in SF) |
| `VIEWPORT_MISSING` | no Viewport column (needs a native seohead crawl or Custom Extraction in SF) |
| `NO_COMPRESSION` | no Content-Encoding column (needs a native seohead crawl or Custom Extraction in SF) |
| `TITLE_OUTSIDE_HEAD` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `DESC_OUTSIDE_HEAD` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `CANONICAL_OUTSIDE_HEAD` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `DIRECTIVES_OUTSIDE_HEAD` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `HREFLANG_OUTSIDE_HEAD` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `HEAD_MISSING` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `HEAD_MULTIPLE` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `BODY_MISSING` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `BODY_MULTIPLE` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `INVALID_HEAD_ELEMENT` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `HEAD_NOT_FIRST` | no element-position evidence (needs a native seohead crawl; Screaming Frog has no notion of this on its own) |
| `OG_MISSING` | no Open Graph columns in Internal:All (enable OG extraction in SF) |
| `REDIRECT_CHAIN` | no redirect data (Internal:All has no Redirect URL column) |
| `REDIRECT_LOOP` | no redirect data (Internal:All has no Redirect URL column) |
| `TITLE_MULTIPLE` | no titles_multiple export (export this SF filter to enable) |
| `LINK_TO_5XX` | export inlinks_5xx not available |
| `BROKEN_EXTERNAL_LINK` | export inlinks_5xx not available |
| `INTERNAL_LINK_TO_REDIRECT` | export inlinks_3xx not available |
| `EXTERNAL_LINK_TO_REDIRECT` | export inlinks_3xx not available |
| `HREFLANG_INVALID_CODE` | no all_hreflang export (export Bulk Export -> Links -> All Hreflang) |
| `HREFLANG_MULTIPLE_ENTRIES` | no all_hreflang export (export Bulk Export -> Links -> All Hreflang) |
| `HREFLANG_MISSING_SELF_REFERENCE` | no all_hreflang export (export Bulk Export -> Links -> All Hreflang) |
| `HREFLANG_MISSING_XDEFAULT` | no all_hreflang export (export Bulk Export -> Links -> All Hreflang) |
| `HREFLANG_NOT_CANONICAL` | no all_hreflang export (export Bulk Export -> Links -> All Hreflang) |
| `HREFLANG_MISSING_RETURN_LINK` | no all_hreflang export (export Bulk Export -> Links -> All Hreflang) |
| `LOW_LINK_SCORE` | no all_inlinks export (needed for the complete internal edge list) |
| `ONLY_NOFOLLOW_INLINKS` | no all_inlinks export (needed for the complete inlink list) |
| `ONLY_NONINDEXABLE_SOURCE_INLINKS` | no all_inlinks export (needed for the complete inlink list) |
| `DEEP_DISCOVERY_PATH` | no all_inlinks export (needed for the complete internal edge list) |
| `INSECURE_SUBRESOURCE` | no all_inlinks export (needed for the resource inventory) |
| `DOM_TOO_DEEP` | no stored HTML (input.html_store_dir not set) |
| `DOM_TOO_MANY_NODES` | no stored HTML (input.html_store_dir not set) |
| `DUPLICATE_BY_HASH` | SF native Hash column already covers this |
| `NEAR_DUPLICATE` | no stored HTML (input.html_store_dir not set) |
| `TITLE_TEMPLATED` | too few titles to assess templating |
| `SITEMAP_NOT_IN_ROBOTS` | no sitemap URL to check (no export, no --sitemap, and live_recheck disabled) |
| `ROBOTS_BLOCKS_RESOURCES` | no sitemap URL to check (no export, no --sitemap, and live_recheck disabled) |
| `SITEMAP_FETCH_INCOMPLETE` | no sitemap URL to check (no export, no --sitemap, and live_recheck disabled) |
| `SITEMAP_TOO_MANY_URLS` | no sitemap document was fetched to measure |
| `SITEMAP_TOO_LARGE` | no sitemap document was fetched to measure |
| `SITEMAP_URL_DUPLICATED` | no sitemap entries were fetched to compare |
| `SITEMAP_STALE_LASTMOD` | no sitemap entries were fetched to compare |
| `SITEMAP_DESYNC` | no sitemap URL set (no export and network disabled) |

