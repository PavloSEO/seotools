# SEOHEAD Tools

**Local, evidence-first SEO audit automation for specialists and tool-calling agents.**

[Website](https://seohead.tech/seotools) · [Documentation](docs/README.md) · [Examples](examples/README.md) · [Scope and trade-offs](docs/COMPARISON.md)

[![CI](https://github.com/PavloSEO/seotools/actions/workflows/ci.yml/badge.svg)](https://github.com/PavloSEO/seotools/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-1565C0)
![MCP](https://img.shields.io/badge/MCP-local%20stdio-151A25)
[![MIT License](https://img.shields.io/badge/code-MIT-1565C0)](LICENSE)

SEOHEAD turns crawl and live-check evidence into reviewable audit documents, task backlogs, and reports. It runs as a Python CLI or a local stdio MCP server. There is no hosted account, dashboard, or public MCP endpoint.

It does not replace specialist judgement. It records what was measured, what failed, and what could not be measured so a specialist can assess scope, business context, and implementation risk.

## Start with the task

| If you have… | Run | You get |
|---|---|---|
| A site with no crawl | `seohead crawl-site --url https://example.com` | A bounded native scan under `./scans/` with retained crawl evidence and audit output |
| Existing Screaming Frog exports | `seohead sf run --exports-dir ./exports --out ./report --tasks` | `audit.json`, `audit.md`, `tasks.json`, and `tasks.md` without another crawl |
| A licensed local Screaming Frog installation | `seohead sf run --crawl https://example.com --out ./report --tasks` | A local SF crawl followed by the same audit artifacts |
| A current-state evidence pass | `seohead site-audit --url https://example.com --limit 25` | One `seohead.site-audit/1` document from selected sitemap URLs and site-level checks |
| Two compatible audit documents | `seohead compare-crawls --before before.json --after after.json` | Findings that entered, changed, or disappeared between runs |
| An agent client | `seohead mcp` | The local stdio MCP server, with the same public behavior as the CLI |

`crawl-site` is SEOHEAD's bounded native collector. It needs no Screaming Frog licence, but it does not claim Screaming Frog parity. Screaming Frog export mode reads CSV/XLSX exports you already have; live SF mode requires your separately installed, active licence. The two inputs produce related audit artifacts, but comparisons require compatible scope, configuration, and provenance.

## What makes an audit honest

Every audit distinguishes four outcomes:

- **Finding:** the available evidence supports a specific problem.
- **Ran without findings:** the check was evaluated and found nothing to report.
- **Skipped:** required evidence was unavailable or incomplete; the report names the check and reason.
- **Failed or unavailable tool:** the result records the boundary failure instead of treating it as a pass.

Partial crawls withhold conclusions that need complete evidence, such as link-graph claims. Health scores are withheld when coverage is too low, and scores based on incomplete coverage are marked as not comparable with full coverage. See [the audit guideline](docs/GUIDELINE.md), [check catalogue](docs/CHECKS.md), and [coverage map](docs/COVERAGE_SF_ISSUES.md).

## Quick start

```bash
git clone https://github.com/PavloSEO/seotools.git
cd seotools
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[all]"

# Confirm the installed interface.
seohead --help

# Run the committed synthetic Screaming Frog export fixture.
seohead sf run --exports-dir examples/exports --out ./report --tasks
```

On Windows PowerShell, activate the environment with `.venv\Scripts\Activate.ps1`.

The `all` extra installs every optional integration. Install only what a workflow needs when a smaller environment is preferable:

- `mcp` for the local stdio server;
- `render` for raw-versus-rendered DOM checks (install a Playwright browser separately);
- `cluster` for keyword clustering;
- `reports` for DOCX/XLSX output;
- `sitemap` for optional sitemap helpers.

## From evidence to a deliverable

```bash
# Crawl a site locally. The URL cap is an explicit input, not a claim about site size.
seohead crawl-site \
  --url https://example.com \
  --max-urls 500 \
  --scan-out ./scans/audit.sqlite

# Render the resulting audit as a client document or working spreadsheet.
seohead report-build --audit ./scans/audit.sqlite --format docx --out audit.docx
seohead report-build --audit ./scans/audit.sqlite --format xlsx --out audit.xlsx

# Run focused evidence checks when a full crawl is not the question.
seohead parse --url https://example.com
seohead headers-check --url https://example.com
seohead schema-check --url https://example.com
seohead domain-profile --domain example.com

# Optimize images into a separate directory; sources stay untouched.
seohead images-optimize \
  --files ./images \
  --output-dir ./optimized \
  --format webp \
  --quality 82
```

`report-build` formats evidence already collected as XLSX, DOCX, CSV, Markdown, or JSON. It does not run new checks or invent findings. [Report fixtures and the field contract](examples/reports/README.md) show the resulting artifacts.

For a retained native scan, `scan reanalyze` creates a new derived SQLite artifact without a network request. [Storage documentation](docs/STORAGE.md) describes retention, provenance, and the limits of offline reanalysis.

## Start a controlled project

```bash
# Create a local workspace and run its policy-bounded preparation path.
seohead project start --directory ./example-project --target https://example.com

# Inspect progress first. A policy preview is read-only; applying a change needs
# the revision returned by the preview.
seohead project status --directory ./example-project
seohead project policy --directory ./example-project
```

Preparation records its crawl scope, operator-supplied competitor candidates,
and each unavailable step. It does not invent competitors or turn a partial
crawl into a completed audit. [The project-control scenario](docs/scenarios/project-control.md)
shows the review points and [PROJECTS.md](docs/PROJECTS.md) describes the local
workspace files.

## Focused investigations

Choose the input that matches the question; a single-page check, an access log and a saved crawl answer different things.

| Question | Tools to start with | Input and useful output |
|---|---|---|
| Which URLs, links or redirects need attention? | `crawl-site`, `sitemap-crawl`, `links-check`, `redirects-check` | A site, sitemap or page URL; bounded crawl evidence, link targets and redirect observations |
| What metadata and indexing directives are present? | `parse`, `headers-check`, `robots-check` | A page URL; titles/headings, canonical declarations, response headers and robots rules |
| What appears only after JavaScript runs? | `render-check` | A page URL; raw/rendered differences, request identity and browser lab measurements |
| Is structured data or language markup inconsistent? | `schema-check`, `hreflang-check` | A page URL, or inline HTML for structured data; validation findings and the declarations behind them |
| Are pages duplicate candidates or template outliers? | `duplicate-check`, `boilerplate-report` | Retained scan bodies or an inline corpus; duplicate candidates, similarity evidence and template groups |
| What is the site's delivery environment? | `domain-profile`, `tech-detect`, `cdn-check` | A domain or URL; DNS/hosting/TLS, stack and cache observations, with unavailable sources named |
| What did clients and bots request? | `log-analyze` | An access log; request/status distributions and optional bot verification |
| What changed, and what can I hand over? | `compare-crawls`, `report-build` | Compatible audit documents or scans; comparisons and reviewable report files |

```bash
# Compare the raw response with the mobile browser representation.
seohead render-check --url https://example.com --viewport mobile

# Reuse the scan produced above without fetching those pages again.
seohead duplicate-check --scan ./scans/audit.sqlite
seohead boilerplate-report --scan ./scans/audit.sqlite

# Small existing inputs can also be passed directly as a JSON argument.
seohead duplicate-check --input '{"items":[{"id":"a","text":"Example product description"},{"id":"b","text":"Example product description"}]}'
```

`render-check` requires the `render` extra and a Playwright Chromium installation. Its timings are lab observations, not real-user Core Web Vitals. Corpus analysis requires retained HTML: omitted, disabled or unsupported bodies remain coverage gaps. Duplicate groups are evidence to review before choosing redirects or canonicals, not automatic site changes.

Use `seohead <command> --help` for calling syntax and `seohead crawl-site --config-help` for crawl settings. The [input catalogue](docs/INPUTS.md) explains what each tool consumes; the [generated tool reference](docs/TOOL_REFERENCE.md) gives arguments, defaults and execution notes. [Scenarios](docs/scenarios/README.md) show longer sequences with acceptance criteria.

## Local MCP server

Register the installed CLI as a stdio server in a compatible client:

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

For example, after an MCP client connects, these `tools/call` parameters perform the same offline duplicate check as the CLI command above:

```json
{
  "name": "seo_duplicate_check",
  "arguments": {"scan": "./scans/audit.sqlite"}
}
```

The corresponding mobile-render tool is `seo_render_check` with `url` and `viewport: "mobile"`. File-producing tools return paths so the next step can reuse the saved output.

The CLI and MCP server share handlers and registration checks. The generated [tool reference](docs/TOOL_REFERENCE.md) is the authoritative list of available commands, arguments, side effects, network use, idempotency, and provider spend. [Scenarios](docs/scenarios/README.md) connect a specialist goal to an ordered tool chain and a usable artifact. For an agent beginning an unscoped audit, start with [the control workflow](.claude/skills/control/SKILL.md).

Start with `seohead mcp --profile full` for the complete local surface. The
`audit`, `infra`, `quick-check`, and `router` profiles remove unrelated schemas
at startup. MCP progress notifications are sent only when the caller provides a
standard progress token; elapsed updates label the total as unknown and do not
claim completion. See [MCP profiles and progress](docs/MCP_PROFILES.md).

## External sources and safety boundaries

Provider integrations are optional and explicit. Yandex Cloud, Arsenkin, Yandex Metrika, DataForSEO, Search Console, and IndexNow each have their own credentials, constraints, and possible cost. DataForSEO defaults to sandbox; paid provider calls are journalled so spend can be reviewed with `spend-report`.

Network tools block private targets by default. File changes, service-path probes, bot DNS verification, provider production mode, and paid calls require explicit inputs. Image optimization writes to a separate output directory unless in-place mutation is explicitly requested; in-place mode creates backups. Secrets and client crawl data do not belong in this repository or a client report.

Native crawl and browser results do not provide field Core Web Vitals. A separate `crux-report` entry point exists, but it is credential-gated and its live access is not claimed as verified; see the [source setup notes](docs/SETUP.md). The toolkit does not provide a web-scale backlink index, a hosted multi-user dashboard, or a general-purpose content strategy. [Comparison notes](docs/COMPARISON.md) describe these boundaries before results are used in a client deliverable.

## Development

```bash
python -m pip install -e ".[dev,mcp,cluster,reports]"
ruff check .
ruff format --check .
pytest -q
seohead sf run --exports-dir examples/exports --out /tmp/seohead-report --tasks
python -m build
```

Public commands and generated references are checked in CI. Keep public prose in English, use synthetic examples only, and add a focused offline test whenever behavior changes. See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [architecture](docs/ARCHITECTURE.md).

## Licence and provenance

The Python implementation and documentation are released under the [MIT License](LICENSE). The bundled Schema.org vocabulary retains its original CC BY-SA terms. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), [PROVENANCE.md](PROVENANCE.md), [TRADEMARKS.md](TRADEMARKS.md), and [CITATION.cff](CITATION.cff) for the relevant notices and policies.
