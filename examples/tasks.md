# Audit Tasks — example.com

> 75 of 155 checks could run; the score is not comparable to a run with full evidence

- Source: audit generated at 2026-09-06T13:35:28Z (health n/a)
- Tasks: **16** (P1: 3, P2: 8, P3: 5)

## P1 (3)

- [ ] **Internal link points to a 4xx URL — 1 page** `BROKEN_INTERNAL_LINK` · critical · effort: high · `TASK-92174247`
    - _How to fix:_ Update the link to the current URL or add an appropriate 301 redirect; if it appears in the footer or navigation, fix the shared template.
    - Broken links (destination ← source · position · XPath):
        - https://example.com/old-page (404) ← https://example.com/ · Content · `/html/body/main/article/p[3]/a`
        - https://example.com/old-page (404) ← https://example.com/page-a · Footer · `/html/body/footer/nav/a[2]`

- [ ] **Page returns a 4xx response (broken page) — 1 page** `BROKEN_PAGE_4XX` · critical · effort: high · `TASK-54ad8503`
    - _How to fix:_ Restore the page or redirect it with a 301 to a relevant URL; remove or update links that point to it.
        - https://example.com/old-page

- [ ] **Title element is missing — 1 page** `TITLE_MISSING` · critical · effort: high · `TASK-c5d3ee4f`
    - _How to fix:_ Add a unique, descriptive title element.
        - https://example.com/no-title

## P2 (8)

- [ ] **Duplicate meta description — 2 pages** `DESC_DUPLICATE` · warning · effort: medium · `TASK-45424e37`
    - _How to fix:_ Write a unique meta description for each page.
        - https://example.com/
        - https://example.com/page-b

- [ ] **Duplicate title element — 2 pages** `TITLE_DUPLICATE` · warning · effort: medium · `TASK-54b0732a`
    - _How to fix:_ Give each page a unique title element.
        - https://example.com/page-a
        - https://example.com/page-b

- [ ] **Indexable page has no canonical URL — 1 page** `CANONICAL_MISSING` · warning · effort: medium · `TASK-64efab10`
    - _How to fix:_ Add a valid <link rel="canonical"> element.
        - https://example.com/no-title

- [ ] **Meta description is missing — 1 page** `DESC_MISSING` · warning · effort: medium · `TASK-aa336983`
    - _How to fix:_ Add a useful meta description, typically up to about 160 characters.
        - https://example.com/no-title

- [ ] **Multiple H1 headings on the page — 1 page** `H1_MULTIPLE` · warning · effort: medium · `TASK-d54d97a7`
    - _How to fix:_ Keep one primary H1 and demote the remaining headings to H2 or H3 as appropriate.
        - https://example.com/page-a

- [ ] **HTML document is large in absolute terms or relative to the site — 1 page** `LARGE_HTML` · warning · effort: medium · `TASK-9e27800e`
    - _How to fix:_ Reduce HTML size by removing unnecessary markup, extracting inline styles or scripts, and avoiding embedded base64 assets.
        - https://example.com/no-title

- [ ] **Slow server response — 1 page** `SLOW_RESPONSE` · warning · effort: medium · `TASK-9bda9060`
    - _How to fix:_ Improve TTFB by profiling the application and origin, then optimizing caching and infrastructure.
        - https://example.com/no-title

- [ ] **Thin content (low word count) — 1 page** `THIN_CONTENT` · warning · effort: medium · `TASK-5c3d9251`
    - _How to fix:_ Add substantial, useful content or exclude the page from indexing when it has no standalone search value.
        - https://example.com/page-b

## P3 (5)

- [ ] **H2 is duplicated across multiple URLs — 3 pages** `H2_DUPLICATE` · notice · effort: low · `TASK-9ea5b337`
    - _How to fix:_ Use a unique, page-specific H2 on each URL, or accept it for a shared boilerplate subheading that is genuinely meant to repeat.
        - https://example.com/
        - https://example.com/no-title
        - https://example.com/page-a

- [ ] **Title falls below the configured length threshold — 2 pages** `TITLE_TOO_SHORT` · notice · effort: low · `TASK-f4d7444c`
    - _How to fix:_ Expand the title to an informative length without padding it with boilerplate.
        - https://example.com/page-a
        - https://example.com/page-b

- [ ] **Meta description falls below the configured length threshold — 1 page** `DESC_TOO_SHORT` · notice · effort: low · `TASK-6c37199b`
    - _How to fix:_ Expand the description with specific, useful page information.
        - https://example.com/page-a

- [ ] **HTML bloat: high document size relative to text content — 1 page** `HTML_BLOAT` · notice · effort: low · `TASK-69aebead`
    - _How to fix:_ Reduce bytes per word by extracting styles and scripts, removing embedded base64 assets, and simplifying markup.
        - https://example.com/page-b

- [ ] **Low text-to-HTML ratio — 1 page** `LOW_TEXT_RATIO` · notice · effort: low · `TASK-b37fa823`
    - _How to fix:_ Increase the proportion of meaningful visible content or reduce unnecessary markup.
        - https://example.com/page-b

