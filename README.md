<div align="center">

![SEOHEAD Tools](https://raw.githubusercontent.com/PavloSEO/seotools/main/.github/assets/social-preview.jpg)

# SEOHEAD Tools

**The local evidence and audit-automation layer for SEO specialists and tool-calling AI agents.**

68 callable tools · 152 checks · 29 workflow skills · 56 scenarios · 2 400+ offline tests · CLI · local MCP · Docker

[Website](https://seohead.tech) · [Documentation](docs/README.md)

[![CI](https://github.com/PavloSEO/seotools/actions/workflows/ci.yml/badge.svg)](https://github.com/PavloSEO/seotools/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-1565C0)
![Tests](https://img.shields.io/badge/tests-1500%2B%20offline-BDDDF5)
![MCP](https://img.shields.io/badge/MCP-local%20stdio-151A25)
[![MIT License](https://img.shields.io/badge/code-MIT-1565C0)](LICENSE)

[Quick start](#quick-start) · [Agent recipes](docs/RECIPES.md) · [Inspect the real example](examples/README.md) · [Scope and trade-offs](docs/COMPARISON.md)

</div>

**SEOHEAD is not a Screaming Frog replacement.** Screaming Frog produces the CSV/XLSX exports
consumed by SEOHEAD's 152-check analyzer, and remains the stronger choice for web-scale crawls.
SEOHEAD also ships its own bounded native crawler (`crawl-site`) for when no SF licence is
installed: it fetches a site directly and feeds the same 152-check registry, but it is not
SF-scale or SF-parity — checks whose evidence only Screaming Frog's own crawl produces (near-
duplicates, readability, pixel widths, link score) come back honestly `skipped`, not clean.
SEOHEAD then runs complementary bounded checks, keeps failed and unavailable measurements
visible, and gives a specialist or tool-calling agent one tested CLI/MCP surface for assembling
an audit, prioritized backlog, and reports.

The package brings live URL checks, infrastructure reconnaissance, structured-data work, log and
content analysis, optional keyword/SERP/traffic sources, report generation, and agent playbooks
into that workflow. Think of it as the automation and evidence layer around the crawler, not an
alternative to the crawler or to specialist judgement.

The toolkit does not write strategy or client copy by itself. It collects evidence, applies
deterministic checks, and returns structured data. A capable tool-calling agent can then combine
those results into a site review, competitor brief, migration plan, prioritized backlog, or
commercial-proposal draft while a specialist keeps control of interpretation.

### Different jobs, one workflow

| Stage | Primary owner | Role |
|---|---|---|
| Crawl collection | Screaming Frog, or SEOHEAD's own bounded `crawl-site` when no SF licence is installed | Discover site URLs and produce evidence for the 152-check registry — SF for web-scale crawls, `crawl-site` for a licence-free bounded pass |
| Evidence processing | SEOHEAD Tools | Analyze that evidence against a 152-check registry, run targeted live and infrastructure tools, preserve uncertainty, and build structured artifacts |
| Interpretation and approval | SEO specialist, optionally supported by an AI agent | Connect findings to business context, implementation risk, and final priorities |

See [how SEOHEAD fits with crawlers and data providers](docs/COMPARISON.md) for the exact scope
boundary.

## Reproducible output from a committed synthetic fixture

![Screaming Frog exports pass through the SEOHEAD analyzer and become an audit and prioritized task backlog](.github/assets/audit-workflow.png)

The values above come from the committed synthetic fixture: **6 URLs, 18 issues, and 15 tasks**.
Open the generated [`audit.md`](examples/audit.md) and [`tasks.md`](examples/tasks.md), or reproduce
them locally with `seohead sf run --exports-dir examples/exports --out examples --tasks`.
No client data is included.

## Choose your path

| Starting point | Start with | What it does |
|---|---|---|
| **A site, and no crawl yet** | `seohead crawl-site --url https://example.com --out-dir ./run` | Crawls the site with this toolkit's own crawler — no Screaming Frog, no licence — audits the result through the same 152-check registry, and writes `audit.json` beside a prioritized `tasks.md` backlog |
| **Existing Screaming Frog exports** | `seohead sf run --exports-dir ./exports --out ./report --tasks` | Evaluates crawl evidence you already have against the 152-check registry and builds an audit plus a prioritized backlog |
| **Screaming Frog installed and licensed** | `seohead sf run --crawl https://example.com --out ./report` | Drives your local Screaming Frog CLI, then audits its exports — one command instead of crawl, export, import |
| A bounded current-state pass | `seohead site-audit --url https://example.com --limit 25` | Sitemap-based live, page and infrastructure checks. Not a link-graph crawl, and it says so |
| A tool-calling AI agent | `seohead mcp` | 63 shared `seo_*` handlers plus five `sf_*` crawl-workflow tools over local stdio |
| Two crawls, and the question "did they fix it" | `seohead compare-crawls --before a.json --after b.json` | Per check: entered, appeared, left, disappeared — a fix and a deletion are different answers |

## Why an agent should reach for this

Five things this gives a tool-calling agent that a general-purpose browser or a shell full of
`curl` does not:

**One evidence contract across every source.** A native crawl, a Screaming Frog export, and a
licensed Screaming Frog run all produce the same audit document with the same field names. An
agent writes one consumer, not three.

**Structured output on failure, not just on success.** Every tool returns JSON. When a source is
unreachable it returns `{"ok": false, "error": "..."}` rather than raising — an unreachable site is
data about the site, not an accident that ends the run. Since #155 that `ok: false` also reaches
the process exit code, so a shell caller and a JSON caller agree about what happened.

**The cost of a call is declared before it is made.** [docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md)
is generated from the code and states, per tool, whether it touches the network, whether it writes
to disk, whether it is idempotent, and whether it can spend money. Paid providers write to a spend
journal; `spend-report` reads it back by source, operation and day.

**It refuses to guess.** No check reports a result it cannot support: missing evidence is a named
skip, a partial crawl withholds the conclusions that need completeness, and a check that suddenly
describes most of a site is flagged for a human before the rest of the report is believed.

**The chains are written down.** 56 scenarios in [docs/scenarios/](docs/scenarios/README.md) give
the actual command sequences for real jobs — a migration audit, a duplicate-content pass, a
robots-and-indexability review — with what each produces, what it costs, and what it cannot answer.
An agent does not have to invent an order of operations.

## Why it is useful

A serious review repeatedly asks the same questions: what is indexable, what redirects, where
canonicals point, whether hreflang is reciprocal, what Schema.org declares, which technologies
and CDN are present, what bots can crawl, what the logs show, and how all of that becomes a
deliverable. SEOHEAD turns this collection layer into reusable tool calls.

In the author's workflow, evidence collection and report scaffolding are often several times
faster because one agent can run the same bounded checks, preserve their structured output, and
assemble the first report pass. This is an experience statement, not a universal benchmark.
Network conditions, crawl scope, provider quotas, and expert review still determine total time.

## What is included

### Start here

**[docs/GUIDELINE.md](docs/GUIDELINE.md)** — what this is, your first run, how to choose a crawl
rate and tell whose fault the errors are, how to read an audit without being misled by it, the
mistakes everybody makes first, and what it cannot answer at all. Written for a person, in the
order a person meets the tool.

### What it does end to end

For an agent: **[.claude/skills/control/SKILL.md](.claude/skills/control/SKILL.md)** is the
entry point — what to run on a site nobody has looked at yet, in what order, and whether to
believe the answer. It routes to the 21 method skills and carries its own sub-skills and a
reference archive of defects found on live sites.

Individual tools are listed below; **[docs/scenarios/](docs/scenarios/README.md)** describes the
chains — several tools in order, ending in something a person can act on. Fifty-six of them,
each with the real commands, the artifact that comes out, what it costs, and what that chain
cannot answer. Every issue this toolkit can find appears in at least one, checked by a test
against [docs/COVERAGE_SF_ISSUES.md](docs/COVERAGE_SF_ISSUES.md). Start there if you are
evaluating what this repository is for; every command shown in those files is executed against
a fixture site on every CI run.

### 63 core CLI commands and MCP tools

| Layer | Tools | What it covers |
|---|---:|---|
| Live page and URL evidence | 14 | parsing, robots.txt, headers, CSS/JS weight and delivery, links, hreflang, redirects, sitemaps, image download and optimization, keyword clustering |
| Domain and infrastructure reconnaissance | 8 | domain/DNS/TLS, CDN cache behavior, technology detection, security headers, mirrors, regional structure, donor backlink verification, AI crawler access |
| Structured data, content, rendering, and logs | 12 | Schema.org validation and graph generation, near-duplicates, llms.txt, citability, content-area Markdown extraction, boilerplate consistency, social previews, soft 404s, raw-vs-rendered DOM, access-log analysis, run-artifact contradiction scanning |
| Audit orchestration and reporting | 6 | bounded sitemap-based site evidence, the crawler's own configuration surface, XLSX/DOCX/CSV/Markdown/JSON output, a no-network multi-site facts export, offline SQLite reanalysis, and a cross-segment counterpart diff that answers what one language version has and another does not |
| Demand, SERP, and traffic sources | 17 | Yandex Wordstat and async SERP, Arsenkin exact frequency, Yandex Metrika, DataForSEO Google data, region tree, credential and spend diagnostics, Wayback snapshot history, certificate-log subdomains, Search Console, CrUX field vitals, IndexNow submission |
| Saved scan history | 6 | metadata cataloguing, bounded table inspection, portable snapshots, retention pins, reviewed pruning, and retained-body comparison |

Run `seohead --help` for the authoritative command list. Every core command goes through the
same handler used by its `seo_*` MCP counterpart; a test gate fails if the interfaces drift — and
another fails if the CLI can name an argument the MCP tool cannot, which is how an entire crawl
mode once stayed unreachable for agents.

<details>
<summary><b>All 63 commands, by what they answer</b></summary>

**Crawl a site yourself** — `crawl-site` · `crawl-describe-settings` · `compare-crawls` ·
`log-scan`

**One page, one answer** — `parse` · `headers-check` · `links-check` · `hreflang-check` ·
`redirects-check` · `redirects-generate` · `schema-check` · `schema-build` · `social-meta-check` ·
`soft404-check` · `render-check` · `markdown-extract` · `citability-check` · `asset-weight-check`

**The site as a whole** — `site-audit` · `sitemap-crawl` · `robots-check` · `llms-txt-check` ·
`ai-bots-check` · `duplicate-check` · `boilerplate-report` · `report-build` · `facts-export`

**Infrastructure and identity** — `domain-profile` · `cdn-check` · `tech-detect` ·
`security-check` · `mirror-check` · `regions-check` · `backlinks-check` · `crtsh-subdomains`

**Images** — `images-download` · `images-optimize`

**Logs** — `log-analyze`

**Demand and search data** — `keywords-expand` · `keywords-seasonality` · `keywords-exact` ·
`keywords-cluster` · `serp-fetch` · `google-keywords` · `google-serp` · `regions-tree` ·
`gsc-query` · `crux-report` · `wayback-history` · `indexnow-submit`

**Analytics** — `metrika-counters` · `metrika-setup` · `metrika-report`

**Housekeeping** — `sources-doctor` · `spend-report`

**Saved scan history** — `scan-list` · `scan-inspect` · `scan-snapshot` · `scan-pin` ·
`scan-prune` · `scan-body-diff`

Each has the same MCP counterpart named `seo_<command>` with dashes replaced by underscores.
[docs/TOOL_REFERENCE.md](docs/TOOL_REFERENCE.md) is generated from the code and carries every
argument, its type and default, whether the tool touches the network, whether it writes, whether
it is idempotent, and whether it can spend money.

</details>

### Three ways to get crawl evidence

The audit layer does not care where the crawl came from. Three sources feed the same 152-check
registry and produce the same audit document, so a report built one way is comparable with a
report built another.

**1. This toolkit's own crawler — no licence, no other software.**

```bash
seohead crawl-site --url https://example.com --out-dir ./run --max-urls 500
```

Follows links from a start URL, or fetches an explicit list (`--urls`), or seeds from a sitemap
(`--sitemap`). It obeys robots.txt by default, adapts its request rate to what the origin can
take, resumes from a checkpoint if it is interrupted, and can escalate to a real browser for the
pages that need one. Every setting is discoverable: `seohead crawl-site --config-help` prints each
one with its type, default, and whether it changes *what the audit finds* or only what the run
costs. `seohead crawl-describe-settings` returns the same thing as JSON, so an agent can read the
configuration surface without a filesystem.

**2. Your licensed Screaming Frog, driven for you.**

```bash
seohead sf run --crawl https://example.com --out ./report --tasks
```

Launches the local Screaming Frog CLI, waits for it, then audits the exports it produced. Requires
an installed and licensed SEO Spider — this toolkit neither bundles nor replaces it.

**3. Screaming Frog exports you already have.**

```bash
seohead sf run --exports-dir ./exports --out ./report --tasks
```

Reads CSV or XLSX exports from any previous crawl, by anyone, on any machine. Nothing is fetched.

**What differs between them is evidence, and the report says so.** A native crawl has its own link
graph and can answer questions about internal linking that no export carries; a Screaming Frog
export carries columns the native crawler does not produce. A check with no evidence is reported as
skipped **with the reason**, never as "no issues found" — that distinction is the point of the
tool, and `docs/COVERAGE_SF_ISSUES.md` maps all 320 published Screaming Frog issue types onto what
this repository does and does not cover.

### Screaming Frog audit layer

Five additional `sf_*` MCP tools turn a Screaming Frog crawl into machine-readable evidence,
compact summaries, filtered findings, an export inventory, and a prioritized task backlog.

The analyzer has a registry of **152 checks** across metadata, indexability, canonicals, redirects,
internal links, sitemaps, hreflang, structured data, page depth, HTML weight, performance signals,
and other crawl-derived evidence. It applies the checks supported by the available exports;
missing input is reported as skipped with a reason, never silently converted into “zero issues.”

Two modes are intentionally supported:

- **Export mode** analyzes existing CSV/XLSX exports and does not require SEOHEAD to run
  Screaming Frog.
- **Live crawl mode** launches the local Screaming Frog CLI and therefore requires an installed,
  active paid Screaming Frog SEO Spider licence. SEOHEAD does not bundle or replace that licence.

### 28 agent workflow skills

The repository ships 22 technical-audit playbooks in `.claude/skills/` and seven broader SEO
content/research playbooks in `seohead/skills/`. They teach an agent when to call tools, how to
separate evidence from inference, and how to assemble outputs without pretending that an
unmeasured signal is clean.

`analytics-console-review` describes a permissioned, read-only browser/export fallback when an
official provider API is unavailable. The repository does not bundle a browser or provider login.

## Quick start

Clone the repository and let one install command resolve the Python dependencies:

```bash
git clone https://github.com/PavloSEO/seotools.git
cd seotools
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

Optional components stay optional:

- `render` adds Playwright-based raw/rendered comparison; install its Chromium separately;
- `mcp` adds the local stdio server;
- `cluster` adds scikit-learn clustering;
- `reports` adds DOCX/XLSX output;
- `sitemap` adds optional sitemap helpers;
- external providers require your own credentials and may charge their own fees.

## One-command examples

```bash
# Bounded sitemap-based live evidence pass (not a link-graph crawl), then write an Excel file
seohead site-audit \
  --url https://example.com \
  --limit 25 \
  --report xlsx \
  --out report.xlsx

# Crawl a site with the crawler built into this toolkit — no Screaming Frog licence needed.
# Writes audit.json, pages.jsonl and a prioritized tasks.md backlog into ./run
seohead crawl-site \
  --url https://example.com \
  --max-urls 500 \
  --out-dir ./run

# Turn that same audit into a client deliverable, or a working file for a developer
seohead report-build --audit ./run/audit.json --format docx --out audit.docx
seohead report-build --audit ./run/audit.json --format xlsx --out audit.xlsx

# Audit existing Screaming Frog exports without crawling again
seohead sf run \
  --exports-dir ./exports \
  --out ./report \
  --tasks

# Inspect one page and its infrastructure
seohead parse --url https://example.com
seohead headers-check --url https://example.com
seohead schema-check --url https://example.com
seohead domain-profile --domain example.com

# Build a connected Schema.org graph from facts visible on the page
seohead schema-build --url https://example.com/product/example

# Optimize images into a separate directory; source files stay untouched
seohead images-optimize \
  --files ./images \
  --output-dir ./optimized \
  --format webp \
  --quality 82
```

All commands also accept a JSON object through `--input`; without explicit flags, that object may
come from stdin. See [usage examples](docs/USAGE.md) and the [tool reference](docs/TOOLS.md).

## One audit document, five deliverables

![The same structured SEOHEAD audit document rendered as XLSX, DOCX, CSV, Markdown, and JSON](.github/assets/report-formats.png)

`report-build` formats existing evidence without adding findings or making network requests.
XLSX is a four-sheet working file; DOCX is a client deliverable; CSV, Markdown, and JSON preserve
the same contract for import, review, and data exchange. See the
[report fixtures and field contract](examples/reports/README.md).

## Local MCP server

Install the `mcp` extra, then register one stdio process in any compatible client:

```json
{
  "mcpServers": {
    "seohead": {
      "command": "/absolute/path/to/.venv/bin/seohead",
      "args": ["mcp"]
    }
  }
}
```

The server exposes **63 `seo_*` tools plus five `sf_*` tools**. The 63 core tools share the tested
handler layer used by the CLI; the five SF tools expose the crawl workflow separately. The process
opens no port, hosts no dashboard, stores no account, and sends no telemetry. File-producing tools
return paths instead of dumping large reports into an agent context.

## Docker and VPS use

The image is headless and exposes no network service:

```bash
docker build -t seohead-tools:local .
docker run --rm seohead-tools:local --version
docker run --rm seohead-tools:local parse --url https://example.com
```

For MCP, keep stdin attached and mount only the workspace the agent may read or write:

```json
{
  "mcpServers": {
    "seohead": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "/absolute/authorized/workspace:/data",
        "seohead-tools:local", "mcp"
      ]
    }
  }
}
```

On a VPS, the same container is launched by the local agent host. There is deliberately no public
MCP endpoint in this repository. The image does not bundle Screaming Frog or a Playwright browser;
export-mode SF analysis works, while live SF crawls and rendered checks use authorized host tools.

## External data sources

Provider integrations are optional and explicit:

- Yandex Cloud supplies Wordstat expansion, seasonality, the region tree, and async Yandex SERP;
- Arsenkin supplies exact frequency where the Wordstat API does not;
- Yandex Metrika supplies counter configuration and traffic reports;
- DataForSEO supplies Google keyword and SERP data and defaults to its sandbox environment.

Secrets are read from environment variables or local configuration files and are never shipped.
Paid calls are journalled before response parsing so a parser failure cannot make spend invisible.
Read [provider gotchas](docs/GOTCHAS.md) before enabling production credentials.

## What keeps the output honest

The claim this repository makes is not "it finds everything" — it is that **it does not report what
it did not measure**. That is enforced mechanically, not by intention:

| | |
|---|---|
| **2 400+ tests**, all offline | No test reaches the network. The suite runs in CI with no egress, so a green run means the logic is right, not that a site happened to answer |
| **56 scenarios** in `docs/scenarios/` | Each is a real chain of commands ending in something a person can act on. Every command in them is executed against a fixture site on every CI run — a documented example that cannot work fails the build |
| **Skipped is not clean** | A check without evidence is reported as skipped *with its reason*. `checks_fired`, `checks_skipped`, `checks_disabled` and `checks_silent` are four separate, listed buckets, so "nothing was wrong here" and "nobody looked here" are different answers |
| **Implausible findings are named** | Any check covering more than half the crawled pages is listed above the findings, because a check that describes most of a site is usually broken rather than right |
| **Partial crawls withhold conclusions** | A finding that needs a complete link graph — "nothing links here" — is withheld and named, not footnoted, when the crawl did not finish |
| **`log-scan`** | Reads a finished run's own artifacts and reports where two numbers in it disagree. Exit 2 means the run contradicts itself |
| **Counts cannot drift** | Every number this README states about the registry is checked against the code by a test. So are the command lists in the skills, the coverage map, and the generated references |

## Safety and honest limits

- Network tools reject non-HTTP schemes and block private/non-public targets by default.
- File-changing operations require explicit intent; image optimization is non-destructive by
  default and validates output before reporting success.
- Security path probes, bot DNS verification, and sitemap live rechecks are opt-in.
- DataForSEO production mode is opt-in; its default is sandbox.
- Yandex SERP uses only the asynchronous endpoint.
- The toolkit does not discover the web-scale backlink profile of a domain.
- Lab browser timings are labelled as lab data, not field Core Web Vitals.
- `backlinks-check` verifies a donor list; it does not replace Ahrefs, Majestic, GSC, or another
  backlink index.
- International tools validate hreflang and regional structure; the package does not claim a
  machine-translation engine. Translation belongs to a reviewed model or localization workflow.
- `site-audit` is a bounded sitemap-based evidence pass, not an exhaustive run of all 47 core
  tools and not a replacement for a production crawler.
- SEOHEAD does not include its own general-purpose crawler. Whole-site crawling is delegated to
  Screaming Frog; export analysis remains available without live crawl mode.

Read [SECURITY.md](SECURITY.md), [architecture](docs/ARCHITECTURE.md), and
[limitations](docs/COMPARISON.md) before using outputs in a client deliverable.

## Development

```bash
python -m pip install -e ".[dev,mcp,cluster,reports]"
ruff check .
ruff format --check .
pytest -q
seohead sf run --exports-dir examples/exports --out /tmp/seohead-report --tasks
python -m build
```

The suite contains **over 1500 offline tests**. CI also checks interface registration, layer boundaries,
the synthetic crawl audit, package metadata, and English-only public documentation.

README visuals are generated from committed synthetic examples with
[`scripts/render_readme_visuals.py`](scripts/render_readme_visuals.py); they are evidence views,
not screenshots of a fictional dashboard.

## Provenance and licence

The Python implementation and documentation are released under the [MIT License](LICENSE).
The bundled Schema.org vocabulary retains its original CC BY-SA 3.0 terms. Compatible upstream
projects that informed individual algorithms are credited in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); no GPL or unlicensed source code is included.
See [PROVENANCE.md](PROVENANCE.md) for the clean-snapshot policy and
[TRADEMARKS.md](TRADEMARKS.md) for the SEOHEAD name and terminal mark. Academic users can use the
repository's [citation metadata](CITATION.cff).
