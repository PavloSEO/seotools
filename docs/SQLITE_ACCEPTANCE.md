# SQLite operator workflow and acceptance record

> **The capacity profile has been run and did not pass in full.** Two limits were
> measured and are published by name below: the 64 MiB saved-audit ceiling stops
> the whole path at 10,000 pages, and the 50,000-page case reaches the profiler's
> 900-second stage ceiling while still filling the database. Nothing here changes
> a default or promises a capacity. Do not treat this file's commands, a passing
> unit test, or an earlier development profile as an acceptance decision: the
> profile JSON, source manifest, child logs and retained snapshots are the
> evidence, and the decision is the owner's.

`scan.v1` is a local SQLite artifact for one captured crawl. It retains the saved
audit, producer provenance, and, for native scans, the evidence available for
offline reanalysis. It is not a shared catalogue, a default output format, or an
attestation system. The normal directory workflow remains the default.

Use [STORAGE.md](STORAGE.md) for the full schema and evidence contract. This page
is the short operator route for a scan used as a comparison baseline.

## Capture or import

Choose the input that exists; an import and a native capture are different
evidence paths.

```bash
# Import a legacy directory. SOURCE_SHA is the build that produced RUN_DIR.
python -m seohead.storage import-run RUN_DIR --out legacy.sqlite --producer-build SOURCE_SHA

# Native SQLite collection is opt-in. SOURCE_SHA identifies this collector build.
seohead crawl-site --url https://example.com --max-urls 50 --scan-out native.sqlite --producer-build SOURCE_SHA
```

An import preserves its saved audit, page/link records, and recorded
configuration. It has no retained HTTP body corpus, browser DOM, resource bodies,
or native resume state. A native capture records each evidence lane's state and
its configured bounds. Its default body policy is
`storage.body_mode=captured_entity_bytes`; the only alternate mode is `off`.
Read the recorded policy and capability states rather than assuming a body was
retained.

Resume an interrupted native collection by repeating the identical `crawl-site`
command with the same `--scan-out`, start URL, producer build, and effective
configuration. A changed revision, configuration, start URL, selected sitemap
input, or credential context is refused. A derived reanalysis scan never resumes
collection.

## Inspect, preserve, and derive

Run these local operations in the artifact directory.

```bash
seohead scan list --directory . --limit 100
seohead scan inspect --input native.sqlite --table pages --offset 0 --limit 100 --max-bytes 1048576

# A no-clobber Backup-API copy.
seohead scan snapshot --input native.sqlite --out snapshot.sqlite

# Reanalyze retained native evidence without a network request.
seohead scan reanalyze --input old.sqlite --out derived.sqlite --producer-build SOURCE_SHA
```

Reanalysis creates a new parented artifact. It preserves capture scope and records
the analyzer build; it does not recrawl, enlarge the corpus, substitute live data,
or replay a network request. It needs the required retained evidence, so a legacy
import normally cannot be reanalyzed. Check `audit_available` and `audit_reason`
before requesting a report.

## Report, compare, retain, and prune

The report and comparison commands use the artifact's internal saved audit. They
do not substitute a neighbouring `audit.json`, calculate new findings, or make a
network request.

```bash
seohead report-build --audit native.sqlite --format md --out native-report.md
seohead compare-crawls --before before.sqlite --after after.sqlite

# Pin a selected baseline. The container hash changes; the saved audit and
# evidence revision do not. A pin is only ever lifted explicitly.
seohead scan pin --input native.sqlite
seohead scan pin --input native.sqlite --unpin

# Preview first and preserve exactly the emitted JSON envelope for review.
seohead scan prune --directory . > plan.json

# Apply only after reading plan.json; apply accepts that reviewed envelope alone.
seohead scan prune --directory . --plan plan.json --apply
```

`compare-crawls` separates a finding that left a still-crawled page from a page
that disappeared. It reports incompatible scope or provenance; an absent page is
not evidence that an issue was fixed.

Prune preview defaults to finished, unpinned, non-partial scans older than 30
days and outside the newest five scans for the same host/configuration. Active
writers are excluded. Apply rechecks the directory, candidate identity, metadata,
lock state, and current retention rank before unlinking anything. Keep scan
artifacts outside the repository.

For `--format csv`, a successful build produces three files: the requested
findings CSV, `<name>.pages.csv`, and `<name>.scope.csv`. Its `outputs` response
lists all three. Every successful CSV build rewrites all three files; if there
are no findings, pages, or scope rows, the corresponding file contains its header
only, never stale rows from an earlier run.

## Evidence and compatibility limits

The format validates structure, cross-table constraints, and recorded hashes.
That detects accidental corruption and inconsistent state. It is not a signature,
a Merkle chain, or proof against a party that can alter the file and recompute its
checksums.

The scan header records `format_version`, writer version/revision, runtime
versions, effective configuration, lifecycle, partialness, capability states, and
retention policy. Reader and importer compatibility are explicit: a newer or
incompatible format is refused, with no automatic migration. Missing or nullable
legacy fields remain unavailable evidence; they do not become clean defaults.

Twenty later `PageRecord` observations may be `NULL` for older sources. The
current source-derived field-to-column table lives in [STORAGE.md](STORAGE.md#the-pages-projection-follows-the-prerelease-crawl-v1-pagerecord); it includes
the canonical-chain pair and `og_url`. `NULL` means the observation was absent
from that source, not a measured empty or zero value. This field-level state is
distinct from `crawl_partial` and `corpus_partial`.

The current audit bridge is bounded to 10,000 pages, 20,000 forms, and a 64 MiB
saved audit JSON. A capture beyond those limits may retain collection evidence but
must name an unavailable audit; report output cannot manufacture one.

## Capacity acceptance: the measured release record

Run the release profiler from a clean working tree and retain its output outside
the repository. `--large` includes the 50,000-page case.

```bash
python scripts/profile_scan_release.py --execute --large --log-dir /tmp/seohead-scan-profile
```

That one command produced every number below. It emits the release JSON on
stdout and retains, in the log directory, a source manifest, one stdout/stderr
log per child stage, and a Backup-API snapshot of each whole-path and each
timed-out artifact. Nothing here is a capacity promise, a default change, or a
closure of #354/#98: it is what this fixture measured on this machine.

### Run conditions

| Field | Value |
|---|---|
| Source revision | `341f63b13e28b6e1eade407a8f1c572d1d8d3fe5`, `source_dirty: false`. This is the revision the run measured, not necessarily the current tip: the retained manifest also records a SHA-256 for every profiled source file, so a later rerun that disagrees can be traced to the file that changed |
| Platform | `macOS-26.6.2-arm64-arm-64bit-Mach-O` |
| Python / SQLite | 3.14.6 / 3.53.3 |
| RSS units | macOS `ru_maxrss` is bytes; the profiler normalizes to MiB and records Linux KiB separately |
| Cache condition | Warm: one uninterrupted run, repository and interpreter already resident. Every case builds its own fixture in a fresh temporary directory, and every stage is a separate process, so no stage inherits another stage's heap |
| Network | None. The collector receives a deterministic injected transport, so no socket, DNS, HTTP, or browser request is possible |
| Runner total | 4,024.821 s wall, 232.00 MiB runner peak RSS |
| Per-stage ceiling | 900 seconds; a stage that reaches it is recorded as blocked, never as clean |
| Disk | free space fell 994,320,384 bytes over the run (157,701,369,856 -> 156,707,049,472). That is the net figure: the retained snapshots and logs stayed, while each case's temporary fixture directory was removed |
| Declared budgets | edge growth <= 128 MiB, whole-path peak <= 2048 MiB |
| Observed budget violations | none |

Two fixture kinds are measured and must not be read as one number. The `build`,
`pages`, `graph`, `audit` and `report` stages are direct-seeded SQLite
microprofiles with complete page fields. The `whole` stage is a separate
artifact: real CLI dispatch, real link discovery, real writer, saved audit,
reopen and Markdown report. Their audit digests are never compared, because
their documents differ.

### 10,000 pages, 300,000 links

| Stage | Peak RSS | Wall | Outcome |
|---|---:|---:|---|
| build (fixture) | 244.69 MiB | 83.342 s | 10,000 pages, 300,000 links written |
| pages projection | 277.47 MiB | 2.716 s | 10,000 page records |
| graph | 527.95 MiB | 27.281 s | 10,000 scores, 10,000 compositions, 10,000 anchor groups, a 4-hop path |
| audit + tasks | 491.73 MiB | 29.310 s | 30,011 findings, `checks_skipped` 40 |
| five report formats | 496.72 MiB | 143.289 s | json 32,604,706 B, csv 6,408,605 B, md 3,657,704 B, xlsx 1,317,047 B, docx 39,388 B |
| whole path | 497.42 MiB | 538.181 s | **blocked** on the saved-audit limit; see below |

Whole-path collection alone: 247.75 MiB peak, 474.704 s, 10,000 fetched pages,
10,000 pages, 300,000 links, 10,000 bodies, 0 resource references,
`finish_reason=finished`, `partial=false`. On-disk after collection: 44,150,784 B
database, 0 B WAL, 0 B shared-memory, 0 B temporary. Case wall: 837.281 s.

### 10,000 pages, 1,500,000 links

| Stage | Peak RSS | Wall | Outcome |
|---|---:|---:|---|
| build (fixture) | 244.48 MiB | 180.991 s | 10,000 pages, 1,500,000 links written |
| pages projection | 277.36 MiB | 10.018 s | 10,000 page records |
| graph | 503.94 MiB | 127.216 s | 10,000 scores, 10,000 compositions, 10,000 anchor groups |
| audit + tasks | 467.36 MiB | 149.227 s | 30,011 findings, `checks_skipped` 40 |
| five report formats | 491.98 MiB | 139.118 s | json 32,634,826 B, csv 6,408,605 B, md 3,657,704 B, xlsx 1,317,047 B, docx 39,388 B |
| whole path | 448.45 MiB | 751.653 s | **blocked** on the saved-audit limit; see below |

Whole-path collection alone: 247.61 MiB peak, 636.381 s, 10,000 fetched pages,
10,000 pages, 1,500,000 links, 10,000 bodies, 0 resource references,
`finish_reason=finished`, `partial=false`. On-disk after collection: 155,189,248 B
database, 0 B WAL, 0 B shared-memory, 0 B temporary. Case wall: 1,372.856 s.

Zero resource references is a measurement of this link-only fixture, not
evidence that a resource lane ran and found nothing on a real site.

### Fixed-page edge growth

Holding pages at 10,000 and raising links from 300,000 to 1,500,000:

| Stage | RSS change |
|---|---:|
| collector | -0.14 MiB |
| pages projection | -0.11 MiB |
| graph | -24.01 MiB |
| audit + tasks | -24.37 MiB |
| five report formats | -4.74 MiB |
| whole path | -48.97 MiB |

Every stage is inside the 128 MiB edge-growth budget, and both whole-path peaks
(497.42 MiB and 448.45 MiB) are inside the 2048 MiB target. Five of six values are
negative: process peak RSS varies with allocation and garbage collection, so read
these as "no growth measured at this edge density", not as a saving produced by
adding edges.

### Measured limit: the saved-audit ceiling stops the whole path at 10,000 pages

Both 10,000-page whole-path runs collected completely and then refused to save an
audit, with the writer's own reason:

> complete audit exceeds the saved JSON limit (67108864 bytes); capture evidence
> is retained, but this audit cannot be saved

This is the designed bound behaving correctly, and it is the reason no whole-path
report exists in this record. The whole-path fixture serves a title-and-links-only
document, so all 10,000 pages carry the full set of missing-field findings. The
direct-seeded audit stage in the same run, over the same 10,000 pages with
complete page fields, produced 30,011 findings and fit. Output volume, not page
count, is what the 64 MiB ceiling bounds. Every captured observation stayed in
the artifact; `audit_available` was false and named its reason, and no report was
manufactured from an unsaved audit.

Reproduce with the release command above, or by reading
`10000-pages-300000-links-whole.stdout.log` in the log directory.

### Measured limit: 50,000 pages does not finish inside the 900-second stage ceiling

The 50,000-page / 7,500,000-link case is **blocked**. Both of its stages reached
the profiler's 900-second per-stage ceiling while still writing to the database,
and each left a consistent Backup-API snapshot:

| Stage | Reached at the ceiling | Snapshot | `PRAGMA integrity_check` |
|---|---|---:|---|
| build (fixture) | 33,930 of 50,000 pages, 5,089,500 links, frontier fully enqueued at 50,000 | 490,233,856 B | ok |
| whole path | 14,605 pages, 2,190,750 links, 14,605 bodies, frontier 14,755 | 226,942,976 B | ok |

The whole-path run's own live progress line last read
`14,319 fetched, 14,469 known (98%), 10.8 req/s, 14m31s, scan 216.4 MB`. Case
wall: 1,808.437 s.

This is a time result, not a memory or corruption result. Neither stage exceeded
a memory budget, neither database was damaged, and both snapshots reopen. What is
unmeasured is everything after collection at this size: no 50,000-page audit,
report, comparison or graph figure exists, and none may be inferred from the
10,000-page rows above. Raising the ceiling would change what the number means,
so it was left where it was and the timeout is published instead.

Reproduce with the release command above; the retained snapshots are
`retained-50000-pages-7500000-links-build.sqlite` and
`retained-50000-pages-7500000-links-whole.sqlite`.

### Acceptance criteria: met and not met

Against the offline acceptance list in #384:

| Criterion | State |
|---|---|
| Publish fixture generation, commands, SHA, Python/SQLite versions, platform, RSS units, cold/warm condition, page/edge/resource/body counts, disk including WAL/temp/backup, and timings | **Met.** Recorded above and in the retained JSON, manifest, logs and snapshots |
| Tiny full-contract CI fixtures | **Met.** `tests/test_profile_scan_release.py` runs every stage for both densities at 3 pages, and `tests/test_scan_operator_workflow.py` runs the documented workflow offline |
| Opt-in 10k/300k and 10k/1.5M profiles | **Met for collection, graph, analysis and report; not met for the whole-path saved audit**, which is blocked by the 64 MiB ceiling named above |
| Opt-in 50k/7.5M profile within a declared <= 2048 MiB whole-process target, or a documented blocking result | **Not met; blocking result documented.** Both stages hit the 900-second ceiling. No memory budget was exceeded, but the target is unverified at this size because the run never completed |
| Fixed-page edge growth <= 128 MiB, separating page/analysis/report memory | **Met.** Six separately measured stages, all inside the budget |
| Outcomes across uninterrupted / resumed / imported / reanalyzed paths | **Partly met.** The capacity profile measures the uninterrupted path only. Resume, legacy import and offline reanalysis are covered by the offline contract suite (`tests/test_scan_native_recovery.py` and `tests/test_resume_completeness.py` for resume, `tests/test_scan_artifact.py` for legacy import, `tests/test_scan_reanalysis_integration.py` for offline reanalysis), not at 10,000 pages |
| Raw / rendered coverage separated | **Partly met.** These fixtures are raw-HTML only; rendered-representation capacity is unmeasured, and the artifacts record it as such rather than as absent |
| Every documented stdlib-reader and CLI/MCP scenario runs offline, with reports and `compare.v1` evidence compared | **Met.** `tests/test_scan_operator_workflow.py` executes the documented commands, compares all five report formats byte-for-byte between a scan and its snapshot, checks `compare.v1` output for both a snapshot and a reanalysis, and runs the SQL this page's companion publishes |
| No default change, capacity promise, #354/#98 closure or automatic migration | **Met.** SQLite capture stays opt-in; nothing in this record raises the #356 URL ceiling |

The measured whole-path ceiling at 10,000 pages and the 50,000-page timeout are
open results for owner review, not defects hidden behind a passing table. Do not
fill any row here from a console transcript; rerun the command and attach the
retained evidence.
