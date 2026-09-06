# Scan storage (`scan.v1`)

A scan artifact is one ordinary SQLite file for one crawl. Its format identifier is
`scan.v1`; its SQLite file signature is the normal `SQLite format 3\000` header,
and its identity is also recorded as `application_id=1397051208` (`SEOH`) and
`user_version=1`. A reader must require all three identifiers to agree before it
treats a file as a scan artifact.

The legacy importer packages an existing crawl directory. Native crawling can
also write directly to SQLite with `crawl-site --scan-out`; the default remains
the existing directory workflow. Both retain the existing audit/report contract.
Use one file per imported run; do not merge runs or write an imported artifact
concurrently.

## Start here

Use the storage module after it is installed with SEOHEAD Tools:

```bash
python -m seohead.storage import-run RUN_DIR --out scan.sqlite --producer-build SHA
python -m seohead.storage inspect scan.sqlite
python -m seohead.storage export-run scan.sqlite --out-dir NEW_DIR
seohead report-build --audit scan.sqlite --format md --out report.md
seohead compare-crawls --before before.sqlite --after after.sqlite
seohead sf tasks --json scan.sqlite --out tasks
```

`--producer-build` names the build that produced `RUN_DIR`. It is required because current legacy
output does not record that source build. It is provenance for the original
crawler, never the revision of the checkout that imports the directory. `inspect`
reports recorded format, provenance, lifecycle, capability states, and
partialness. `report-build`, `compare-crawls`, and `sf tasks` resolve a scan's
internal audit as their input and make no network request.

`export-run` requires a new, absent output directory. It writes only
`pages.jsonl`, `links.jsonl`, and `audit.json`; `audit.json` is published last as
the completion marker. The JSONL files are deterministic UTF-8 with sorted keys,
compact JSON, and one newline per row. Pages follow committed page order. The link export uses the existing static-plus-new-rendered-destination union in global `link_id` order; the database itself retains every raw/rendered occurrence. SQL `NULL` in a
late page field is omitted so an older source's absent field remains absent rather
than becoming a measured default. The saved audit is copied as its exact UTF-8
bytes. The export contains no response bodies, raw HTML, forms, robots state,
start-page evidence, sitemap-response corpus, or resume checkpoint.

A scan made partial only by recovery of a truncated JSONL tail cannot be represented
faithfully by these three files when its unchanged audit says the crawl was complete.
That export is refused; use the original SQLite artifact, which keeps the recovery
provenance. Export is allowed when the retained audit already records partialness.

The new directory is private while it is staged. Existing directories, files, and
symlinks are refused; a failed export removes only the files it created. Reimport
the three-file directory with the same source producer build if a legacy-shaped
input is needed again.

The import keeps the source directory unchanged. A malformed middle JSONL row,
incompatible artifact version, duplicate conflict, or inconsistent cross-file
population is refused. A single truncated final JSONL line may be recovered only
with explicit partialness. Missing fields are unavailable evidence, never clean
measurements.

For a legacy import, `scan.writer_version` is the original
`audit.tool.version`, while `scan.writer_revision` is the explicitly supplied
full 40-hex `--producer-build` source SHA. `audit.analyzer_revision` is the same
original producer build. These fields describe the source that collected and
analyzed the evidence; they do not name the importer checkout and do not make a
cryptographic attestation. The effective configuration comes from the exact
`run.crawl_config` manifest or an explicit `--config` JSON. If neither exists the
import is refused; if both exist they must agree. The importer records no separate
importer-version field because `scan.v1` has no such column.

## What is in the file

`scan` is the single run header: format, producer provenance, effective
configuration, lifecycle, capability states, retention policy, and partialness.
`urls` interns exact URL strings. `pages` and `links` are the primary crawl
observations; link occurrences retain their original order and are never
deduplicated. `forms`, `decisions`, `frontier`, `query_variants`, `resume_state`,
and `context_items` hold the native storage core's recovery and collection lanes
(empty in legacy imports). `responses`,
`documents` and `bodies` hold captured HTTP/document provenance. `resource_refs` remains reserved until resource capture lands. `audit.document_json` is the only authoritative stored audit
snapshot; report formats render that document and do not compute new findings.

Existing report and comparison routes can take a scan path directly. The MCP
`seo_report_build`, `seo_compare_crawls`, and SF audit summary, issues, and tasks
tools accept the same path. If an `audit.json` next to a scan is different or
unreadable, the shared resolver keeps using the scan's internal audit and emits an
`input_diagnostics` notice where the response shape permits it. The issue-list
tool remains a list, so it records that notice as a `RuntimeWarning` rather than
inserting a fabricated issue.

The complete DDL is [the SQL reference](../seohead/storage/scan_v1.sql). The
existing audit document remains governed by
[the audit JSON Schema](../seohead/sf/schema/audit.schema.json). The precise JSON
and cross-table validation contract is part of `scan.v1`; readers must reject
unknown/newer versions and missing required fields rather than guessing.

Raw HTTP entities, decoded documents, and rendered DOM are distinct evidence. A
rendered DOM never substitutes for raw HTTP bytes. The legacy importer retains no
bodies. Native G captures fetched HTML entity bytes and separately captured DOM
bytes when rendering provides them, with SHA-256 deduplication and `identity` or
`zlib` level-6 storage (compression is used only when smaller). A body record is
consistency evidence, not a signature or an anti-tampering claim.

Native defaults are 5 MiB decoded bytes per body, 10 GiB stored bodies per scan,
1 GiB free-space reserve, and a recorded 20 GiB history-warning threshold. History management is not yet available. Capture processes one
body at a time. The native fetch clamp is a 64 MiB hard limit: a larger response
is marked truncated rather than retained, even if a configured policy limit is
larger; rendering fails rather than silently keeping an over-limit DOM. `off`,
`no-store`, credentialed, unsupported, failed, truncated, and budget-exhausted
captures each retain their named state/reason. Native SQLite mode requires
`cache.mode=off` before collection; it never changes or deletes the old directory
cache, which remains part of the directory workflow.

`scan_decoder.v1` records entity decoding. A static document's logical URL stays
separate from the effective navigation URL; legacy-fragment navigation is recorded
as its explicit transform. Direct script and stylesheet fetching requires the explicit resource setting below.
Offline replay and reanalysis remain unavailable until child I.

## Direct script and stylesheet capture

The `resource_refs` lane records every declared script and stylesheet
occurrence for a selected static, rendered, or legacy-fragment document. It
keeps repeated declarations and uses separate occurrence ordinals per resource
kind. A single fetched URL may serve many references; references are not
deduplicated. The active extraction records `resource_inventory` under
`document:<id>` with the exact payload
`{"document_id": <id>, "state": "complete|partial|unavailable", "omitted": <n>}`.
`complete` with zero declarations means the document inventory was measured and
empty. `unavailable` means it was not measured; it is never a clean empty
inventory. A later extraction for the same logical document representation
supersedes the current coverage marker without deleting historical evidence.

Resource network collection is opt-in through `resources.fetch=true`. Its
default bounds are 20,000 HTTP attempts per scan, including retries and
redirects, and 5 MiB content-decoded bytes per resource response. Resource
attempts share the crawl's total time limit, retained-body store limit, and
free-space reserve. They do not consume the page URL limit or add resources to
the page frontier. Every redirect hop stays within the crawl start origin (scheme, host and effective port) under guarded transport. One request is made per URL and kind-specific `Accept` variant, while
every referring occurrence remains in `resource_refs`.

When fetching is disabled, declarations have the named `resources_disabled`
state. Other named omissions include `not_fetched`, scope or robots exclusion,
request/body budget exhaustion, fetch failure, and body unavailability. Resource
bodies are complete only when every declaration in the measured scope has a
successful complete response; declaration coverage is independent of whether
bodies were fetched. CSS `@import`, JavaScript modules, third-party resources,
and browser-network response capture are outside this slice. Resource inventory
and capture add no SEO findings, and they do not enable I's offline replay.

The writer records `resource_commit` with
`{"digest": <64 lowercase hex>, "requests_used": <n>}` alongside inventory
context. These are operational consistency metadata, not signatures or an
anti-tampering claim.

Credential material is re-supplied out of band. The closed `credential_context`
payload is exactly `{"verifier": null|<64 lowercase hex>, "implicit_state": bool}`:
environment references and profile paths are redacted. A changed explicit verifier
refuses resume. Changed implicit cookie or browser-profile state cannot be resumed
safely and is refused conservatively.

Legacy import's only populated `context_items` lane is
`legacy_import_provenance`; it exports no restore checkpoint or equivalent resume
state. Native collection and resume use their own validated lanes; body access,
retained-resource access, and offline reanalysis remain unavailable.

The `pages` projection follows the prerelease `crawl.v1` `PageRecord`, including
`content_frames`, `content_frames_same_origin`, ordered `hreflang_json`,
`body_unavailable`, and `meta_refresh`. The first two are parser observations about frames in the
resolved content area. `hreflang_json` preserves the document's alternate
declarations. `body_unavailable` records why collection could not parse a page
body (for example, an oversized response); it does **not** describe whether this
artifact retained that body. `meta_refresh` retains the page's declaration as
written, including a declaration with no navigation target. Retention remains unavailable in Point A.

These five later-added columns are nullable for legacy compatibility. `NULL`
means the field was absent from an older crawl record; it never means measured
zero frames, an empty hreflang list, an empty body-unavailability reason, or no
meta-refresh declaration. Current imports validate and store actual field types. Imported
older records therefore leave the `pages` capability partial independently of the
run's `crawl_partial` state.

This is a prerelease `scan.v1` schema synchronized with that current record
contract. There is no automatic migration. A prototype SQLite file with the old
DDL is refused and must be explicitly reimported from its legacy source. The
legacy importer can preserve a pre-merge 43-field JSONL record losslessly by
recording these four unknown fields as `NULL`; it does not invent defaults that
claim a measurement.

## Read safely with the Python standard library

Open the artifact read-only. Do not load extensions, execute SQL supplied by an
untrusted party, or use a writable connection merely to inspect a file. A reader
should set a finite progress handler so a costly query can be cancelled.

```python
import sqlite3

from pathlib import Path
import time

if sqlite3.sqlite_version_info < (3, 31, 0):
    raise RuntimeError("SQLite >= 3.31 is required")
path = Path("scan.sqlite").resolve()
con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
con.execute("PRAGMA trusted_schema = OFF")
deadline = time.monotonic() + 30
con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
try:
    if con.execute("PRAGMA application_id").fetchone()[0] != 1397051208:
        raise ValueError("not a SEOH artifact")
    if con.execute("PRAGMA user_version").fetchone()[0] != 1:
        raise ValueError("unsupported version; no automatic migration")
    if con.execute("SELECT format_version FROM scan").fetchone()[0] != "scan.v1":
        raise ValueError("inconsistent format version")
    row = con.execute(
        "SELECT scan_uuid, format_version, source_kind, lifecycle, crawl_partial "
        "FROM scan WHERE singleton = 1"
    ).fetchone()
    print(row)
finally:
    con.close()
```

SQLite 3.31 or newer is required for `trusted_schema=OFF`. The importer and reader
must report an actionable error if Python lacks `sqlite3` or the SQLite runtime is
too old; no extension, JSON1, or STRICT-table dependency is required.

These three read-only queries work with stock `sqlite3`:

```sql
-- Run identity and declared capability state.
SELECT scan_uuid, format_version, writer_revision, source_kind, lifecycle,
       crawl_partial, corpus_partial, capabilities_json
FROM scan WHERE singleton = 1;

-- Count pages by recorded HTTP status without loading page bodies.
SELECT status_code, COUNT(*) AS pages
FROM pages GROUP BY status_code ORDER BY pages DESC, status_code;

-- Most-linked destinations; every stored link occurrence contributes once.
SELECT u.url, COUNT(*) AS inlink_occurrences
FROM links AS l JOIN urls AS u ON u.url_id = l.destination_url_id
GROUP BY l.destination_url_id ORDER BY inlink_occurrences DESC, u.url LIMIT 20;
```

## Bounded body read

This standard-library example reads one validated complete body. Point A imports
have no body rows. Bound decompression before processing bytes; do not read every
body into memory.

```python
import hashlib
import zlib

MAX_DECODED_BYTES = 5 * 1024 * 1024


def decode_body(codec: str, data: bytes, decoded_bytes: int, sha256: str) -> bytes:
    if decoded_bytes < 0 or decoded_bytes > MAX_DECODED_BYTES:
        raise ValueError("body exceeds this reader's limit")
    if codec == "identity":
        decoded = data
    elif codec == "zlib":
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(data, MAX_DECODED_BYTES + 1)
        if (
            len(decoded) > MAX_DECODED_BYTES
            or decoder.unconsumed_tail
            or not decoder.eof
            or decoder.unused_data
        ):
            raise ValueError("invalid or oversized decoded body")
    else:
        raise ValueError("unsupported scan.v1 body codec")
    if len(decoded) != decoded_bytes or len(decoded) > MAX_DECODED_BYTES:
        raise ValueError("invalid or oversized decoded body")
    if hashlib.sha256(decoded).hexdigest() != sha256:
        raise ValueError("body digest mismatch")
    return decoded
```

For a live writer, do not copy the main `.sqlite` file alone: committed rows can be
in its WAL. The native core's `NativeScan.snapshot()` uses SQLite's Backup API to
publish a validated single-file copy; a public snapshot command is a later slice.
Until a file is finalized or snapshot-created, retain
its WAL/SHM beside it and treat lifecycle/partialness as part of the evidence.
Future format changes use an explicit migration into a new file; readers do not
silently add columns or reinterpret a newer version.

## Legacy import boundaries

The importer accepts current `crawl.v1` PageRecord/LinkEdge fields and a validated
`audit.json` schema `2.0`. Nonstandard JSON numbers (`NaN`/`Infinity`) are
refused without rewriting the old report. It refuses unknown fields instead of silently losing
them. It preserves exact audit UTF-8 bytes, page order, nullable flags and repeated
link occurrences. Only one physically truncated final JSONL record may be
recovered, with partialness recorded; it must not hide a missing audit page.

The reader validates the exact schema, foreign keys, scalar/JSON types, revision,
page population, ordering and audit digest in one read transaction. For a legacy import it
refuses populated native lanes; native files have a separate validator for their
collection state and current audit. This verifies consistency, not authenticity or protection from an
editor who can recompute a checksum. No Merkle tree, signature or key management
is provided.

Limits are 64 MiB per audit/config input, 8 MiB per JSONL record and per metadata
value, and a 30-second SQL validation/query deadline. Import only completed source
directories; inputs are hashed from the exact bytes consumed. Failed imports
publish no destination and remove their private temporary file. An existing
file or symlink is never overwritten. Reading never upgrades or migrates a file.

Report parity uses the same existing renderer on the unchanged saved audit. Tests
compare complete bytes for JSON, Markdown, both CSV files, XLSX and DOCX; Office
creation/ZIP timestamps are held equal in both test branches. Normal independent
Office builds can differ in timestamps even for the same original audit.

These are opt-in storage entry points and artifact inputs for the additive
foundation. Existing `seohead crawl-site` keeps its directory workflow. The legacy importer provides no native resume state. Migration, body retention,
resource fetch, replay, reanalysis, pruning, and SQL-backed analyzer work remain
unavailable. Audit-level findings and context already saved in the exact audit
remain available; the missing raw crawl corpus cannot be reconstructed from the
three exported files.

## Native collection (opt-in)

```bash
# SOURCE_SHA is the full commit SHA of the crawler build producing this run
seohead crawl-site --url https://example.com --max-urls 50 --scan-out native.sqlite --producer-build SOURCE_SHA
seohead report-build --audit native.sqlite --format md --out native-report.md
```

Replace `SOURCE_SHA` with the actual 40-character lowercase source SHA. A clean
source checkout can determine its own revision when `--producer-build` is omitted;
a wheel without source-revision metadata needs it explicitly. A dirty checkout
is not described as a clean build. The producing version/revision, runtime
versions, full effective configuration and result-affecting fingerprint are stored
in the scan. A different configuration or producing build refuses resume.

`python -m seohead.storage inspect native.sqlite` also validates a capture with
no audit and reports `audit_available`; report commands still require an audit.
Repeat the same command/path to resume an interrupted scan. A finished file is
immutable and cannot be overwritten or resumed for writing. Use a new destination
for a new run. `--scan-out` cannot be combined with `--out-dir` or URL-list mode;
SQLite mode currently requires `cache.mode=off`. Credentials are re-supplied out
of band and resumability is governed by the redacted credential context above. The MCP
`seo_crawl_site` exposes the same `scan_out` and `producer_build` parameters.
Response bodies are **not retained**, including the raw start-page HTML used
transiently by the first-run rendering gate.

The collector keeps only its bounded worker batch and page observations in
Python; page/link/form records, seen identities, queue, query variants and
recovery state live in SQLite. Parser caps retain at most 20,000 link observations and 2,000 form observations
per page. Full valid link totals remain PageRecord measurements;
omitted occurrences/discovery produce an explicit partial state. Seeds and query
reservations retain legacy ordering, including the different seen/query order
for sitemap seeds and ordinary links. Interrupted work can be fetched again;
committed pages are not issued again.

The existing analyzer now consumes SQL graph projections without rebuilding
`all_inlinks` or a complete Python edge list. Pages and final audit/report data
still materialize: the native audit admits at most 10,000 pages and 20,000 forms.
Above those output-population limits, the scan is retained with
`audit_available=false` and a recorded reason. There is no separate 100,000-link
admission limit. These are finite operating bounds, not a memory guarantee for
arbitrary field lengths or finding populations. Sitemap capture streams membership
in chunks of 256, with the existing per-root expansion limits; it no longer
allocates the full declared-URL list before an admission check.

The complete serialized audit also has the existing 64 MiB reader limit.
The writer checks that exact size before replacing an audit. If it cannot fit,
the result explicitly says `audit_available=false`; all captured observations
remain intact. No findings are truncated and the reader limit is not raised.

A resumed scan without retained static start-page HTML has a named no-audit guard
until body retention is available; it cannot invent a clean rendering verdict.
An already-current audit can be reused when only file finalization was blocked.
Current audits feed the existing report, comparison and task APIs; no automatic
`audit.json` or task sidecars are written in SQLite mode. A zero configured delay
is recorded as the schema-supported rate string `unbounded`, never JSON Infinity.

### Collector capacity measurement

The offline profiler uses 10,000 seeded pages, generated HTML, and injected
transport without sockets. On macOS 26.6.2 arm64, Python 3.14.6 and SQLite 3.53.3:

| Stored links | Peak collector RSS | Collection time |
|---:|---:|---:|
| 300,000 (30/page) | 241.05 MiB | 61.69 s |
| 1,500,000 (150/page) | 241.95 MiB | 244.55 s |

The fixed-page increase was 0.90 MiB; both runs met the 256 MiB peak and 128 MiB
increase budgets. Counts and ordered link digests were checked from SQLite. macOS
reports `ru_maxrss` in bytes; the profiler normalizes Linux KiB separately. This
measures collection only: the analyzer compatibility bridge, report rendering,
sitemap expansion, large page fields and retained bodies are outside this result.
No larger default crawl ceiling follows from these measurements.

Reproduce with `python scripts/profile_scan_collector.py`.
The profiler emits progress and JSON with platform/runtime versions and source
file hashes. It keeps no full edge graph in Python. See
[the profiler](../scripts/profile_scan_collector.py) for the exact fixture.

### SQL graph and sitemap projections

Native scan graph projections now read a validated `scan.v1` artifact through a
read-only SQLite snapshot. They use bounded cursors and connection-local,
file-backed temporary tables; they do not rebuild the stored graph as a Python
edge list, set, or DataFrame. The recorded destination text remains an edge
fact. A destination enters the composition population only when its existing
crawler canonical identity belongs to a recorded crawled page; repeated DOM
occurrences remain stored, while composition alone counts one distinct
`(destination, source, position)` triple. Unclassified occurrences remain
unmeasured rather than becoming content links. Destinations outside the
crawled-page population are disclosed separately and do not create an internal
linking conclusion.

Sitemap membership records the selected **expanded root** after the existing
sitemap tool follows a root and its nested indexes. It does not claim that a
member came directly from one XML document line. Member order is the run-wide
post-expansion normalization/deduplication order across selected roots, while
the original retained URL spelling remains the report value. A selected root
with a complete expansion and zero members is a measured empty sitemap. A scan
with no saved selected roots is `unavailable` rather than a zero-sized sitemap;
partial root expansion is also unavailable for a reconciliation conclusion.

The same SF analyzer checks now consume a narrow graph access contract for
generic anchors, inlink composition, PageRank, and discovery paths. SQL keeps
the edge inventory, uncrawled internal graph nodes, scores and BFS predecessors
in file-backed temporary tables. Existing check eligibility, thresholds,
messages and output ordering remain shared with the SF export path. Only
requested score lookups, bounded location examples and emitted paths enter
Python. Native links still have no measured resource Type, so the resource
inventory check retains its existing named skip. Response bodies, raw HTML,
rendered DOM, JavaScript and CSS bodies remain unavailable.

#### SQL graph profile

The E profiler constructs a deterministic 10,000-page native-writer fixture
with classified `content`/`footer` links and a complete 10,000-member selected
expanded root. Build, graph, and sitemap readers run in separate
subprocesses, so graph/sitemap RSS does not inherit fixture-construction RSS.
It measures no `all_inlinks` DataFrame, report rendering, or body lane. Run
`python scripts/profile_scan_graph.py --profile` to reproduce the JSON and
incremental progress output.

On macOS 26.6.2 arm64 with Python 3.14.6 and SQLite 3.53.3, the separate
reader subprocesses produced these results. macOS `ru_maxrss` is bytes; the
script normalizes it to MiB and records Linux KiB separately. Each graph digest
matched a one-row-at-a-time expected digest; each sitemap run had exactly 10,000
`in_sitemap_and_linked` members and no other bucket.

| Stored links | Graph reader peak RSS | Graph seconds | Composition rows / distinct triples | 10,000-member sitemap reader peak RSS / seconds |
|---:|---:|---:|---:|---:|
| 300,000 | 247.97 MiB | 1.819 | 10,000 / 300,000 | 248.28 MiB / 2.680 |
| 1,500,000 | 248.22 MiB | 6.608 | 10,000 / 1,500,000 | 248.30 MiB / 14.068 |

The graph-reader peak increased 0.25 MiB over the fixed-page edge-density
change. This is a cursor-projection measurement, not an end-to-end crawler,
analyzer, report, body, or arbitrary-site capacity claim.

### Saved-scan analysis and report capacity

Run `python scripts/profile_scan_analysis.py --pages 10000` for the full F
profile. It uses complete synthetic PageRecords and a balanced graph at
300,000 and 1,500,000 links. Only collection and the sitemap protocol request
are injected: the whole stage runs CLI dispatch, saves the native audit,
reopens the file, and creates a Markdown report. Separate subprocesses measure
page projection, graph calculations, audit plus task derivation, and all five
report formats. Every run contains 10,000 pages, 10,000 score/composition
results, and 20,011 audit findings; counts and outcome digests are recorded.

Observed on macOS 26.6.2 arm64, Python 3.14.6 and SQLite 3.53.3:

| Stored links | Pages RSS | Graph RSS | Audit/tasks RSS | Five report formats RSS | CLI + saved scan + Markdown RSS |
|---:|---:|---:|---:|---:|---:|
| 300,000 | 273.61 MiB | 525.98 MiB | 453.44 MiB | 459.44 MiB | 551.39 MiB |
| 1,500,000 | 273.69 MiB | 469.06 MiB | 453.62 MiB | 419.39 MiB | 505.06 MiB |

The audit/task increase was 0.18 MiB; graph and whole-route peaks decreased in
this pair. Each is below the 128 MiB edge-growth budget, and both whole-route
peaks are below 1 GiB. Process peaks vary with allocation and garbage collection;
these measurements are evidence for this fixture, not arbitrary-site guarantees.
The whole CLI stages took 19.163 and 69.425 seconds respectively.

Output volume remains a separate bound. The earlier blank-field ring fixture
generated 129,874 findings and 116,868,817 bytes of audit JSON before pretty
printing, beyond the 64 MiB saved-audit limit. It is an output-stress case, not
a passing saved-scan CLI example. Narrow the capture scope when the full audit
cannot fit; the oversized-audit result must never be mistaken for a clean report.

## Native transactions and recovery

`seohead.storage.native_scan.NativeScan` is an internal Python storage API for a
native `scan.v1` file used by the opt-in collector. Its
`create`, `open`, `enqueue`, `claim`, `commit_page`, `recover_inflight`, `interrupt`,
`inspect`, `finish_without_audit`, `resume_or_finalize`, and `snapshot` operations
are exercised with offline page observations.
Fetch workers must return their bounded results to the one writer; they must not
share its connection or start additional writers. The directory crawler and
legacy-import validator remain separate; CLI/MCP select native capture explicitly.

A committed page unit contains its page projection, ordered duplicate link/form
occurrences, decisions, accepted query variants, frontier updates, runtime state,
and one evidence revision. A rejected query variant keeps its exact URL decision
and excluded frontier identity without consuming its query reservation. Frontier
order and committed-page order are separate contiguous sequences. A retry of an
already committed lease succeeds only when its whole input digest matches; a
changed retry is refused. This digest is idempotency bookkeeping, not a signature
or protection against an editor who can recompute it.

The core records the producing build, complete validated effective configuration,
result-affecting configuration fingerprint, runtime versions, and unavailable
body/resource/reanalysis states. A different start URL or configuration refuses
resume. Missing, foreign, newer, malformed, and terminal files cannot become live
writers. Validation occurs read-only before writer setup; reading never upgrades
the file. `NativeScan.inspect()` validates native metadata and references in one
bounded read transaction. Report/compare/task routes require a current audit
whose hash, revision, build, configuration, and page population agree with the
scan. A capture with no admitted audit is inspectable evidence, never a clean
report.

Only the process holding the lifetime POSIX advisory lock may requeue orphaned
inflight URLs. The lock lives at `<scan>.writer.lock`, separate from SQLite's own
database locks on macOS/Linux. Keep its inode in place; unlinking/recreating an
active lock could let another writer acquire a different inode. A second writer
is refused. A committed URL stays done; fetched but uncommitted work may be
fetched again after a crash. Exactly-once HTTP execution is not promised.

Live writes use WAL, FULL synchronous mode, foreign keys, a finite busy timeout,
an 8 MiB SQLite page cache, and file-backed temporary work on a local filesystem.
Transaction failure rolls back the complete pending unit. If the filesystem also
prevents saving an error state, the caller still receives the failure and retains
the last committed artifact; no success is inferred from an unchanged header.
Initial queue chunks and each page observation lane are limited to 20,000 items
and 8 MiB of JSON; the page record is limited to 8 MiB and the complete commit
input to 64 MiB. Oversized input is refused atomically. The collector adapter must
record any deliberately retained prefix as partial evidence; this core does not
silently truncate observations. A WAL above 64 MiB triggers a bounded checkpoint
before accepting more work; a blocking reader causes explicit backpressure.
Do not actively write in a network filesystem or cloud-synchronized directory.

`snapshot()` admits space for the logical database, observed WAL/SHM, a temporary
margin, and a 1 GiB reserve. It uses a bounded Backup API operation, validates the
copy, closes it in DELETE journal mode, fsyncs it, and publishes with a no-clobber
hard link. Existing destinations and symlinks are refused. Only the operation's
own temporary output is cleaned on failure. A snapshot of an unfinished scan
remains unfinished and has no audit, even though it is a portable single file.
The default backup deadline is 60 seconds. Finalization has a separate 10-second
deadline; a reader that blocks checkpointing leaves `lifecycle=interrupted` and
`finish_reason=finalization_blocked`, preserving collection completeness separately.
After the reader closes, `resume_or_finalize()` with an empty frontier only
finalizes the file. WAL/SHM files are never manually deleted.

The current native lanes use these versioned context rows:

| Kind / key | Exact `scan_context.v1` payload |
|---|---|
| `robots_blocked_url` / `url:<url_id>` | `{"url_id":positive_integer,"token":string,"policy":"respect or report_only"}` |
| `seed_url` / `url:<url_id>` | `{"url_id":positive_integer,"depth":0,"source":"sitemap"}` |
| `robots_summary` / `run` | Policy/token, fetch state, nullable response ID, note, and parsed groups/sitemaps; the exact closed payload is in [the format contract](https://github.com/PavloSEO/seotools/issues/372). |
| `native_commit` / queue ordinal as decimal text | `{"digest":lowercase_sha256}` |
| `sitemap_declaration` / `ordinal:<root_ordinal>` | `{"sitemap_url_id":positive_integer,"source":"explicit or robots","ordinal":nonnegative_integer}`. This names one selected expanded root, including its nested sitemap indexes. |
| `sitemap_declared_url` / `sitemap:<sitemap_url_id>:ordinal:<global_ordinal>` | `{"sitemap_url_id":positive_integer,"url_id":positive_integer,"ordinal":nonnegative_integer}`. `ordinal` is run-wide post-expansion normalization/deduplication order across roots, never a direct XML-line claim. |
| `sitemap_fetch_summary` / `url:<sitemap_url_id>` | `{"sitemap_url_id":positive_integer,"response_ids":[positive_integer],"complete":boolean,"reason":string}`. Current E capture has `response_ids: []`; response provenance belongs to the later response/body lane. |

Unknown context kinds/keys and extra payload fields are refused. A robots context
references this scan's URL and measured policy; `native_commit` belongs to a done
frontier row. Sitemap reconciliation stops at the first selected root whose
summary is partial or failed: it returns an unavailable state rather than a
shortened replay that could label absent membership as an orphan or a clean empty
sitemap. Exclusion counts derive from decision occurrences. Raw start-page HTML
is not hidden in a context row: it requires the later document/body lane.
Response, document, body, and resource retention, rendering updates, and offline
replay remain unavailable.
