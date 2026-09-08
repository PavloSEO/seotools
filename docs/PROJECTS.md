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

Use the CLI's `--input` JSON file or the corresponding MCP `seo_project_new`
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
executing template text. Custom checklist definitions, manual review/signoff,
client deliverable review and automatic project preparation are subsequent
controller stages. Until those stages exist, status says `not_initialized` and
`pending`; it never reports 0/0 or a completed audit.

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
does not fetch pages, run a checklist, or contact a provider.
