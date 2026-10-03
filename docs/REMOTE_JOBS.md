# Durable jobs for the optional remote scan API

`seohead.remote_api.backend.SQLiteJobBackend` is the optional, self-hosted
queue and artifact backend for the `/api/v1` contract. Constructing it creates
a private state directory and SQLite database. It does not start a listener or
worker. A service operator must explicitly create the ASGI app and run worker
processes. Local CLI and stdio MCP use neither component.

The backend is configured with trusted project limits, not with fields from a
submission. Its own `authorize_submission` method can be injected as the API's
mandatory target policy. It resolves and stores the exact `ScanOptions`
effective config; a worker reads that immutable JSON snapshot, verifies it
against the request, creates a **fresh** `RemoteEgressPolicy` for the job and
calls the existing `crawl_site_scan(settings=...)` handler inside
`checked_job`. It never re-reads crawler defaults, a user config file or proxy
environment variables at dispatch. This preserves the same target, DNS,
redirect and browser-resource guard through the whole run.

Each submit is atomically keyed by `(project_id, subject, Idempotency-Key)`.
An identical request returns its existing job; a changed request conflicts.
Claims use SQLite `BEGIN IMMEDIATE`, a lease owner, expiry and heartbeat.
Global and per-project active-job slots prevent workers from claiming beyond
configured concurrency. A process death leaves a lease; after expiry,
`recover_expired()` records a named failed outcome and retains its private
scan for operator inspection. Queued jobs survive restart. The backend does
not automatically replay an interrupted scan under uncertain state.

Cancellation is immediate for queued jobs. A running job switches to
`cancel_requested`; its progress callback or final publication boundary stops
the worker and records `cancelled`. Network calls have the native crawl
timeouts, so cancellation of one in-flight request is not instantaneous.
The API status and result preserve complete/partial/failed/skipped coverage;
missing audit or report artifacts cannot be presented as complete.

Artifacts live under private, service-owned project and job directories.
The scan SQLite file and, when the audit is available, JSON and Markdown
reports are registered with opaque IDs, sizes and SHA-256 digests. The authenticated
`GET /api/v1/projects/{project_id}/scans/{job_id}/artifacts/{artifact_id}` route
streams a registered file only after `scan:result` authorization and a
project/job-scoped lookup. It hashes the opened file descriptor before sending
bytes; a same-size replacement or symlink is unavailable and reduces complete
coverage to partial. The request never supplies a filesystem path.
Operational events contain job IDs, fixed state/reason codes and numeric
progress; target URLs, tokens, request headers and exception text stay out of
them. Retained scan/report content is separate, project-protected evidence.

Project limits include queued/active jobs, URL count, physical request count,
requests per origin, concurrency, crawl duration, delay floor and disk bytes.
Over-budget submissions and a full queue return explicit bounded API errors.
The native scan's own body and free-space limits still apply. CPU and RAM
hard limits belong to the deployment's isolated worker process/container;
this Python backend does not claim OS-level containment. Proxy routes and
alternate browser backends remain rejected for queued jobs until their
egress can be validated under the same policy.

Retention is explicit. `prune_terminal(project_id, before=...)` lists eligible
terminal jobs without deleting anything; `confirm=True` tombstones and removes
only that project's owned job directories and database rows. A crash during
pruning leaves tombstones for the next confirmed pass. No periodic deletion is
started automatically.

Synthetic integration coverage is in `tests/test_remote_backend.py`. It
exercises the authenticated API through an in-process client, then executes
the actual native collector against a fake DNS/HTTP transport; it neither
opens a public listener nor contacts a customer site. Deployment, TLS,
service supervision, hard CPU/RAM containment and backup/restore are separate
from this backend and must be demonstrated in the self-hosted service profile.
