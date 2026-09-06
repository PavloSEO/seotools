# Setup from zero

Everything below was verified on macOS (darwin, arm64) with the repo's own
venv; the same steps work on Linux. Windows paths for the SF CLI are
supported by `config.json` search paths.

## Requirements

- **Python 3.10+** (`requires-python` in `pyproject.toml`).
- pip, git. That is all — every other dependency is a Python package.
- Optional, improves results if present on the system:
  - **Screaming Frog SEO Spider CLI** — for audit mode A (the toolkit
    drives the crawler itself). Without it, mode B works from ready exports.
  - **system `whois`** — fallback for ccTLDs without RDAP. Without RDAP and
    without `whois`, domain registration data is honestly reported as
    `source: none`.

## Install

```bash
git clone https://github.com/PavloSEO/seotools.git
cd seotools

python3 -m venv .venv
source .venv/bin/activate

pip install -e ".[all,dev]"            # everything incl. reports, render, tests
python -m playwright install chromium  # browser for render-check (~150 MB)
```

Why `-e`: the `seohead` entry point must see the working tree while you edit
it. The install is not global — everything lives inside `.venv/` (which is
gitignored).

The toolkit is deliberately split into dependency groups (`pyproject.toml`,
`[project.optional-dependencies]`); with any group missing, the rest still
works, and the affected tool answers `{"ok": false, "error": ...,
"install": "..."}` with the exact install command instead of crashing:

| Group | Installs | What stops working without it |
|---|---|---|
| base (always) | `httpx`, `beautifulsoup4`, `lxml`, `defusedxml`, `pandas`, `h2`, `pydantic`, `jsonschema`, `openpyxl`, `Pillow` | nothing, this is the minimum |
| `reports` | `python-docx` (+ openpyxl already in base) | `docx` output of `report-build` (xlsx/csv/md/json stay) |
| `render` | `playwright` | `render-check`, `regions-check --render` |
| `sitemap` | `advertools`, `python-dateutil` | deep parsing of very large sitemaps |
| `mcp` | `mcp` | the MCP server (the CLI stays) |
| `cluster` | `scikit-learn`, `numpy`, `snowballstemmer` | `keywords-cluster` |
| `dev` | `pytest`, `pytest-cov`, `ruff`, `respx` | the test suite |

## First run

```bash
seohead --version                     # seohead 3.0.0
seohead --help                        # the command list
pytest -q                             # over 1500 offline tests; runtime depends on extras
seohead sf run --exports-dir examples/exports --out /tmp/report --tasks
```

The last command runs a real audit (mode B) over the synthetic crawl in
`examples/exports/` and writes `/tmp/report/audit.json` + `audit.md` +
`tasks.json` + `tasks.md`. If that works, the toolkit works.

## Crawling without a Screaming Frog licence

```bash
seohead crawl-site --url https://example.com/ --max-urls 200 --out-dir ./report
```

Follows links from the start URL on the same host, respects `robots.txt`, and audits the result
through the same checks used for Screaming Frog exports. `--min-delay` is the floor beneath an
adaptive back-off: latency widens the delay, a timeout widens it hard, and repeated timeouts stop
the run rather than pushing a failing origin. Rows land in `pages.jsonl` as they are collected, so
an interrupted crawl still leaves evidence behind.

This is not Screaming Frog parity. Checks whose evidence a native crawl cannot produce —
near-duplicates, readability, pixel widths, link score — are reported as **skipped**, never
as clean, and `summary.check_coverage` states how much of the registry ran. Redirect chain
and loop detection are the exception: they are resolved from the crawl's own per-URL
redirect targets, as a second pass over the finished crawl, with no Screaming Frog report
required.

`seohead sf doctor` prints environment diagnostics: where the SF CLI is (or
is not), which optional dependencies are present, which base Screaming Frog
config a crawl would use, and which module switches that config turns on. A
module the reader cannot decode is printed as `unknown`, never as `off`.

Module switches decide whether the module-dependent checks can run at all.
Without a base config SF crawls with its own defaults and those checks come
back `skipped` — which you would otherwise only discover after the crawl. Two
things make that visible up front:

```bash
seohead sf save-config                # copy the latest SF crawl config to audit.seospiderconfig
seohead sf save-config --out base.seospiderconfig --force
```

`sf run` prints a `[preflight]` line before a fresh crawl for every check that
the configuration in force cannot satisfy, so the config can be fixed first.
Mode B (`--exports-dir`) already has the exports and is not affected.

`sf-analyzer` is also installed as a focused audit-CLI alias (`[project.scripts]` in
`pyproject.toml`). Use `seohead sf ...` when one entry point is preferable.

## Docker

```bash
docker build -t seohead-tools .                      # slim: crawl, audit, MCP
docker build --build-arg EXTRAS=all -t seohead-tools:full .   # + clustering, rendering
```

The default image carries the crawl-and-audit path and the MCP server. Keyword clustering is
deliberately not in it: `scikit-learn` pulls `scipy` transitively, which is 119 MB for a feature
unrelated to crawling a site. Rendering (Playwright) is likewise opt-in — it needs a real Chromium
and its shared libraries.

Measured: **440 MB slim, 1.08 GB full.** CI prints the image size on every run, so a transitive
dependency that quietly adds a couple of hundred megabytes shows up in the build log rather than
being discovered later. Both variants are built and smoke-tested in CI.

The builder stage also drops build-only weight that a straight `pip install` leaves behind: it
installs with `--no-compile` (pip's install-time `compileall` pass otherwise bakes in ~67 MB of
`__pycache__` that `PYTHONDONTWRITEBYTECODE` at runtime does nothing to prevent, since that only
stops imports from writing bytecode later) and uninstalls `pip` itself (~13 MB) before the venv is
copied into the runtime stage — the image only ever runs the `seohead` entry point, never pip.

`pandas` does **not** require `pyarrow`: as of pandas 3.0, `pyarrow` is still an optional extra
(`pandas[pyarrow]`), not a runtime dependency — confirmed against the pandas 3.0.5 wheel metadata
and by building this image and checking `pip`'s own dependency-resolution log. `pyarrow` is real
weight in the *full* image, but it comes from `advertools` (the `sitemap` extra, pulled in by
`EXTRAS=all`), not from pandas, and not from anything in the default slim build — the slim image
never installs `pyarrow` regardless of the pandas pin. Pinning `pandas<3` would therefore not
remove it and was not made; if `sitemap`'s ~46 MB `pyarrow` weight needs trimming later, the fix is
in `advertools`'s own dependency tree, tracked separately from this issue.

## Comparing two crawls

```bash
seohead compare-crawls --before old-audit.json --after new-audit.json
```

The most repeated question in audit work is "did the fix actually ship", and a naive diff of two
finding sets cannot answer it: a page that stopped matching because it was fixed and a page that
stopped matching because it dropped out of the crawl look identical unless something also checks
which pages were actually crawled each time.

Every finding lands in exactly one of four sets:

- **entered** — a new problem on a page that was already being crawled;
- **left** — the page is still crawled and no longer matches: a real fix;
- **appeared** — a genuinely new page, and it has a finding;
- **disappeared** — the page is not in the later crawl at all, so a missing finding proves
  nothing about whether it was fixed.

`left` and `disappeared` are the pair that matters. Confusing them is how "we fixed it" gets said
about a page nobody re-checked.

The result carries `warnings` when the comparison should be trusted less: either crawl marked
invalid or partial, or the two runs using different results-affecting settings (`run.crawl_config`
from #13) — in which case some of the difference may be the configuration, not the site.

## Crawler configuration

```bash
seohead crawl-site --url https://example.com/ --config crawl.json --out-dir ./report
```

```json
{
  "limits": {"max_urls": 500, "max_depth": 4},
  "speed": {"min_delay_seconds": 1.0},
  "robots": {"policy": "report_only"},
  "discovery": {"external": {"store": true, "crawl": false}}
}
```

Resolution order is defaults, then the file, then environment variables, then explicit command-line
arguments — the most local statement of intent wins.

`crawl-site --help` only shows the handful of settings used directly on the command line
(`--url`, `--max-urls`, `--out-dir`, `--config`, `--robots`, `--sitemap`); everything else — the
settings above and every one the crawler build-out has added since — lives in the config file. Run
`seohead crawl-site --config-help` for the full list: every key's path, type, default, and
description, generated from this module rather than hand-maintained. (`--max-depth` and
`--min-delay` still work as direct flags for scripts written before `--config` existed; they are
just no longer shown in `--help`.) The same list is reachable without a filesystem via
`seohead crawl-describe-settings` (CLI) or the `seo_crawl_describe_settings` MCP tool — a
tool-calling agent can discover the configuration surface, not only a human reading the source.

`--sitemap <url>` seeds the crawl from that sitemap's declared URLs — each one is fetched and its
own links are followed, rather than the sitemap being treated as the final answer — and reconciles
the declared set against the URLs the crawl actually reaches by following a link. The result lands
in `audit.json` as `summary.sitemap`, with three disjoint sets: URLs declared and linked (healthy),
URLs declared but never linked from any crawled page (orphaned — reported via `SITEMAP_ORPHAN`),
and URLs linked but never declared (reported via `URL_NOT_IN_SITEMAP`). With no explicit
`--sitemap`, setting `sitemaps.auto_discover` in `--config` seeds from every sitemap robots.txt
declares — a site can list more than one `Sitemap:` directive, and each one is fetched and its
URLs unioned, not just the first.

Three properties are deliberate:

**An unknown key is an error, not a no-op.** A setting the crawler does not read would promise
behaviour that does not exist, and a typo in a scope pattern would silently widen a crawl.

**`store` and `crawl` are separate flags** for every link type: keep it in the report, versus
request it for a status code. These are different questions, and one flag for both is why a crawler
either misses broken images or triples its request count.

**Settings that change what the audit finds are written into `audit.json`** as
`run.crawl_config`, with their resolved values. Two reports on the same site are otherwise not
comparable, and nobody can tell why they differ. `run.effective_max_requests_per_second` records the
politeness the run actually permitted, because politeness is a combination of settings rather than
any single one.

The crawler stops on its own when a host is failing: repeated timeouts, or repeated 429 and 5xx
responses, end the run rather than continuing at the same rate. A single 429 is treated as an
overload signal rather than a retryable blip — it is the server explicitly asking for less. A
numeric `Retry-After` raises the delay to at least what was asked.

When robots.txt supplies `Crawl-delay` or a valid `Request-rate: requests/seconds`, the crawler
uses the stricter interval before fetching declared sitemaps and every later request. The stored
`crawl_delay_applied` value is that effective robots-derived interval; the parsed robots context
retains the two directives separately.

The shared request budget covers robots, sitemap discovery and audit rechecks, page requests,
retries, redirects, captured resources, and browser HTTP routes. Robots directives can raise
the configured delay floor, never lower it.

Rendering launches Chromium with its sandbox enabled and refuses root execution. HTTP routes,
including popup requests, are fulfilled through the same validated, pinned HTTP transport;
Chromium does not continue those requests through its own DNS resolver. Browser cookies and
cross-origin restrictions are preserved. Service workers are blocked.

The renderer supports GET, HEAD, and OPTIONS, with a 5 MiB limit on each response's encoded
HTTP body. A blocked WebSocket, unsupported method, refused destination, or exceeded response
limit makes the render explicitly unavailable. Persistent browser profiles are currently
unavailable; requesting one returns a named error without opening its directory. Stored DOM
limits and credential-sensitive retention rules still apply separately.

`robots.policy` accepts `respect` (obey), `report_only` (fetch it, report what it would block, crawl
anyway — the honest audit setting), and `ignore` (do not fetch it at all).

Environment overrides: `SEOHEAD_CRAWL_MAX_URLS`, `SEOHEAD_CRAWL_MAX_DEPTH`,
`SEOHEAD_CRAWL_MIN_DELAY`, `SEOHEAD_CRAWL_ROBOTS`, `SEOHEAD_CRAWL_USER_AGENT`.

## Run journal

Every CLI command and MCP call is appended to one JSONL journal, so a session can be
reconstructed after the process exits: which tools ran, against what, how long they took, and
whether they failed.

```bash
SEOHEAD_RUN_LOG=./runs.jsonl seohead crawl-site --url https://example.com/
SEOHEAD_RUN_LOG=off seohead parse --url https://example.com/    # disable
```

Default path is `~/.config/seohead/runs.jsonl`. Journaling wraps the shared handler registry
rather than each interface, so both faces of the toolkit record exactly once and a new tool
cannot be added without being recorded.

Arguments whose names look like credentials — token, key, secret, password, auth — are stored as
`[redacted]`, and long values and lists are shortened rather than dropped. A journal that leaks an
API key would leak it silently, since nothing about a log file suggests it holds secrets.

Each entry carries a `fingerprint` of the call: the same tool with the same arguments produces the
same value regardless of argument order. Nothing currently reuses it — reuse is a decision for a
caller who knows whether a stale answer is acceptable, and that decision is deliberately not made
inside the journal.

An unwritable journal never fails a run: a degraded observation is not a failed audit.

## Environment variables

Names only — values are secrets and never belong in a repo, a log or a doc.

Tool behaviour:

| Variable | Purpose |
|---|---|
| `SF_CLI`, `SCREAMINGFROG_CLI` | explicit path to the SF CLI executable for audit mode A (`seohead/sf/core/runner.py`) |
| `SEOHEAD_TECH_DB` | path to an external technology-fingerprint database; not shipped for license reasons (`recon/tech_db.py`) |
| `SEOHEAD_RUN_LOG` | where the run journal is written (default `~/.config/seohead/runs.jsonl`); `off` disables it |
| `SEOHEAD_SPEND_LOG` | override for the paid-call journal (default `~/.config/seohead/spend.jsonl`) |
| `DATAFORSEO_ENV` | `sandbox` (default) or `prod` for the DataForSEO tools |

Credentials (each wins over its file under `~/.config/`; see
`seohead/data_sources/credentials.py`):

| Variable | File fallback | Used by |
|---|---|---|
| `ARSENKIN_TOKEN` | `~/.config/arsenkin/token` | `keywords-exact` |
| `YANDEX_CLOUD_API_KEY` | `~/.config/yandex-wordstat/api_key` | `keywords-expand`, `keywords-seasonality`, `serp-fetch` |
| `YANDEX_CLOUD_FOLDER_ID` | `~/.config/yandex-wordstat/folder_id` | same |
| `YANDEX_METRIKA_TOKEN` | `~/.config/yandex-metrika/token` | `metrika-*` |
| `DATAFORSEO_LOGIN` | `~/.config/dataforseo/login` | `google-keywords`, `google-serp` |
| `DATAFORSEO_PASSWORD` | `~/.config/dataforseo/password` | same |

`seohead sources-doctor` reports which of these are present and where they
are read from — run it before planning any paid collection.

## Docker alternative

No local Python needed:

```bash
docker compose run --rm seohead sf run --exports-dir /data/exports --out /data/report --tasks
docker compose run --rm seohead headers-check --url https://example.com
```

The image is a multi-stage build on `python:3.12-slim`, runs as non-root user `seohead`, and mounts
`./workspace` as `/data`. It does not bundle Screaming Frog or Chromium. See
[USAGE.md](USAGE.md).

## What is intentionally absent

- **No GUI, no web service, no HTTP API.** The two interfaces are the CLI and the local stdio MCP
  server. Reports are files.
- **No push deploy.** `git push` deploys nothing — there are no deploy
  workflows, hooks or scripts in this repo.
