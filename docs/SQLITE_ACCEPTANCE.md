# SQLite operator workflow and acceptance record

> **Release capacity acceptance is pending.** Do not treat this file's commands,
> a successful unit test, or earlier development profiles as a passed capacity
> claim. The final profile JSON and retained child logs are the required evidence.

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
# evidence revision do not.
seohead scan pin --input native.sqlite

# Preview first and preserve exactly the emitted JSON envelope for review.
seohead scan prune --directory . > plan.json
```

`compare-crawls` separates a finding that left a still-crawled page from a page
that disappeared. It reports incompatible scope or provenance; an absent page is
not evidence that an issue was fixed. Unpin a retained baseline explicitly with
`seohead scan pin --input native.sqlite --unpin`.

Prune preview defaults to finished, unpinned, non-partial scans older than 30
days and outside the newest five scans for the same host/configuration. Active
writers are excluded. Apply accepts the reviewed envelope and rechecks the
directory, candidate identity, metadata, lock state, and current retention rank
before unlinking anything. After reviewing `plan.json`, run `seohead scan prune
--directory . --plan plan.json --apply`. Keep scan artifacts outside the
repository.

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

Five later `PageRecord` fields may be `NULL` for older sources:
`content_frames`, `content_frames_same_origin`, `hreflang_json`,
`body_unavailable`, and `meta_refresh`. `NULL` means the observation was absent
from that source, not zero frames, an empty hreflang list, no body problem, or no
meta refresh. This field-level state is distinct from `crawl_partial` and
`corpus_partial`.

The current audit bridge is bounded to 10,000 pages, 20,000 forms, and a 64 MiB
saved audit JSON. A capture beyond those limits may retain collection evidence but
must name an unavailable audit; report output cannot manufacture one.

## Capacity acceptance: pending measured evidence

Run the release profiler from a clean working tree and retain its output outside
the repository. `--large` includes the 50,000-page case.

```bash
python scripts/profile_scan_release.py --execute --large --log-dir /tmp/seohead-scan-profile
```

The release record requires the emitted JSON, source manifest, and child
stdout/stderr logs. It must state the platform, Python/SQLite versions, source
revision, dirty state, cache condition, observed memory-budget violations,
audit-availability result, and reviewer decision. A timeout, failed child, or
missing requested case blocks acceptance; it is evidence to investigate, not a
passing measurement.

### Current blocking record

The frozen `5e4cb7d5273dbe5367c3c739de52ec66eeaae526` baseline did not pass capacity acceptance: both 10,000-page
whole-path cases reached the 900-second timeout. The 50,000-page run exposed a
batch-harness defect and then a whole-path timeout. A proven SQL-query performance
fix is being prepared separately as #602; the release profile must be rerun after
that change is merged. No replacement RSS or timing values have been accepted.

| Case | Measured release result | Acceptance state |
|---|---|---|
| 10,000 pages, 300,000 links | Pending post-#602 profile JSON and logs | Blocked |
| 10,000 pages, 1,500,000 links | Pending post-#602 profile JSON and logs | Blocked |
| 50,000 pages, 7,500,000 links | Pending post-#602 profile JSON and logs | Blocked |

Do not fill this table from a partial console transcript or a development
microprofile. Attach the retained profile evidence, then record the observed
values and a reviewer decision.
