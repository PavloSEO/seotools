---
name: sf-report
description: >-
  Produces a readable Markdown report from a Screaming Frog export: instead of merely
  "collecting and fixing," it formats an audit report that people can understand —
  health score, issues by severity, broken links with DOM location
  (source/destination/position/XPath), duplicates, heavy pages, and sitemap. Use when
  asked to "create a report from an SF export," "format a readable report," "prepare
  a client audit report," or "explain audit.json clearly." Triggers: Screaming Frog
  report, audit.md, readable report, export report, SEO report.
---

# SF Report — Readable Report from a Screaming Frog Export

Converts an SF export (CSV/XLSX) **or** an existing `audit.json` into a human-readable
`audit.md` and helps review it with the user. It uses the same engine as
`sf-analyzer`, but the goal here is not diagnostics for the sake of JSON; it is a
**clear report**.

## Trigger
- "Create a readable report from this SF export";
- "Format an audit report for the client / developers";
- "Explain `audit.json` in plain language and identify what matters."
- Frontmatter triggers: Screaming Frog report, audit.md, readable report, export
  report, SEO report.

## Anti-trigger
- No SF export directory and no `audit.json` exist yet, and only a `.seospider`
  project file is available — that needs the SF CLI or a manual CSV export first.
  Run `sf-analyzer` (`seohead sf run`) to produce the data this skill formats.
- The deliverable is a prioritized backlog ("what should I fix first," "tasks.md",
  P1/P2/P3 tickets) rather than a narrative explanation — use `sf-tasks`. It reads
  the same `audit.json` but outputs a task list, not prose.
- There is no Screaming Frog crawl at all and the ask is a bulk sitemap-based audit
  with xlsx/docx/csv output — use `site-report`, which runs its own site-level and
  page-level checks against the sitemap instead of reformatting an SF export.
- The question is about topical/silo structure (clusters, hubs, semantic coverage),
  not issue severity — use `silo-audit`, which layers structural analysis on the
  same `audit.json`.

## Preconditions
- [ ] Either a directory of SF exports (at minimum `Internal:All`) or an existing
  `audit.json` is available.
- [ ] If only a `.seospider` file exists, the SF CLI (Mode A) or a manual CSV
  export (Mode B) has been produced first — see `../sf-analyzer/SKILL.md`.
- [ ] It is known whether `audit.json` is already current for the exports at hand,
  or whether it must be (re)generated in step 2.

## Workflow
1. **Identify the input:** a directory containing SF exports (at minimum
   `Internal:All`) or an existing `audit.json`. If only a `.seospider` file is
   available, see `../sf-analyzer/SKILL.md` (this requires the SF CLI or a manual
   CSV export).
2. **Generate the report:**
   ```bash
   seohead sf run --exports-dir ./exports --out ./report --format md,json
   ```
   The generated `report/audit.md` is the readable report. If `audit.json` already
   exists, rebuild the Markdown with the same run when necessary.
3. **Review it with the user** according to the `audit.md` structure:
   - **Health summary** — health score, severity table, top issues, HTML weight;
   - **Critical** — broken links (table
     `Destination｜Source｜Anchor｜Position｜XPath`), 5xx responses, 4xx/5xx URLs in
     the sitemap, redirect loops;
   - **Warning** — duplicates (grouped), multiple H1 elements with their text,
     canonical issues, thin content, sitemap mismatches, heavy HTML (× median);
   - **Notice** — URL hygiene, meta-field lengths;
   - **Sitemap & robots** — sitemaps, mismatches, `lastmod` distribution.
4. **Highlight what matters:** start with critical issues and the most frequent
problems (`summary.by_check`); explain the impact and what to fix (the `fix_hint`
field). Do not dump the entire list — highlight the priorities and attach the
rest as a file.
5. **Attach** `report/audit.md` (+ `audit.json`) for download.

The Markdown report is for a reader, while `audit.json` remains the technical
artifact. Use the recorded URL, response status, source location, and detail as
the reproduction. Do not present a registry identifier or collector name as
proof. If an older saved audit does not contain a reproducible primitive, state
that limitation instead of re-crawling or inventing a result.

## Decision points
- **`audit.json` already exists vs. fresh exports were also handed over.** If the
  exports are newer than the existing `audit.json` (or its content looks stale),
  rebuild via `seohead sf run`; otherwise reuse it rather than re-running the audit
  engine for nothing.
- **Severity vs. frequency when ordering the narrative.** `summary.by_check` can
  show a notice-level check with hundreds of occurrences outranking a critical
  check with three. Always lead with severity (critical first), and use frequency
  only to order items *within* the same severity band, not to override it.
- **Multiple-H1 findings.** A page reporting two H1 elements is not automatically
  wrong — an accordion, a hidden tab panel, or an SVG title can trigger a false
  positive. Show the actual H1 text for both occurrences (as the report format
  requires) so the user can judge intent before it is treated as a defect.
- **"Heavy HTML (× median)."** Judge this relative to the site's own median page
  weight, not an absolute KB threshold — a template-heavy page can be normal for
  one site and anomalous for another. Flag pages that are outliers against their
  own crawl, not against a generic number.

## Definition of done
- [ ] `report/audit.md` exists and covers all five structural sections: health
  summary, critical, warning, notice, sitemap & robots.
- [ ] Every broken link in the critical section carries
  source/destination/anchor/position/XPath, not just a URL.
- [ ] Each delivered finding names a reader-facing issue and its saved
  reproduction, or explicitly says that the saved audit cannot reproduce it.
- [ ] The narrative delivered to the user leads with critical issues and cites
  `summary.by_check` for scale, rather than dumping the full issue list inline.
- [ ] `audit.md` (+ `audit.json`) is attached for download.

## Cost
This skill does not call any `seohead <command>` beyond `seohead sf run` (the same
engine `sf-analyzer` uses) to (re)build the report from data already collected by
a prior Screaming Frog crawl or export — it makes no live HTTP requests of its own
and touches no paid API. Runtime is local computation over the export/`audit.json`
size: seconds for a small site, low minutes for a large export.

## Interpretation
- Check registry and severity levels — `../sf-analyzer/reference/checks.md`.
- `audit.json` contract — `../sf-analyzer/reference/json_schema.md`.
- If tasks/a backlog are needed from the report, switch to the `sf-tasks` skill.
