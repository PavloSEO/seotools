# Usage — copy-paste commands

Install first ([SETUP.md](SETUP.md)); below, `seohead` means the venv's
`seohead` (`.venv/bin/seohead` with the venv not activated). Everything that
takes `https://example.com` goes to the network; audit mode B over
`examples/exports` is fully offline.

## The whole site in one command

```bash
# bounded sitemap-based live evidence pass + a ready Excel file (not a link-graph crawl)
seohead site-audit --url https://example.com --limit 50 --report xlsx --out audit.xlsx

# re-render an existing audit document into other formats
seohead report-build --audit audit.json --format docx --out client.docx
```

`--limit` caps the pages parsed (default 25); URLs come from the sitemap
unless `--urls` is given. Any format: `xlsx`, `docx`, `csv`, `md`, `json`.

## Native SQLite crawl (opt-in)

```bash
# Replace SOURCE_SHA with the actual crawler build's full source commit SHA
seohead crawl-site --url https://example.com --max-urls 50 --scan-out native.sqlite --producer-build SOURCE_SHA
seohead report-build --audit native.sqlite --format md --out native-report.md
```

The default crawl output remains a directory. SQLite mode keeps queue, evidence
and runtime in one transactional scan and resumes an interrupted file under the
same build/configuration. It currently retains no response bodies and requires
raw rendering, cache off and credential-free configuration. Audit creation has an explicit compatibility guard;
check `audit_available` before requesting a report. See [STORAGE.md](STORAGE.md)
for limits, provenance, interrupted-file handling and missing evidence.

## Saved scan artifact

```bash
# import a finished legacy directory; SHA is the original crawler build
python -m seohead.storage import-run RUN_DIR --out scan.sqlite --producer-build SHA
python -m seohead.storage inspect scan.sqlite

# restore the compatible three-file legacy export into a new directory
python -m seohead.storage export-run scan.sqlite --out-dir NEW_DIR

# use the stored audit through existing report, comparison, and task routes
seohead report-build --audit scan.sqlite --format md --out report.md
seohead compare-crawls --before before.sqlite --after after.sqlite
seohead sf tasks --json scan.sqlite --out tasks
```

The scan supplies its internal saved audit to these routes. An adjacent
`audit.json` that differs or cannot be read is not substituted for it; supported
structured responses carry an `input_diagnostics` notice. The MCP report and
compare tools, plus SF summary, issues, and tasks, also accept a scan path. The
issues tool retains its list response and emits a `RuntimeWarning` for that input
notice instead of adding a synthetic issue.

The export has deterministic UTF-8 `pages.jsonl` and `links.jsonl`, plus the exact
saved `audit.json`. It does not recreate bodies, raw HTML, forms, robots,
start-page evidence, sitemap responses, or a resume checkpoint. See
[STORAGE.md](STORAGE.md) for safe read-only access, version rules, and the export
contract.

## Screaming Frog crawl audit

```bash
seohead sf run --exports-dir examples/exports --out report --tasks   # mode B: ready exports
seohead sf run --crawl https://example.com --out report --tasks         # mode A: SF CLI crawls (license)
seohead sf tasks --json report/audit.json                            # backlog from an existing audit.json
seohead sf doctor                                                    # environment diagnostics
```

Output: `report/audit.json` (schema-validated by `sf/schema/audit.schema.json`)
and `report/audit.md`; `--tasks` adds `tasks.json`/`tasks.md`. Exit codes:
`0` ok, `2` critical findings found (`--fail-on critical`, for CI), `1` error.

Useful `sf run` flags: `--profile lite|full|custom`, `--config config.json`,
`--sitemap <url>`, `--auth USER:PASS` / `--auth-config` for protected
staging, `--sf-cli <path>`, `--max-urls-per-second N` (polite crawling),
`--live-recheck` (network re-check of sitemap URLs — off by default).

Prefer an SF-owned `--auth-config` profile where possible. A literal `--auth USER:PASS` value can
be exposed by shell history or process inspection, so use it only in an isolated transient
session and never paste it into logs or issue reports.

A worked example lives in [`examples/`](../examples/README.md): a synthetic
crawl with deliberate problems, its `audit.json` and the derived tasks.

## Recon: what is this domain made of

```bash
seohead domain-profile --domain example.com
seohead cdn-check      --url https://example.com
seohead tech-detect    --url https://example.com
seohead security-check --url https://example.com
seohead mirror-check   --url https://example.com
seohead ai-bots-check  --url https://example.com
```

## Single-page checks

```bash
seohead parse         --url https://example.com/product/nasos
seohead headers-check --url https://example.com
seohead hreflang-check --url https://example.com
seohead schema-check  --url https://example.com/product/nasos
seohead soft404-check --url https://example.com
seohead llms-txt-check --url https://example.com --brand "My Brand"
```

## Backlinks by your own donor list

```bash
seohead backlinks-check --target example.com --donors "https://donor1.example/page,https://donor2.example"
seohead backlinks-check --target example.com --donors-file donors.txt   # one URL per line, # comments
```

## Input conventions

Primary input for any command is `--input '<json>'` — the object is mapped
onto the handler's arguments. Frequent parameters are duplicated as flags:

```bash
seohead duplicate-check --input '{"items":[{"id":"a","text":"..."},{"id":"b","text":"..."}],"threshold":0.9}'
echo '{"url": "https://example.com"}' | seohead parse          # stdin JSON also works
```

Output is always JSON on stdout. Exit codes:

- `0` — the command completed and reports `"ok": true` (or carries no `ok` field at all).
- `1` — either the handler could not complete its check and says so in the JSON with
  `"ok": false` (a bad input, an unreachable host, a missing dependency — the tool's own
  contract in `docs/ARCHITECTURE.md` is to report this as data, never raise), or the process
  crashed before producing JSON, in which case a one-line `error: ...` goes to stderr instead
  of stdout.
- `2` — reserved for `log-scan` alone: the run it inspected contradicts itself (its own numbers
  disagree), which is a distinct signal from "the command failed" so a script can tell the two
  apart. `log-scan` still exits `1` for an ordinary failure such as a missing run directory.

An MCP client sees the same distinction via `isError` on the tool result rather than a process
exit code.

## Paid data sources (demand, SERP, traffic)

Check readiness first, then work; check what was charged afterwards:

```bash
seohead sources-doctor                                  # which secrets are present
seohead keywords-expand --phrase "underfloor heating" --limit 100
seohead keywords-exact --keywords "underfloor heating,floor screed" --region 225
seohead serp-fetch --queries "underfloor heating,floor screed" --region 213 --top 10
seohead google-keywords --keywords "seo tools"           # DataForSEO, sandbox by default
seohead google-serp --query "seo tools"
seohead metrika-counters
seohead metrika-setup --counter 12345678                 # before any traffic conclusions
seohead metrika-report --counter 12345678 --metrics "ym:s:visits" --date1 90daysAgo
seohead spend-report --since 2026-08-01
```

Money rules for this layer: [GOTCHAS.md](GOTCHAS.md).

## MCP server

```bash
seohead mcp        # stdio server, all 56 seo_* tools + 5 sf_* audit tools
```

Client config (`.mcp.json` in this repo does exactly this):

```json
{ "mcpServers": { "seohead": { "command": "seohead", "args": ["mcp"] } } }
```

Large audit results come back as file paths, never dumped inline. Network
side effects (sitemap live-recheck, path probing) stay opt-in.

## Docker

```bash
docker compose run --rm seohead sf run --exports-dir /data/exports --out /data/report --tasks
docker compose run --rm seohead headers-check --url https://example.com
```

Authorized inputs and outputs go under `./workspace` (mounted as `/data`); without arguments the
container prints `--help`. The image does not include Screaming Frog or a Playwright browser.

## Configuration

Audit behaviour lives in `config.json` (template: `config.example.json` in
the repo root): `thresholds`, `severity_overrides`,
`checks.<ID>.enabled`, `tasks_pipeline`, plus the `sf_cli` block (path
search list for the SF CLI). Numbers are not hardcoded in the source — a
threshold is changed in config, not in code.

`config.example.json` is a **complete template**, not a sparse override example: it lists
every path `DEFAULT_CONFIG` (`seohead/sf/config.py`) currently defines, at that default's own
value. `DEFAULT_CONFIG` stays the one semantic owner — the example is a projection of it,
checked for parity by `tests/test_config_example_parity.py` — so copying the file and editing
only what you mean to change is safe, and a config path added to the code without a matching
line here fails that test rather than shipping a template that silently hides a control.
