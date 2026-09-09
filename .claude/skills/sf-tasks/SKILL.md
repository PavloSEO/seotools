---
name: sf-tasks
description: >-
  Builds a prioritized task backlog from a Screaming Frog export or audit.json, with
  a configurable pipeline (which severities to include, grouping by issue type or
  URL, P1/P2/P3 priorities, effort estimates, and limits). For broken links, each
  task includes the location (source/destination/position/XPath). Use when asked to
  "create tasks from the export," "build an audit backlog," "prepare developer
  tasks," or "create a task table from audit.json." Triggers: tasks from an audit,
  SEO backlog, tasks.md, task pipeline, Screaming Frog Scrum backlog, what to fix
  from an export.
---

# SF Tasks — Task Backlog from an Audit

Converts an audit (`audit.json`) into a ready-to-use **backlog**: `tasks.json`
(machine-readable) and `tasks.md` (a checklist organized by priority). The pipeline
is configurable: what to include, how to group items, and which priorities and
effort estimates to assign.

## Trigger
- "Create tasks from this export / audit";
- "Build a backlog for developers" or "What should be fixed first?";
- "I need tasks.md / a prioritized task list."
- Frontmatter triggers: tasks from an audit, SEO backlog, tasks.md, task pipeline,
  Screaming Frog Scrum backlog, what to fix from an export.

## Anti-trigger
- No `audit.json` and no SF exports exist yet — there is nothing to turn into
  tasks. Run `sf-analyzer` (`seohead sf run`) first, or add `--tasks` to that same
  run to skip this skill entirely.
- The ask is a human-readable narrative for a client review, not a checklist for
  developers — use `sf-report`, which formats the same `audit.json` as prose
  instead of prioritized tickets.
- The ask is about topical/silo architecture gaps (missing hub pages, clusters,
  semantic coverage) rather than crawl-detected issues — use `silo-audit`; its
  gap list is a different kind of backlog, not derived from `tasks_pipeline`.
- The site has no Screaming Frog crawl at all and the backlog should come from a
  sitemap-based bulk audit instead — use `site-report` (`seohead site-audit`),
  which has its own report/CSV export path, not `tasks.json`.

## Preconditions
- [ ] An `audit.json` exists, or a directory of SF exports exists from which one
  can be generated in step 1.
- [ ] If a custom pipeline is wanted (severities, grouping, priority/effort maps,
  limits), a `config.json` with a `tasks_pipeline` section is ready — otherwise
  the defaults (all severities, grouped by check) are used.

## Workflow
1. **Obtain the audit.** If `audit.json` is available, go directly to step 3. If
   only SF exports are available, run the audit first:
   ```bash
   seohead sf run --exports-dir ./exports --out ./report
   ```
2. **(Optional) Configure the pipeline** in `config.json` → `tasks_pipeline`:
   - `include_severities` — which levels to include (all by default);
   - `group_by` — `check` (one task per issue type) or `issue` (by URL);
   - `priority_map` — severity → `P1/P2/P3`; `effort_map` — severity → effort;
   - `max_urls_per_task`, `min_occurrences`, `include_checks`/`exclude_checks`.
3. **Build the tasks:**
   ```bash
   seohead sf tasks --json ./report/audit.json --out ./report --config config.json
   # Or generate them together with the audit in a single command:
   seohead sf run --exports-dir ./exports --out ./report --tasks
   ```
   Through MCP, use the `sf_audit_tasks {json_path, out, config?}` tool.
4. **Deliver the result.** `tasks.md` is a checklist organized by `P1/P2/P3` with
a "how to fix" field; for broken links, it includes a
`destination ← source · position · XPath` list. Discuss P1 first, estimate the
total scope (`summary.by_priority`), and attach `tasks.json` for the tracker.

`tasks.json` keeps machine IDs for a tracker. `tasks.md` is reader-facing: its
task title and reproduction must use the recorded URL/status/location rather
than a registry ID or a collector name. If the saved audit has no such primitive
evidence, state that reproduction is unavailable; do not create new network work.

## Decision points
- **`group_by: check` vs. `group_by: issue`.** Grouping by check produces one task
  per issue type (e.g. "fix 40 broken links") that a single developer session can
  clear in bulk; grouping by issue/URL produces one task per affected page, useful
  when different pages have different owners. Pick based on who will pick up the
  ticket, not by default.
- **Default `priority_map`/`effort_map` vs. a custom one.** The severity from
  `audit.json` (critical/warning/notice) is a technical-impact judgment, not a
  business-priority one — a "notice" that affects 500 URLs may deserve `P1` on
  effort/reach grounds even though its technical severity is low. Override the
  maps in `config.json` when the client's priorities diverge from raw severity.
- **`min_occurrences` and `max_urls_per_task`.** On a large crawl, leaving these
  at defaults can produce a backlog that is technically complete but too long to
  action. Raise `min_occurrences` to suppress one-off findings, and cap
  `max_urls_per_task` when a single task would otherwise list hundreds of URLs
  the tracker cannot render usefully.
- **Re-running after fixes.** Because groups and priorities are deterministic,
  a second `tasks.json` can be diffed against the first to check what was
  actually closed — decide whether the user wants that diff before re-running
  the full pipeline from scratch.

## Definition of done
- [ ] `tasks.json` and `tasks.md` both exist and were built from the same
  `audit.json`/config inputs.
- [ ] Every task carries `id`, `check`, `priority`, `severity`, `effort`, `title`,
  `fix_hint`, and `affected_count`; broken-link tasks additionally carry
  `broken_links[]` with source/position/XPath.
- [ ] `tasks.md` is organized by `P1/P2/P3`, and P1 was discussed with the user
  first along with the total scope from `summary.by_priority`.
- [ ] Every reader-facing task includes saved reproduction evidence or an honest
  unavailable statement; the machine-only IDs remain in `tasks.json`.
- [ ] `tasks.json` is attached for the tracker.

## Cost
No new network requests: this skill only calls `seohead sf tasks` (or `sf run
--tasks`) over an `audit.json`/export set already produced by a prior
`sf-analyzer` crawl or export — local computation, no paid API. If `audit.json`
still needs to be generated in step 1, that cost is `sf-analyzer`'s (see its own
skill), not this one's.

## Task Format (`tasks.json`)
`id`, `check`, `priority` (P1/P2/P3), `severity`, `effort`, `title`, `fix_hint`,
`affected_count`, `occurrences`, `urls[]` (subject to the limit), and, for links,
`broken_links[]` with location details. Groups and priorities are deterministic, so
the backlog can be diffed between runs.

## Related Skills
- If no audit has been generated from `.seospider`/exports yet, use the
  `sf-analyzer` skill.
- If a human-readable review is needed instead of tasks, use the `sf-report` skill.
- Check registry (everything that can be detected) —
  `../sf-analyzer/reference/checks.md`.
