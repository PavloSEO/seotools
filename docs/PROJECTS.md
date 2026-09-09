# Projects

A project groups independent `scan.v1` files, reports and site facts in a portable
local directory. Create it once, then keep successive scans and competitor scans
under its `scans/` directory. Each scan keeps its own site, build and configuration.

```bash
seohead project new --directory ./example-project --target https://example.test/ --label "Example"
seohead project open --directory ./example-project
seohead project status --directory ./example-project
```

The directory contains `project.json`, `scans/`, `reports/` and `log.md`.
`project.json` records format `seohead.project.v1`, integer version 1, a persistent
project UUID, UTC creation time, normalized target/host and an optional human label.
Unknown formats/versions refuse; opening never upgrades or rewrites the file.
Existing project directories are never overwritten. Move the entire directory to
preserve the relative artifact references.

Facts are optional scalar values with a name, source (`provenance`) and UTC
`observed_at` timestamp, or null when the observation time is unknown. Keep secrets
out of facts. Project data is local working material, not a public export.

Use the CLI's `--input` JSON argument (or JSON on stdin) or the corresponding MCP `seo_project_new`
parameters to pass `facts`, `template_references` and `profile_references`:

```json
{
  "directory": "./shop-project",
  "target": "https://shop.example.test/",
  "template_references": ["ecommerce/product-card", "ecommerce/category"],
  "profile_references": ["ecommerce/basic"],
  "facts": [{"name": "cms", "value": "WordPress", "provenance": "operator", "observed_at": null}]
}
```

References are portable identifiers; creation records them without loading or
executing template text. Checklist definitions, manual review/signoff and client
deliverable review are explicit local coverage operations described below; creating
or opening a project never runs them. Before checklist initialization, status says
`not_initialized`. Automatic project preparation remains `pending`; neither state
is a 0/0 result or a completed audit.

## Checklist coverage

Checklist initialization records the built-in catalogue as local definitions; it
does not run a check, skill or scenario, and makes no network request.

```bash
seohead project checklist-init --directory ./example-project
```

The returned status includes `revision`, `counts`, `views` and `items`. Pass that
revision to every update or evidence record so a concurrent writer cannot replace
newer local history. Structured definitions and records use the normal `--input`
JSON convention:

```bash
seohead project checklist-update \
  --directory ./example-project \
  --expected-revision 1 \
  --input '{"item":{"id":"custom:client-copy-review","title":"Review client copy","scope":{"site":"https://example.test/","template":null,"urls":[]},"dependencies":[],"execution_kind":"manual","priority":"P1","enabled":true,"order":900,"operation":null,"source_hash":null}}'
```

Use `project-checklist-record --directory DIRECTORY --item-id ITEM_ID
--expected-revision N --input '{"record": ...}'` to store supplied evidence or an
explicit applicability review. It validates the record and dependencies, then
returns the same completion views. Recording never executes an item or turns a
missing measurement into a clean result.

`crawl-site --project DIRECTORY` defaults an absent start URL to the project's
target and a new artifact to `DIRECTORY/scans/`. Explicit URLs and scan paths win,
so a competitor retains its own scan identity. Explicit `--out-dir` or configured
`output.dir` keeps the legacy directory route. `--resume` continues the explicit
artifact without injecting a new URL or destination. A supplied project is always
validated, including with explicit paths.

`scan-list --project DIRECTORY` and `scan-prune --project DIRECTORY` default their
history directory to the validated project's `scans/`; an explicit directory wins.
Prune previews by default and still requires an explicit reviewed plan and apply.
Individual-file inspect/snapshot/pin/reanalysis commands continue to take explicit
scan paths. The CLI and MCP share these rules. Merely opening or listing a project
does not fetch pages, run a checklist, or contact a provider. The MCP equivalents
are `seo_project_checklist_init`, `seo_project_checklist_update` and
`seo_project_checklist_record`.

`report-build --project DIRECTORY` includes the validated checklist coverage, reasons,
scope and measurement in a human report without fetching or rerunning the audit. The
original JSON audit remains unchanged, and `--out` still controls the destination.
