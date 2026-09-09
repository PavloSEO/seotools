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

### Definitions, evidence, and reusable templates

The [synthetic ecommerce template](../examples/ecommerce-checklist.json) is a
reusable data-only input for the `template` argument of checklist initialization.
Pass its parsed object through CLI `--input` or the MCP `template` argument; a JSON
filename is not an inline JSON argument. Replace its synthetic site and sample
URLs with the agreed scope. Template text never runs code.

Items have stable IDs, a site/template/URL scope, dependencies, execution kind,
priority, order, and an enabled flag. Updates append definition history; they do
not erase attempts. Explicit priority choices are preserved separately from
defaults. Reconciliation picks up new or changed catalogue definitions. New
entries remain pending; changed definitions or evidence remain visibly stale.
Disabling an item keeps its history and appears in the disabled count, separately
from a reasoned `not_applicable` decision.

Execution records use `running`, `failed`, `unavailable`, `succeeded`, or
`not_applicable`. The checklist states remain `run`, `not_run`, and
`not_applicable`: failed or unavailable attempts are unfinished. Every record
requires a reason. Applicability decisions also require a reviewer; missing data
alone is not an exclusion.

Automatic completion binds a registered check to a validated SQLite artifact
under `scans/` or `reports/`, verifies the saved check outcome and site identity,
and records its digest, producer/configuration, time, and measured population.
A template requires explicit sample URLs matching that artifact's population.
Partial measurements remain limited even when the step completed. This metadata
records provenance and detects changed local bytes; it is not independent
attestation of how an artifact was produced.

Manual completion requires a named reviewer and either `signoff: true` or an
artifact with `review: "approved"`. Deliverables always require an artifact and
approved review. A file's existence, opening a skill, or discovering a finding
does not establish completed work or an implemented client-site fix. Current
status lists running, blocked, waiting-for-manual-review, deliverable-ready, and
remaining items from the same records. A previously run step can become blocked
when its dependency becomes stale; human reports show that distinction.

Writes use an exclusive `.coverage.lock`, optimistic revisions and atomic file
replacement. A concurrent writer refuses without discarding earlier work. After
an interrupted process leaves a lock, confirm that no writer is active before
removing that lock and retrying with the freshly read revision. Unknown coverage
schemas, unsafe paths and malformed history refuse rather than being migrated
on read.


### Preview and apply work priorities

```bash
seohead project priorities --directory ./example-project
seohead project priorities --directory ./example-project --apply --expected-revision 1
```

Initialize the checklist first and pass the revision returned by its current status.
Preview is offline and does not change files. Apply records the policy, saved fact
provenance, and per-item reasons in an atomic `seohead.coverage.v2` update. Reading a
v1 checklist never upgrades it; existing definitions, attempts and completion
hashes survive the explicit update. Reapplying unchanged decisions and inputs is a
byte-preserving no-op. Explicit operator/template priorities, including P1, win.

The [packaged policy](../seohead/data/project_priorities.json) assigns baseline
P0 response/robots/canonical reviews and P2 URL-style reviews; other defaults stay
P1. Saved `framework` facts raise JavaScript rendering work; `cms` facts raise PHP
and CMS URL/security work; `site_type: publisher` raises pagination/depth/sitemap
work. These are configurable specialist work priorities, not Google requirements
or changes to finding severity. Missing stack facts do not imply a detected stack.

A custom `policy` object can be supplied with `--input` JSON or through MCP. Its
format is `seohead.project-priorities.v1`; each rule has `id`, `facts`, `priority`
(P0/P1/P2), and a list of exact catalogue `items`. An empty facts object is an
unconditional rule. Other fact conditions compare declared string values without
case sensitivity; all conditions in a rule must match. Contradictory matching
rules refuse instead of silently choosing a winner. Policy text never executes
code, fetches a site, or contacts a provider.

Status and human reports display the saved priority, origin and reason. A priority
change does not complete work or invalidate a previously reviewed result; changes
to the work definition or evidence still follow the normal stale-evidence rules.
