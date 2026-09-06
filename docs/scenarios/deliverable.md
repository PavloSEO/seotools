# Scenario 56 — From audit to deliverable: the last mile

## The question

> I have the audit. Now give me something I can send to the client and the developer.

This is the step most tooling skips, and it is the one that decides whether any of the analysis
turns into work.

## Covers

Nothing in the published catalogue. This is an operating scenario: what to do with
findings once you have them.

## The chain

**1. Start from a finished audit.**

```bash
seohead crawl-site --url https://example.com --out-dir ./run
```

**2. Verify it before it leaves the building.**

```bash
seohead log-scan --run ./run
```

Exit 2 means the run's own numbers disagree with each other. Nothing that fails this should
reach a client — every defect this toolkit has had on live sites reached a report first.

**3. Build the document the reader actually wants.**

```bash
seohead report-build --audit ./run/audit.json --format docx --out ./audit.docx
seohead report-build --audit ./run/audit.json --format xlsx --out ./audit.xlsx
seohead report-build --audit ./run/audit.json --format csv --out ./audit.csv
```

Three audiences, one source document:

| Format | For |
|---|---|
| `docx` | the client — an executive summary (counts, unavailable checks) followed by evidence, not a generated conclusion or recommendation |
| `xlsx` | the SEO — four sheets, filters, a chart |
| `csv` | separate finding, scope-evidence, and page tables — a flat export, not a grouped backlog |
| `md` | the repository, or another agent |

`report-build` only renders an audit document (`findings`+`pages`, or the SF `issues`+`pages`
schema); it does not group, prioritize, or import anywhere by itself.

**4. Build the backlog separately, if that is what is needed.** `seohead sf run --tasks` writes
`tasks.json`/`tasks.md` alongside `audit.json` — a grouped, prioritized set of work items with
its own schema. `report-build` rejects `tasks.json` (`audit document schema not recognized`):
the two are separate contracts, and CSV rows are findings, not tasks.

**5. Hand over the artifacts beside it.** A report that says "optimize your images" is a
request. A report with the optimized images attached is a delivery — see
the [images scenario](images.md).

## What comes out

```
audit.docx        the narrative, for a person who will not open JSON — summary and evidence, no conclusion
audit.xlsx        the working file, filterable by severity and section
audit.csv         findings — needs recipient-side field mapping to become tracker rows
audit.pages.csv   page facts
audit.scope.csv   crawl validity, scope, and unavailable/disabled check evidence
tasks.json/.md     the grouped, prioritized backlog, from `sf run --tasks`, not from `report-build`
run/audit.json    the machine-readable original, which the formats above cannot contradict
```

Everything is derived from one document, so the client's PDF and the developer's ticket cannot
drift apart.

## What it costs

Local file generation only. `docx` and `xlsx` need the optional report extras installed.

## What it cannot answer

- **What to do first.** Severity is not priority: a critical finding on a page nobody visits
  ranks below a warning on the money page. That ordering needs traffic data and a person.
- **How long anything will take.** Effort estimates in the backlog are shapes, not commitments.
- **Whether the client will act.** The measurable part ends at the handover.
