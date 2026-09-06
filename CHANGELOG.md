# Changelog

All notable public changes are documented here.

## Unreleased

- Close the three pagination rows #385 left open, taking the registry from 149 to 152 checks.
  `PAGINATION_MULTIPLE` and `PAGINATION_URL_NOT_IN_ANCHOR` read the All Inlinks export, which
  is the only place a page's complete `rel="next"`/`rel="prev"` declaration list exists --
  `Internal:All` keeps the first of each and drops the rest -- and report a page that declares
  two different successors, or one whose declared URL is not also an anchor on the same page.
  `PAGINATION_SEQUENCE_ERROR` reports a break in a page-number run the series otherwise
  follows, per the issue's own caveat: a series may start at a number other than one, a stride
  is not a break, and a series whose URLs do not state a page number is declared unevaluated
  rather than judged against a numbering that would have had to be invented.

- Fix four export-selection and run-validation defects that let an audit present a result it
  could not support (#209, #210, #215, #216). `internal_all`'s matcher required only the
  filename token `internal`, so a partial per-type Internal tab (e.g. `internal_html.csv`,
  missing every non-HTML row) satisfied the required Internal:All master export with no
  missing-master warning; the matcher now also requires `all`. `discover_exports` picked the
  first candidate for a logical key in sorted filename order when two files matched it (e.g.
  `internal_all.csv` and `internal-all.csv` both present with different rows), silently
  discarding the other with nothing in run metadata to say a choice was made; it now raises,
  naming every candidate. `run_sf` picked the newest timestamped subfolder under `--out`
  regardless of when it was created, so a re-run that exited 0 without writing anything (a
  startup failure Screaming Frog does not report as a nonzero exit) could return a prior
  invocation's exports as if they were fresh; it now compares against a snapshot taken before
  the process starts and fails loudly when nothing new appears. `build_command` added
  `--auth-config` only when the given profile file already existed, silently starting an
  unauthenticated crawl on a typo'd or deleted path — appropriate for the optional
  `seospiderconfig` default, wrong for a profile the caller explicitly requested; a missing
  explicit `sf_cli.auth_config` now raises before Screaming Frog starts.
- Fix two content-extraction defects in `seohead/tools/parser.py` (#138, #140). `collapse_whitespace`
  decoded HTML entities a second time on top of the single decode BeautifulSoup's lxml parser
  already performs on every `tag.get_text()`/`tag.get(attr)` value it hands to that helper — a
  silent no-op on ordinary markup, but on a page whose CMS or import pipeline already
  double-escaped its entities (a real, common artifact) it turned visibly-broken entity soup into
  clean-looking text and shortened the reported length below what a browser tab or a SERP snippet
  actually renders, flipping length-based title/description checks in both directions. The helper
  no longer decodes at all. Separately, link and URL-source extraction walked the whole document
  unconditionally, so an `<a>`/`<img>` that existed only inside an inert `<template>` — never part
  of the rendered document per the HTML spec, never requested by a browser or a crawler unless a
  script clones it in — was reported as a real, on-page link and actually fetched by `spider.py`,
  the same phantom-URL failure mode closed for `<base href>` in #4; both extractors now skip
  `<template>` descendants. `<noscript>` is deliberately left reachable there, unlike in text
  extraction: it is real, spec-defined fallback markup a JS-disabled client (and search engines'
  non-rendering crawl pass) does load, and excluding it would hide a genuinely fetchable URL from
  the auditor. Word count also no longer counts `<svg>`/`<math>` descendant text (an icon sprite's
  accessibility `<title>`, glyph `<text>`, MathML notation) as body copy — a 20-icon header could
  double a page's reported word count against its real content — and that exclusion list now lives
  in one place (`content_area.TEXT_EXCLUDED_TAGS`) shared by both text extractors instead of two
  copies that had already drifted out of sync.
- Keep a link's full `rel` tokens, its `target` attribute, and its raw (pre-resolution) href,
  and extract `<form>` elements — method, action, whether a password field is present (#125).
  Six catalogued issues depended on those three facts being discarded: unsafe cross-origin
  links (`target="_blank"` without `rel="noopener"`/`"noreferrer"`), protocol-relative links
  (`//host/path`, before resolution), outlinks to localhost, a page receiving both a followed
  and a nofollow internal link, an insecure form action, and a password form served from a
  plain-HTTP page. All six are now `UNSAFE_CROSS_ORIGIN_LINK`, `PROTOCOL_RELATIVE_LINK`,
  `OUTLINK_TO_LOCALHOST`, `FOLLOW_AND_NOFOLLOW_INLINKS`, `FORM_URL_INSECURE` and
  `FORM_ON_HTTP_URL` in the registry. The form/localhost/nofollow-mix checks need only fields
  every crawl already recorded, so those four run unconditionally; the cross-origin and
  protocol-relative pair need `link_attributes.capture`, off by default,
  because the extra per-edge data measured roughly +50% on a synthetic 3387-page,
  150-link-per-page crawl — about +95 bytes/edge, +46 MiB total, `raw_href` alone accounting
  for most of it. Registry grows from 121 to 127 checks.
- Fix `report-build` silently rendering a zero-findings report for an SF Analyzer
  `audit.json` (#151). The documented recipe — `sf run --tasks` piped into
  `report-build --format docx`/`xlsx`/`csv`/`md` — read `findings`/flat page keys, which
  the SF schema does not use (findings live under `issues`, page facts under
  `pages[].metrics`); the mismatch produced a confident `0/0/0/0` summary for an audit
  that found real critical issues, with no findings sections at all. `build_report` now
  recognizes both the native `seohead.site-audit/1` shape and the SF Analyzer shape,
  normalizing the latter into the flat contract the four human-facing writers already
  understand before rendering. `--format json` is untouched — it already relayed the
  original document correctly, which is what proved the data was never missing. A
  document matching neither contract is refused with `ok: False` naming the schema
  mismatch instead of being rendered as an empty deliverable, and the dead
  `if ... : pass` conditional that looked like this validation but always no-opped is
  gone. `tests/test_docs_commands_execute.py` now asserts the documented recipe's
  rendered summary against `audit.json`'s own totals, not just its exit code.
- Close two money-safety gaps in `seohead/data_sources/` (#157, #159). `geo_guard` checked only
  the advisory `country` string, so `search_volume`/`keyword_ideas`/`keyword_difficulty`/`serp`
  could still reach DataForSEO's live endpoint for `location_code=2643` (Russia) or `2112`
  (Belarus) whenever a caller supplied the numeric geo-target without also filling in `country`;
  the guard now checks `location_code` first, since that is the field actually billed on. Separately,
  a network-level exception (`URLError`/`TimeoutError`/`SSLError`) during a billed call — DataForSEO's
  `post`, Arsenkin's `/set`, and Yandex Cloud's `wordstat.topRequests`/`wordstat.dynamics`/
  `web.searchAsync` — used to retry the identical payload with no idempotency key and log only the
  attempt that finally returned a response. None of the three providers offers an idempotency
  mechanism for these endpoints, so a lost response is no longer retried: the attempt is recorded
  in the spend ledger (cost unknown, flagged `attempt_failed: network_error`) and the call fails
  outright instead of risking a second charge. Idempotent reads (Arsenkin `/check`/`/get`, Yandex
  Cloud operation polling) are unaffected and keep retrying. `yandex_cloud.WebSearch.search_batch`
  isolates a lost submission to its own query instead of aborting the batch. `metrika.py` retries
  network errors the same way but is not billed money and was left unchanged; flagged for a
  follow-up if its Logs API export creation should get the same treatment for quota reasons.
- Close a DNS-rebinding gap in `http_client()` (#142): `pinned_target()`, the fix for the
  TOCTOU window described in #14, protected only `collect.py`'s list-mode fetch — the
  other fourteen call sites, including `spider.py`'s `crawl-site` engine, let httpx resolve
  DNS a second, independent time to open the socket, so a hostile resolver answering that
  second lookup differently than the guard's own reached `169.254.169.254` or any RFC 1918
  address regardless of the guard's verdict. `http_client()` now builds every client on a
  transport that pins the connection to the address it resolves itself, on the first
  request and on every redirect hop, keeping the hostname only for the `Host` header and
  TLS SNI — so the fix is structural rather than a discipline every caller had to remember.
  `SEOHEAD_ALLOW_PRIVATE_NETWORKS`/`SEOHEAD_ALLOW_PRIVATE_HOSTS` are unchanged.
- Fix the CLI/MCP exit-code contract for a handler's own-reported failure, and complete
  `SOURCE_FLAGS` (#155, #156). A handler returning `{"ok": false, ...}` — the tool layer's
  documented way of reporting a fetch, parse, or provider failure without raising — used to
  print that JSON and exit 0 on the CLI and return a normal (non-`isError`) result over MCP,
  so a pipeline gating on `$?` or a client checking `isError` alone could not detect it.
  `cli.py` now exits 1 for that case (`log-scan`'s own exit 2 for a self-contradicting run
  stays a separate, documented signal — see `docs/USAGE.md`), and `mcp_server.py` raises
  `ToolError` from a shared `_checked()` wrapper so a client sees `isError` instead. Both call
  a single `handlers.handler_failed()` so the two interfaces cannot drift on what counts as a
  failure. Separately, `SOURCE_FLAGS` gained `--phrase`, `--keywords`, `--query`/`--queries`,
  `--seed`, `--counter`, and `--before`/`--after` — each already identifies a command's whole
  input the way `--url` does, but was missing, so a per-line loop over any of them silently
  processed only its first line. The set is now built by `_source_flag()` at the point each
  flag is declared instead of hand-listed separately, so it cannot drift out of sync again.
- Expand `docs/scenarios/` from ten chains to fifty-six, grouped by the question a reader
  arrives with (#120). Which scenarios exist is decided by the coverage map rather than by
  taste: each declares the catalogued issues it resolves, and
  `tests/test_scenario_coverage.py` asserts that every issue this toolkit claims to find
  appears in at least one, that no scenario names an issue the map does not list, and that
  none omits its limits. All 143 findable issues are covered. Every command in every file
  is still executed against the fixture site in CI — the suite runs 2008 tests, of which
  the doc-command gate is now the largest single group.
- Assert the sitemap protocol's own limits (#124): `SITEMAP_TOO_MANY_URLS` above 50,000
  entries in one file, `SITEMAP_TOO_LARGE` above 50 MB uncompressed, and
  `SITEMAP_URL_DUPLICATED` for a URL declared in more than one sitemap, naming both
  documents. A file over either limit is invalid rather than merely large — a search
  engine may read part of it and discard the rest with nothing the site owner can see —
  and the thresholds come from the protocol, not from config. Both parsers now record the
  per-document byte size and declared count, and the findings name the child sitemap
  rather than the index. Registry grows from 118 to 121 checks.
- Add `docs/GUIDELINE.md`, the document a person reads first (#121): what this is and is
  not, a first run end to end, how to choose a crawl rate and how to tell whether the
  errors are yours, how to read `audit.json` without being misled by it, what the config
  file changes and what it only costs, the six mistakes everybody makes first, what to do
  when the tool is wrong, and what it cannot answer at all. Linked first from the README,
  ahead of the reference.
- Add `docs/COVERAGE_SF_ISSUES.md`, generated from `seohead/sf/core/sf_issue_map.py`:
  every one of the 320 issues in the field's published catalogue, each with exactly one
  status — found by a named check, found by a named command, found in part with the
  missing part stated, a named gap, or a decision with its reason (#119). Of the 212
  in-scope issues, 126 are found today and 17 in part; 41 are gaps and 28 need something
  deliberately not built. Accessibility (92) and AMP (16) are declined as single
  decisions, with their full lists shown so the decision is auditable. A test asserts
  every referenced check id and command still exists, so a rename breaks the build rather
  than the document. Supersedes `docs/CHECKLIST_AUDIT.md`, which was written without the
  catalogue in hand.
- The sitemap seeder requests the address the sitemap published, not its normalised form
  (#115). `sitemap.crawl()` returned only the normalised `loc`, so a sitemap declaring
  `/a/` caused a fetch of `/a` — on most CMSes a 301 the crawler invented and then
  reported as a fact about the site, and a 404 wherever the slashless form is not routed
  at all. Each entry now carries `loc` as published alongside `loc_normalized` for the
  consumers that compare on it. Redirect statistics from earlier sitemap-seeded crawls
  are suspect for this reason.
- Restructure the operator skill into a controller directory (#111). `.claude/skills/`
  now has two tiers: 21 method skills, each covering one thing well, and `control/`,
  which decides which of them to run on a site nobody has looked at yet. The controller
  routes rather than restating, and carries five loadable sub-skills (scoping, rate and
  load, reading an audit, verifying, deliverables) and a three-file reference archive
  (defects found on live sites and what gave each away, which population each check
  describes, and what the toolkit cannot answer at all). The English-only gate and the
  doc-command gate now cover every Markdown file under a skill directory, not only
  `SKILL.md`.
- Add `tests/chains/`: a fixture site built out of the shapes that actually break chains —
  both slash forms of one URL, a body that is not valid UTF-8, a windows-1251 page, a
  masthead outside `<main>`, an off-host link, a robots-disallowed path — crawled over
  loopback, with seventeen assertions about the run as a whole rather than about any one
  module (#112). Four properties: conservation (a number does not change meaning as it
  travels), population (a finding is about a member of the set it describes),
  determinism (two crawls, two concurrency levels, one answer) and representation (a page
  says how it was measured). The population rules are `logscan`'s own, so a contradiction
  the scanner can name is a chain test that asserts it.
- `reconcile_sitemap` reports each URL as it was written rather than as it was normalised.
  Comparison still happens on the normalised key, but a finding that named a normalised
  form named a URL appearing nowhere in the crawl — unactionable for a reader, and
  indistinguishable to the anomaly scanner from a finding about a page never fetched.
- Add `docs/scenarios/`: ten end-to-end chains, each with the real commands in order, the
  artifact that comes out, what it costs, and what that chain cannot answer (#110). The
  rest of the documentation lists what the toolkit has; this describes what it does. Linked
  from the README above the tool list, so an agent evaluating the repository finds the
  chains before the inventory. Every command shown is executed against the fixture site by
  `tests/test_docs_commands_execute.py`, whose extractor and whose English-only and count
  gates now walk `docs/` at every level rather than only its top.
- Add `log-scan` (CLI) and `seo_log_scan` (MCP): read a finished run's own artifacts and
  report claims that cannot all be true at once (#109). Eight rules, each written from a
  defect that shipped past the whole test suite — a recorded size that disagrees with the
  file on disk, a text ratio over 100%, a check firing more often than there are pages to
  fire on, a finding about a URL the run never fetched, a canonical called a redirect while
  that URL answered 2xx in the same run, a summary that disagrees with its own rows, words
  counted on a zero-byte page, and pages measured two ways where only some say which. Each
  anomaly names both values and where each was read from. The CLI exits 2 when a run
  contradicts itself, so a pipeline stops instead of publishing the numbers.
- The cross-worker pacing test no longer measures the wall clock (#107). `_DispatchGate`
  now reads the crawl's injected clock instead of `time.monotonic()` directly, so the test
  drives it with a virtual clock that advances only when something sleeps: the dispatch
  instants are the crawler's own arithmetic and the assertion is exact. The old form
  compared real elapsed gaps against a 0.024s floor and failed on unchanged code whenever
  the machine was busy.
- Close the second half of the unwired-settings audit (#91). `http.headers` is merged into
  every request beside the credential headers; `speed.adaptive` gates the throttle's delay
  and concurrency adjustment (the timeout and server-error counters keep running — giving
  up is a separate mechanism from backing off); `discovery.hyperlinks.store` / `.crawl`,
  `discovery.external.store` and `discovery.redirects.crawl` now decide what the crawl
  records and what it follows. `discovery.canonicals.*`, `discovery.external.crawl` and
  `discovery.redirects.store` are removed rather than wired: they named capability the
  spider does not have (canonical-chasing, cross-host crawling) or state it cannot
  withhold, and a setting that appears in `--config-help` and the run manifest while
  changing nothing is worse than no setting. The coverage canary's exemption set is now
  empty: every `DEFAULTS` path changes an observable outcome and is named by a test.
- Detect the content area from the document's own semantics when nothing is configured
  (#96): `main`, then `[role="main"]`, then `article`, recording which one matched as
  `auto_main` / `auto_role_main` / `auto_article`. The previous default — the whole body
  minus the `nav` and `footer` tags — counted 126 template words out of 433 on a live
  WordPress post (29%), including a skip-to-content link, and that inflation feeds
  `THIN_CONTENT` and `LOW_TEXT_RATIO` in the same direction on every page of a template.
  `header` and `aside` join `nav` and `footer` in `DEFAULT_EXCLUDE_TAGS` for the fallback
  path. A configured selector still wins, and one that matches nothing still falls back to
  `fallback_default_body` rather than silently auto-detecting a different region.
- `URL_NOT_IN_SITEMAP` now compares pages with pages (#94). It compared a sitemap's URLs
  against every destination in the crawl's link graph, so on a live 124-page site it fired
  392 times — 362 image files a gallery links to directly, five off-host links, and 30 URLs
  the crawl never fetched — which was 74% of that report and buried the findings that were
  real. The observed side is now the pages a sitemap is supposed to declare: fetched, 2xx,
  HTML, same-host and indexable. `reconcile_sitemap` takes that population as a separate
  `comparable` argument, so `SITEMAP_ORPHAN` keeps asking about reachability against every
  link destination and cannot invent orphans; what was set aside is returned under
  `linked_not_comparable` rather than dropped.
- Fix the canonical checks on a site that serves both slash forms of a URL (#95).
  `norm_url` folds a trailing slash away on purpose, so a canonical written without one
  matches the page that has it — but the normalised index kept only one page per key, and
  a crawl of a typical WordPress site holds two: `/x` (301) and `/x/` (200). Reading
  whichever was inserted first made `CANONICAL_TO_REDIRECT` report 78 live pages whose
  canonical answers 200. `AuditContext` now exposes `pages_by_norm` with every page under
  a key, `page_by_norm` returns the variant that answered 2xx, and `CANONICAL_TO_REDIRECT`
  and `CANONICAL_NON_INDEXABLE` only fire when no variant contradicts them.
- Fix `size_bytes`: it is now the response body as it arrived on the wire, measured before
  the body is decoded (#99). It was measured from the decoded string, so every byte that is
  not valid UTF-8 became U+FFFD and re-encoded to three — a 739 KB WebP from a real crawl
  was recorded as 1.27 MB, and the inflation factor differs per file. Images, PDFs, fonts
  and HTML served in a legacy charset (windows-1251) were all over-counted, and so was the
  text ratio computed against that denominator. The HTTP cache stores the wire size with
  the entry, so a replayed page reports what the live fetch reported; its schema moves to
  `http_cache.v2` and v1 entries are re-fetched once rather than replayed without a size.
- Add `docs/TOOL_REFERENCE.md`, generated from the MCP tool definitions
  (`seohead/servers/tool_reference.py`, `scripts/generate_tool_reference.py`): every
  `seo_*`/`sf_*` tool's arguments with type and default, its cost (network/writes/
  idempotent/spend, read from its `ToolAnnotations` profile), and its own docstring's
  behavior and failure-mode notes. `tests/test_docs_drift.py` fails the build if it
  drifts from the tool definitions or is missing a tool.
- Add `tests/test_docs_commands_execute.py`: extracts every `seohead ...` command
  shown in README/docs/skills/examples (`scripts/doc_commands.py`) and runs each one
  offline, against a loopback fixture site (`tests/doc_fixtures/`) and materialized
  copies of `examples/`, asserting a clean exit. Commands that need real
  infrastructure (RDAP/DNS, a licensed SF binary, a paid provider credential, the
  never-returning `mcp` server) are at least parsed against the live argument parser.
  A documented command that no longer works now fails CI instead of shipping stale.
- Reshape all 21 technical workflow skills (`.claude/skills/*/SKILL.md`) into a
  shared shape: Trigger, Anti-trigger, Preconditions (as a checklist), the existing
  Workflow, Decision points, Definition of done (as a checklist), and Cost. Fix the
  stale command-coverage count at the bottom of `docs/SKILLS.md` (the real count,
  recomputed by scanning every skill file, is asserted by a new drift test).
- Fix several tool/test counts that had silently drifted from the real registries
  (a stale CLI command count in `docs/USAGE.md`/`docs/COMPARISON.md`/`README.md`/
  `docs/TESTING.md`, a stale MCP tool count in `docs/TOOLS.md`, a stale tool-reference
  count in `docs/README.md`, and the offline test count) and pin the fixed ones with
  a regression test (`test_stale_tool_counts_do_not_reappear`).

- Add the four static Lighthouse audits that need no browser and no third-party API
  (#59): `MISSING_CHARSET`, `MISSING_DOCTYPE`, `VIEWPORT_MISSING` and `NO_COMPRESSION`,
  each computed from evidence a crawl already holds. `content_encoding`,
  `meta_charset`, `doctype` and `viewport` join the normalized column vocabulary, so an
  SF export that happens to carry them as Custom Extraction columns feeds the same
  checks. Registry grows from 114 to 118 checks.
- Wire the ten remaining crawler settings that were validated, written into the run manifest, and
  described by `--config-help` but read by nothing (#63): `limits.max_response_bytes`,
  `speed.max_delay_seconds`, `robots.user_agent_token`, and `speed.stop_after_consecutive_timeouts`
  now configure behaviour that was previously hardcoded; `robots.unavailable_means_stop` now
  governs whether an unreachable or 5xx robots.txt stops the crawl or is treated as unrestricted
  (previously an unreachable robots.txt never stopped the crawl regardless of policy, while a 5xx
  one always did — now both are the same "unavailable" case, gated by the setting);
  `limits.max_url_length`, `limits.max_query_variants_per_path`, `http.retry_on_timeout`, and
  `discovery.follow_nofollow` are newly-implemented behaviour. `http.user_agent` is now applied to
  real requests instead of always sending the toolkit's default. Add `crawl-describe-settings`
  (CLI) and `seo_crawl_describe_settings` (MCP) so an agent can discover the configuration surface
  without a filesystem (#23).
- Add custom search (`tools/custom_search.py`) and custom extraction
  (`tools/custom_extract.py`) over an already-crawled corpus: presence/absence filters
  (raw source, visible text, a named CSS element, or an XPath node) and CSS/XPath/regex
  extractors, both reporting which representation (static markup vs. rendered DOM) they ran
  against. Absence is counted honestly: a page whose fetch failed is excluded from both the
  numerator and the denominator rather than counted as missing. Extraction runs each
  (document, extractor) pair under a wall-clock budget (`SIGALRM` on POSIX): a pathological
  expression aborts only that document, and the run still finishes.
- Add link position classification (`tools/link_position.py`): nav/header/sidebar/footer/content,
  by ordered rule over a link's ancestor path, reusing `content_area.py`'s notion of content
  rather than inventing a second one. Wired into `crawl/spider.py`'s link recording behind
  `link_position.classify` (default off — a position per link costs memory on a large crawl) and
  aggregated site-wide by `crawl/linkgraph.py`'s `inlink_composition`, which now feeds a new
  `INLINK_BOILERPLATE_ONLY` audit finding for pages linked only from boilerplate. Registry grows
  from 104 to 105 checks.
- Add eight post-crawl second-pass computations that only become answerable once a crawl is
  complete (issue #15): an internal link score computed from the `all_inlinks` edge graph
  (`LOW_LINK_SCORE`); a canonical target no hyperlink ever points to (`UNLINKED_CANONICAL`);
  `rel="next"` loop and unlinked-series detection (`PAGINATION_LOOP`,
  `UNLINKED_PAGINATION_SERIES`); hreflang reciprocity (`HREFLANG_MISSING_RETURN_LINK`);
  inlink-composition aggregates (`ONLY_NOFOLLOW_INLINKS`, `ONLY_NONINDEXABLE_SOURCE_INLINKS`);
  the concrete shortest discovery path from the crawl seed (`DEEP_DISCOVERY_PATH`); a
  self-computed mixed-content fallback (`INSECURE_SUBRESOURCE`); and near-duplicate clustering
  from stored page text (`NEAR_DUPLICATE`/`DUPLICATE_BY_HASH`), wiring `tools/duplicate.py` and
  `tools/content_area.py` into the audit for the first time. `ORPHAN_PAGE`, `SITEMAP_ORPHAN` and
  the two new "unlinked" checks are now withheld — reported as a named skip, not a finding — on a
  crawl the aggregator has marked partial, since "nothing links here" is unprovable on a
  truncated crawl. Registry grows from 104 to 114 checks.
- Audit the crawl registry against an external technical-SEO checklist and close eight cheap,
  verified gaps: five hreflang checks (invalid language/region codes, missing self-reference,
  missing x-default, duplicate entries, non-canonical targets), two robots directives
  (`notranslate`, `unavailable_after`), and canonical URLs containing a fragment. Registry grows
  from 96 to 104 checks. See `docs/CHECKLIST_AUDIT.md`.
- Add `asset-weight-check`: fetches a page's linked CSS/JS and reports
  render-blocking resources, oversized files, duplicate libraries (by content
  hash), missing minification, missing `font-display`, legacy polyfilled JS,
  and missing compression/long-lived caching.
- Add `crawl-site --sitemap <url>` (and `sitemaps.auto_discover` in `--config`): seed the native
  crawler from a sitemap's declared URLs, follow links from each, and reconcile the declared and
  observed sets into `audit.json`'s `summary.sitemap`, under the same `SITEMAP_ORPHAN` /
  `URL_NOT_IN_SITEMAP` check ids the Screaming Frog pipeline already reports.
- Add `crawl-site --config-help`, generated from `seohead/crawl/config.py`, and hide `--max-depth`
  and `--min-delay` from `--help` (still accepted) so the flag surface stops growing with every
  crawler setting.
- Split technology fingerprinting into a fetch step and a pure `analyze_tech` step,
  capture analytics/tag-manager ids instead of only names, and add a `tag_coverage`
  report that groups presence by URL template and stamps how each page was measured.
- Resolve redirect chains and loops as a second pass over a finished crawl's own
  redirect targets, so `REDIRECT_CHAIN`/`REDIRECT_LOOP` no longer require the native
  Screaming Frog Redirect Chains report — a light-profile export or a `crawl-site` run gets
  the same findings for free.
- Add a configurable content area (`content_area.py`) that scopes word count to
  the main region, excluding navigation and footer by default, without
  affecting link discovery; the resolved strategy is reported per page.
- Separate exact from near duplicates in `duplicate.py`: exact matches are
  hashed from extracted text (not raw bytes) and excluded from near-duplicate
  clusters, and comparisons default to indexable pages only.
- Add a boilerplate-consistency report (`boilerplate_report.py`) that hashes
  header/nav/footer per page and flags minority template groups.
- Add dependency-free Markdown extraction (`markdown_extract.py`): a
  content-area-only rendering and a full-document one.
- Wire `markdown_extract` and `boilerplate_report` into the CLI and MCP surface as
  `markdown-extract`/`seo_markdown_extract` and `boilerplate-report`/`seo_boilerplate_report`
  (47 core tools, up from 45), and add the `only_indexable` flag `duplicate_check` already had
  at the handler layer to `seo_duplicate_check`'s MCP signature, where it had been missed.
  Rescope `citability-check`'s URL path from the parser's whole-document `text` field (a single
  collapsed line with no paragraph or heading breaks at all) to `markdown_extract`'s content-area
  Markdown, fixing both the boilerplate dilution the issue raised and a latent bug where the flat
  text silently zeroed the Answer-Blocks and Structure-Quality dimensions for every live URL.
  Left unscoped, deliberately: the parser's `text` field itself, still whole-document, because
  `page_facts.py`'s schema-evidence extraction (`sameAs` social links, breadcrumbs, price/rating
  regexes) depends on facts that legitimately live in header/footer widgets the content area
  excludes; and the Screaming-Frog-driven `THIN_CONTENT`/`LOW_TEXT_RATIO` checks in
  `sf/core/rules.py`, whose `word_count`/`text_ratio` come from Screaming Frog's own export
  columns — third-party data the toolkit has no raw HTML to rescope without re-fetching every
  page, defeating the zero-request offline-corpus design of the SF audit path. The toolkit's own
  crawler (`crawl-site`) already inherits the content-area scoping for free, since its word count
  reads straight from `parser.parse_html`.
- Write down a naming convention (`docs/NAMING.md`) and resolve the module-basename collisions
  and process-named test files it found; no CLI command, handler, or MCP tool name changed.
- Add community, citation, and no-key agent onboarding files.
- Add the permissioned `analytics-console-review` workflow skill and three practical recipes.
- Document support for the current `3.x` security line.
- Require TLS 1.2 or newer and pin direct certificate probes to prevalidated public addresses.
- Add an HTTP response cache for `crawl-site` (`seohead/crawl/cache.py`, opt-in via
  `cache.mode` — default `off`, so no side effect appears behind a default): real HTTP freshness
  semantics (`max-age`/`Expires`, `ETag`/`Last-Modified` revalidation, `Vary`-aware variants,
  `no-store`/`no-cache` honoured), a `replay` mode for debugging that is stamped in the manifest
  and never the default, and an `invalidate` flag for an explicit hard refresh. Every fetched
  page carries `cache_status` (`hit`/`revalidated`/`miss`); the run carries `cache_stats` and
  `cache_replay` in both the handler output and `audit.json`'s `run` block, so a report built
  partly from cache says so. A cache hit costs no request and consumes no throttle delay or
  concurrent dispatch-gate slot either — the wait is issued from inside `fetch_one` itself, only
  once a real network round trip is actually about to happen.
- Add journal-driven reuse to `seohead/runlog.py` (`SEOHEAD_REUSE_POLICY`, a per-tool maximum
  age in seconds; default empty, meaning nothing is ever reused). A configured, still-fresh,
  successful prior answer is returned instead of calling the tool again, marked `reused: true`
  with `reused_from_ts` in both the result and the new journal entry it still writes — freshness
  is always measured against when the value was actually fetched, never extended by reuse itself.

## 3.0.0 — first public snapshot

- One Python package with 42 shared CLI/MCP tools.
- Five additional Screaming Frog MCP tools and a 96-check crawl analyzer.
- Domain, CDN, technology, security, mirror, regional, bot, and backlink reconnaissance.
- Schema.org validation and connected graph generation.
- Bounded sitemap-based site evidence and XLSX, DOCX, CSV, Markdown, and JSON reports.
- Optional Wordstat, Yandex SERP, Arsenkin, Metrika, and DataForSEO integrations.
- Twenty-one technical workflow skills and seven packaged SEO playbooks.
- Local stdio MCP and Docker support; no GUI, hosted API, or telemetry.
- Evidence-led public README with reproducible synthetic audit, task, interface, and report visuals.
- History-free public release boundary and explicit third-party notices.
