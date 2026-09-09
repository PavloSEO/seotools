# Command inputs

This reference is generated from `seohead.input_contracts`. Each row inventories consumed
source inputs rather than inferring them from a command's output. Forms on one row can be
required together; the notes name those relationships.

A **scan artifact** is a retained local `scan.v1` SQLite file. Read-only analysis and
history operations do not replay a crawl or promise retained page bodies. `crawl-site --resume`
is the explicit exception: it continues network collection. `duplicate-check` and
`boilerplate-report` may instead read one retained scan corpus with `--scan`.

## Operational-store decision

`scan.v1` is the authoritative retained evidence store for an artifact-mode crawl.
The HTTP cache remains sharded `http_cache.v3` files because concurrent crawl workers
can independently replace one cache entry. In live cache modes a cache miss is a safe
network fallback; a replay-mode miss remains offline and is reported. The run
journal remains an append-only local `runs.jsonl` record. Neither store is a second scan corpus,
and this decision makes no backend migration.

## Catalogue

| Command | Accepted input forms | Notes |
| --- | --- | --- |
| `provider-auth` | Inline JSON (`provider, action, grant_file, confirm`) | GSC private grant import/status/refresh; confirmed disconnect or remote revoke. No secret values returned. |
| `provider-replay` | Scan artifact (`input_path`); requires `evidence_file, out_dir` | Offline join with a private saved provider envelope; raw joins remain in restricted output. |
| `parse` | Live URL (`url`)<br>URL list (`urls`) | — |
| `crawl-site` | Live URL (`url`)<br>URL list (`urls`)<br>Local file (`urls_file`)<br>Scan artifact (`resume`)<br>Local configuration (`config`)<br>Project directory (`project`) | TXT, CSV, XLSX, or XML URL input; Resumes retained crawl evidence and continues network collection.; Defaults the target and scans/ path; explicit paths, legacy output and resume keep their route. |
| `crawl-describe-settings` | No direct input | — |
| `scan-reanalyze` | Scan artifact (`input_path`) | — |
| `log-scan` | Local directory (`run`) | — |
| `compare-crawls` | Audit document (`before, after`) | Each path may be audit JSON or scan.v1. |
| `crawl-enrich` | Audit document (`audit`); requires `external_csv`<br>Local file (`external_csv`); requires `audit` | — |
| `segment-diff` | Audit document (`audit`) | — |
| `redirects-generate` | Inline JSON (`redirects`) | — |
| `redirects-check` | Live URL (`url`) | — |
| `sitemap-crawl` | Live URL (`url`) | — |
| `images-download` | URL list (`urls`) | — |
| `images-optimize` | Local file (`files`) | — |
| `keywords-cluster` | Inline JSON (`params`) | — |
| `robots-check` | Live URL (`url`) | — |
| `headers-check` | Live URL (`url`) | — |
| `asset-weight-check` | Live URL (`url`) | — |
| `links-check` | Live URL (`url`) | — |
| `hreflang-check` | Live URL (`url`) | — |
| `domain-profile` | Domain (`domain`) | — |
| `cdn-check` | Live URL (`url`) | — |
| `tech-detect` | Live URL (`url`) | — |
| `security-check` | Live URL (`url`) | — |
| `backlinks-check` | Domain (`target`); requires `donors`<br>URL list (`donors`); requires `target`<br>Local file (`donors_file`); requires `target` | — |
| `schema-check` | Live URL (`url`)<br>Inline HTML (`html`) | — |
| `schema-build` | Live URL (`url`)<br>Inline HTML (`html`) | — |
| `duplicate-check` | Inline corpus (`items`)<br>Scan artifact (`scan`) | — |
| `ai-bots-check` | Live URL (`url`)<br>Inline text (`robots_text`) | — |
| `mirror-check` | Live URL (`url`) | — |
| `llms-txt-check` | Live URL (`url`) | — |
| `citability-check` | Live URL (`url`)<br>Inline text (`text`) | — |
| `markdown-extract` | Live URL (`url`)<br>Inline HTML (`html`) | — |
| `boilerplate-report` | Inline corpus (`pages`)<br>Scan artifact (`scan`) | — |
| `social-meta-check` | Live URL (`url`)<br>Inline JSON (`og, twitter`) | — |
| `soft404-check` | Live URL (`url`) | — |
| `log-analyze` | Local log (`path`) | — |
| `regions-check` | Live URL (`url`) | — |
| `render-check` | Live URL (`url`) | — |
| `site-audit` | Live URL (`url`)<br>URL list (`urls`) | — |
| `report-build` | Audit document (`audit`)<br>Project directory (`project`) | Audit JSON or a retained scan.v1 artifact.; Includes validated checklist coverage in human reports; JSON audit is unchanged. |
| `facts-export` | Inline JSON (`sites`) | — |
| `keywords-expand` | Provider query (`phrase`) | — |
| `keywords-seasonality` | Provider query (`phrase`) | — |
| `keywords-exact` | Provider query (`keywords`) | — |
| `serp-fetch` | Provider query (`query`)<br>Provider query (`queries`) | — |
| `spend-report` | Local log | Configured local spend log. |
| `sources-doctor` | Local configuration | — |
| `regions-tree` | Local configuration | — |
| `metrika-counters` | Local configuration | — |
| `metrika-setup` | Provider query (`counter_id`) | — |
| `metrika-report` | Provider query (`counter_id`) | — |
| `google-keywords` | Provider query (`keywords`)<br>Provider query (`seed`) | — |
| `google-serp` | Provider query (`query`) | — |
| `wayback-history` | Live URL (`url`) | — |
| `crtsh-subdomains` | Domain (`domain`) | — |
| `gsc-query` | Provider query (`site_url`) | — |
| `crux-report` | Provider query (`url`)<br>Provider query (`origin`) | — |
| `indexnow-submit` | URL list (`urls`) | — |
| `scan-list` | Local directory (`directory`)<br>Project directory (`project`) | — |
| `project-new` | Project directory (`directory`)<br>Live URL (`target`) | — |
| `project-open` | Project directory (`directory`) | — |
| `project-status` | Project directory (`directory`) | — |
| `project-checklist-init` | Project directory (`directory`)<br>Inline JSON (`template`) | Optional reusable data-only checklist template. |
| `project-checklist-update` | Project directory (`directory`)<br>Inline JSON (`item`) | Requires expected_revision for optimistic concurrency. |
| `project-checklist-record` | Project directory (`directory`)<br>Selector (`item_id`)<br>Inline JSON (`record`) | Requires expected_revision; records supplied evidence only. |
| `project-priorities` | Project directory (`directory`)<br>Inline JSON (`policy`) | Optional data-only priority policy; preview by default. Apply requires expected_revision. |
| `project-policy` | Project directory (`directory`)<br>Inline JSON (`policy`) | Optional data-only policy; preview by default. Apply requires expected_revision. |
| `project-prepare` | Project directory (`directory`)<br>Inline JSON (`template`)<br>Inline JSON (`competitors`) | Optional data-only project template.; Optional bounded competitor inputs. |
| `project-start` | Project directory (`directory`)<br>Live URL (`target`)<br>Inline JSON (`facts`)<br>Inline JSON (`template`)<br>Inline JSON (`competitors`) | Optional supplied project facts.; Optional data-only project template.; Optional bounded competitor inputs. |
| `skill-list` | No direct input | — |
| `skill-show` | Selector (`name`) | — |
| `scenario-show` | Selector (`name`) | — |
| `provider-registry` | No direct input | — |
| `provider-verify` | Provider identifier (`provider`)<br>Inline JSON (`request`) | Optional read-only target-access request. |
| `provider-collect` | Provider identifier (`provider`)<br>Selector (`operation`)<br>Inline JSON (`request`)<br>Local directory (`artifact_dir`) | Optional restricted raw-evidence location. |
| `provider-join` | Inline JSON (`crawl_pages`)<br>Inline JSON (`evidence_rows`)<br>Inline JSON (`adjustments`) | Optional evidence-backed priority adjustments. |
| `inspect-url` | Live URL (`url`)<br>Inline JSON (`checks`) | Optional bounded selection of closed investigation checks. |
| `audit-workflow` | Project directory (`directory`)<br>Selector (`action`)<br>Live URL (`target`)<br>Audit document (`audit`) | status, start, prepare, or report.; Required only for action=start.; Required only for action=report. |
| `tool-catalog` | Inline text (`query`) | Optional bounded discovery query. |
| `scan-inspect` | Scan artifact (`input_path`) | — |
| `scan-status` | Scan artifact (`input_path`) | — |
| `scan-rendered-routes` | Scan artifact (`input_path`) | — |
| `scan-snapshot` | Scan artifact (`input_path`) | — |
| `scan-pin` | Scan artifact (`input_path`) | — |
| `scan-prune` | Local directory (`directory`)<br>Local file (`plan`)<br>Project directory (`project`) | Defaults the directory to project scans/; apply remains explicit. |
| `scan-body-diff` | Scan artifact (`left, right`)<br>Selector (`url`) | Selects the logical URL within both scans. |
| `scan-evidence` | Scan artifact (`input_path`)<br>Selector (`section`) | capabilities, corpus, structured, routes, resources, or timeline. |
| `scan-extract` | Scan artifact (`input_path`)<br>Inline JSON (`rules`)<br>Selector (`url`) | Closed declarative rules over retained complete bodies.; Optional exact logical URL. |
| `scan-requeue` | Scan artifact (`input_path`)<br>Selector (`where`)<br>Local file (`backup_path`)<br>Scan artifact (`from_scan`) | Restricted saved URL/page predicate.; Mandatory new verified backup destination.; Optional alternate saved selection source. |
| `scan-import-urls` | Scan artifact (`input_path`)<br>Local file (`urls_file`)<br>Local file (`backup_path`) | Explicit TXT, CSV, XLSX, or XML URL source.; Mandatory new verified backup destination. |
| `sf run` | Live URL (`crawl`)<br>Local file (`load_crawl`)<br>Local file (`crawl_list`)<br>Local directory (`exports_dir`)<br>Local configuration (`config`) | Saved .seospider crawl; requires licensed SF CLI; URL-list file for licensed SF live traversal |
| `sf tasks` | Audit document (`audit_json`)<br>Local configuration (`config`) | — |
| `sf doctor` | Local configuration (`config`) | — |
| `sf save-config` | No direct input | — |
| `mcp` | No direct input | Starts the local stdio server; profile and progress-notification behavior are startup options. |
