# Operational gotchas

These are the boundaries most likely to produce a wrong conclusion, unexpected cost, or damaged
input. Sources are the current code contracts, provider documentation, and
[DECISIONS.md](DECISIONS.md).

## Money

- **Yandex SERP is async-only, on purpose.** The synchronous endpoint is substantially more
  expensive under the provider tariff. There is no sync method in the code; confirm current
  prices before a large run.
- **The Wordstat quota is 100 requests/hour** — tighter than the price. A
  careless collection loop hits the quota long before it hits the budget.
- **Wordstat frequency is base, not exact.** The API has no `!`/`+`/`[]`
  operators; the base is ~9x inflated. Exact numbers come from
  `keywords-exact` (Arsenkin). A multi-region request **sums** frequency —
  query regions one at a time.
- **Every paid call journals before parsing.** `spend.record()` fires the
  moment a task is created, with its `task_id` — a crashed parser must not
  turn money into nothing, and a paid result can be re-fetched for free.
  Check `seohead spend-report` before and after any large run; measured provider
  units are safer than an estimate.
- **DataForSEO defaults to `sandbox`** (real shapes, fake data, RUB 0).
  Production is `DATAFORSEO_ENV=prod`, never switched automatically. Russia
  and Belarus are unsupported; a request for either
  country is stopped by a geo guard before it reaches the network.
- **Metrika Logs API exports contain raw `ClientID`** — personal data.
  Never commit, never show a client; output paths must stay gitignored.

## CLI footguns

- **`seohead sf tasks` needs `--json report/audit.json`**, not a positional
  path. The parser is `argparse` with `required=True` (`seohead/sf/cli.py`).
- **`images-optimize` is non-destructive by default.** It requires `--output-dir` unless
  `--in-place` is explicit. In-place mode creates backups by default, and replacing an existing
  destination still requires `--overwrite`.
- **Piping and stdin**: `seohead` waits 0.2 s for stdin and then gives up
  (`STDIN_WAIT_SECONDS` in `cli.py`) — a script with an open but empty
  stdin hangs forever otherwise. Conversely, when a source flag (`--url`,
  `--domain`, `--path`, …) is present, stdin is never read: without that,
  `while read u; do seohead parse --url "$u"; done < urls.txt` would drink
  the whole file on the first iteration and process exactly one URL.
- **`sf run` exit code 2 means critical findings** (not an error). `1` is a
  real error; use `--fail-on critical` deliberately in CI.
- **`sf-analyzer` is a focused alias** for the same audit CLI; `seohead sf ...` keeps the whole
  toolkit under one entry point.

## Reading the results

- **`networkidle` may never arrive on a live commercial site** because analytics, chats, ads,
  or WebSockets can keep connections open. Hence `--wait load` is the default; request
  `networkidle` explicitly only when the target makes it meaningful. If a requested milestone
  times out, `render-check` reads the DOM at `domcontentloaded` instead of losing the check and
  records that in `wait_reached` — two runs compared against each other should be captured at
  the same milestone.
- **An empty rendered snapshot is not a JavaScript finding.** A render that did not finish
  returns a document with no title, no `h1`, no canonical and no links. `render-check` detects
  that (`ok: false`, `reason: "incomplete_render"`, `js_dependent: null`) rather than reporting
  the site as rewriting its own metadata; re-run it, and never quote the truncated snapshot as
  a diff.
- **Playwright metrics are lab numbers** — `metrics_lab`, one browser, one
  machine. They are not field Core Web Vitals; naming them that way in a
  client report sets up a contradiction with Search Console.
- **Geo is country-only.** No city, no coordinates — without a licensed
  GeoIP database a city would be a guess.
- **Without `h2` the HTTP version is not measured**: the report says
  `http_version_measurable: false` instead of inventing HTTP/1.1.
- **"Skipped" is not "clean".** A check with no data reports itself as
  skipped with a reason. "0 problems" and "nothing to check" are different
  statements — trust the skip list.
- **Some sites serve 403 to bots**: a "broken" external link can be a bot
  block, not a dead page (see the `BROKEN_EXTERNAL_LINK` fix hint).
- **Robots blocks crawling, not indexing**: a page blocked in robots.txt
  with internal links pointing at it is flagged as
  `IMPORTANT_URL_BLOCKED_BY_ROBOTS` because link discovery is lost —
  indexing is controlled by canonical/noindex.

## Audit prerequisites and limits

- **`.seospider` files are Java serialization** and are never parsed
  directly. Mode A needs the SF CLI with a license; mode B needs manual
  CSV/XLSX exports. Mode B is self-sufficient — do not break it for A.
- **Crawl Analysis must be enabled in SF** for Sitemaps / Near Duplicates /
  Orphan pages data to exist in exports at all.
- **`health_score` is a heuristic orientation**, not a truth. Do not put it
  in a contract.
- **`backlinks-check` verifies your donor list**; it does not discover
  links. "Show all links to a site" needs external indexes (Ahrefs,
  Majestic, GSC).

## Contributor traps

- **Numbers in docs are enforced by `tests/test_docs_drift.py`.** Wrong
  counts of tools, skills, or checks in README, skills, or docs fail
  the suite. A line with an intentional old number needs the
  `<!-- drift-ok -->` marker.
- **A new tool has four registration points** (core, handler, CLI, MCP);
  `test_registration.py` catches a missed one, while the MCP integration test verifies that the
  published stdio server starts and exposes structured output schemas.
- **Changed `sf/core/registry.py` -> regenerate
  `.claude/skills/sf-analyzer/reference/checks.md`.**
- **Refactoring rule: after every step, `pytest -q` green and an identical
  `audit.json` on `examples/exports`** (diff by `summary.by_check`).
- **Never commit** `*.seospider`/`*.dbseospider`, `report/`, root-level
  `audit.json`/`audit.md` (sample outputs live in `examples/` only). All of
  these are gitignored — `git status` after an audit run should stay clean.
- **Thresholds/severity live in `config.json`**, never in code. A hardcoded
  number in a check is a bug even when the value is right.
