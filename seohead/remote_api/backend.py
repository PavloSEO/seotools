"""Durable, project-isolated native scan jobs for the optional remote API.

This module creates neither an HTTP listener nor background workers. A service
operator constructs one backend from trusted project limits and runs ``run_one``
or ``run_forever`` in its own worker process. The existing native scan handler
remains the collector and audit owner.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seohead.recon.remote_policy import RemoteEgressPolicy
from seohead.remote_api.contracts import (
    ArtifactReference,
    JobBudgetExceeded,
    JobConflict,
    JobNotReady,
    JobProgress,
    JobQueueFull,
    JobResult,
    JobStatus,
    OpenedArtifact,
    ScanSubmission,
    SubmitOutcome,
    evidence_from_scan,
)

_PROJECT = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_JOB = re.compile(r"[0-9a-f]{32}\Z")
_TERMINAL = frozenset({"cancelled", "finished", "partial", "failed"})
_EMPTY_PROGRESS = {"queued": 0, "inflight": 0, "done": 0, "excluded": 0, "pages": 0}


class WorkerCancelled(Exception):
    """The project requested cancellation at a bounded progress boundary."""


class WorkerLeaseLost(Exception):
    """A stale worker must not finalize a job claimed by another worker."""


class WorkerResourceLimit(Exception):
    """A project disk or job resource budget was reached."""


@dataclass(frozen=True)
class RemoteProjectLimits:
    """Trusted per-project admission and worker limits, never a submit field."""

    allowed_private_hosts: frozenset[str] = frozenset()
    max_active_jobs: int = 1
    max_queued_jobs: int = 100
    max_disk_bytes: int = 12 * 1024 * 1024 * 1024
    max_urls: int = 10_000
    max_requests: int = 20_000
    max_job_seconds: int = 3_600
    max_concurrency: int = 4
    max_requests_per_origin: int = 1_000
    min_delay_seconds: float = 0.5

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 1
            for value in (
                self.max_active_jobs,
                self.max_queued_jobs,
                self.max_disk_bytes,
                self.max_urls,
                self.max_requests,
                self.max_job_seconds,
                self.max_concurrency,
                self.max_requests_per_origin,
            )
        ):
            raise ValueError("remote project limits must be positive")
        RemoteEgressPolicy(
            "validation",
            self.allowed_private_hosts,
            max_requests_per_origin=self.max_requests_per_origin,
            max_total_requests=self.max_requests,
            max_concurrency=self.max_concurrency,
            min_delay_seconds=self.min_delay_seconds,
        )

    def policy(self, project_id: str) -> RemoteEgressPolicy:
        """Return a fresh request-counter scope for exactly one job."""
        return RemoteEgressPolicy(
            project_id,
            self.allowed_private_hosts,
            max_requests_per_origin=self.max_requests_per_origin,
            max_total_requests=self.max_requests,
            max_concurrency=self.max_concurrency,
            min_delay_seconds=self.min_delay_seconds,
        )


def _stamp(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).isoformat()


def _safe_dir(path: Path) -> None:
    """Create a private directory only below a service-owned root."""
    if path.is_symlink():
        raise ValueError("remote state directory cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("remote state directory must be private")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SQLiteJobBackend:
    """SQLite queue and artifact registry implementing ``JobBackend``.

    A fresh connection and ``BEGIN IMMEDIATE`` serialize idempotent submits,
    claims and terminal transitions across processes. Network execution takes
    place outside that transaction. Leases fence stale workers; an expired
    lease becomes a named failed job while its private scan file is retained
    for operator recovery, rather than silently mixing two collections.
    """

    def __init__(
        self,
        root: str | Path,
        projects: Mapping[str, RemoteProjectLimits],
        *,
        max_active_jobs: int = 2,
        lease_seconds: float = 60.0,
        now: Callable[[], float] = time.time,
        producer_build: str | None = None,
        runner: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        if not projects or any(not _PROJECT.fullmatch(key) for key in projects):
            raise ValueError("trusted project IDs are required")
        if type(max_active_jobs) is not int or max_active_jobs < 1:
            raise ValueError("worker concurrency must be a positive integer")
        if (
            type(lease_seconds) not in (int, float)
            or not math.isfinite(lease_seconds)
            or lease_seconds < 3
        ):
            raise ValueError("worker lease must be finite and at least three seconds")
        self.projects = dict(projects)
        self.max_active_jobs = max_active_jobs
        self.lease_seconds = lease_seconds
        self.now = now
        self.producer_build = producer_build
        self.runner = runner
        self.root = Path(root).absolute()
        _safe_dir(self.root)
        self.db_path = self.root / "jobs.sqlite"
        if self.db_path.is_symlink():
            raise ValueError("remote job database cannot be a symlink")
        if self.db_path.exists() and self.db_path.stat().st_mode & 0o077:
            raise ValueError("remote job database must be private")
        with sqlite3.connect(self.db_path, timeout=30) as con:
            con.execute("PRAGMA journal_mode=WAL")
        with self._db(write=True) as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("remote job database schema version is unsupported")
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                  job_id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  subject TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  request_json TEXT NOT NULL,
                  config_json TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  started_at TEXT,
                  finished_at TEXT,
                  finish_reason TEXT,
                  progress_json TEXT NOT NULL,
                  lease_owner TEXT,
                  lease_until REAL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  audit_available INTEGER NOT NULL DEFAULT 0,
                  audit_reason TEXT NOT NULL DEFAULT '',
                  retention_state TEXT NOT NULL DEFAULT 'active',
                  UNIQUE (project_id, subject, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created_at);
                CREATE TABLE IF NOT EXISTS artifacts (
                  artifact_id TEXT PRIMARY KEY,
                  project_id TEXT NOT NULL,
                  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                  kind TEXT NOT NULL,
                  relpath TEXT NOT NULL,
                  media_type TEXT NOT NULL,
                  size_bytes INTEGER NOT NULL,
                  sha256 TEXT NOT NULL,
                  UNIQUE (job_id, kind)
                );
                CREATE TABLE IF NOT EXISTS events (
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  project_id TEXT NOT NULL,
                  job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                  at TEXT NOT NULL,
                  state TEXT NOT NULL,
                  code TEXT NOT NULL,
                  progress_json TEXT NOT NULL
                );
                """
            )
            con.execute("PRAGMA user_version=1")
        os.chmod(self.db_path, 0o600)
        self.recover_expired()

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA synchronous=FULL")
        if write:
            con.execute("BEGIN IMMEDIATE")
        try:
            yield con
            if write:
                con.commit()
        except BaseException:
            if write:
                con.rollback()
            raise
        finally:
            con.close()

    def _project(self, project_id: str) -> RemoteProjectLimits:
        if project_id not in self.projects:
            raise ValueError("project is not configured")
        return self.projects[project_id]

    def authorize_submission(
        self, project_id: str, target_url: str, effective_config: dict[str, Any]
    ) -> None:
        """Serve the API policy Protocol without sharing per-job counters."""
        limits = self._project(project_id)
        if (
            effective_config["limits"]["max_urls"] > limits.max_urls
            or effective_config["limits"]["max_requests"] > limits.max_requests
            or effective_config["limits"]["max_crawl_seconds"] > limits.max_job_seconds
            or effective_config["storage"]["max_body_store_bytes"] > limits.max_disk_bytes
        ):
            raise JobBudgetExceeded("project job resource limit exceeded")
        limits.policy(project_id).authorize_submission(project_id, target_url, effective_config)

    def _job_dir(self, project_id: str, job_id: str, *, create: bool = False) -> Path:
        if project_id not in self.projects or not _JOB.fullmatch(job_id):
            raise ValueError("job path is invalid")
        project_dir = self.root / "projects" / project_id
        job_dir = project_dir / job_id
        if create:
            _safe_dir(self.root / "projects")
            _safe_dir(project_dir)
            _safe_dir(job_dir)
        elif (
            (self.root / "projects").is_symlink()
            or project_dir.is_symlink()
            or job_dir.is_symlink()
        ):
            raise ValueError("job path cannot be a symlink")
        return job_dir

    @staticmethod
    def _status(row: sqlite3.Row) -> JobStatus:
        return JobStatus(
            job_id=row["job_id"],
            project_id=row["project_id"],
            state=row["state"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            finish_reason=row["finish_reason"],
            progress=JobProgress.model_validate(json.loads(row["progress_json"])),
        )

    @staticmethod
    def _event(con: sqlite3.Connection, row: sqlite3.Row, code: str, at: str) -> None:
        # Job events contain IDs and fixed codes only, never target URLs or
        # exception text. Retained scan evidence is not an operational log.
        con.execute(
            "INSERT INTO events(project_id,job_id,at,state,code,progress_json) VALUES(?,?,?,?,?,?)",
            (
                row["project_id"],
                row["job_id"],
                at,
                row["state"],
                code,
                row["progress_json"],
            ),
        )

    def submit(
        self,
        project_id: str,
        subject: str,
        idempotency_key: str,
        fingerprint: str,
        request: ScanSubmission,
        effective_config: dict[str, Any],
    ) -> SubmitOutcome:
        limits = self._project(project_id)
        if (
            not subject
            or not idempotency_key
            or not isinstance(request, ScanSubmission)
            or fingerprint != request.fingerprint()
            or effective_config != request.options.effective_config()
        ):
            raise ValueError("job request and resolved settings do not match")
        self.authorize_submission(project_id, request.target_url, effective_config)
        created_at = _stamp(self.now())
        with self._db(write=True) as con:
            previous = con.execute(
                "SELECT * FROM jobs WHERE project_id=? AND subject=? AND idempotency_key=?",
                (project_id, subject, idempotency_key),
            ).fetchone()
            if previous is not None:
                if previous["fingerprint"] != fingerprint:
                    raise JobConflict("idempotency key conflicts with an existing job")
                return SubmitOutcome(self._status(previous), created=False)
            queued = con.execute(
                "SELECT COUNT(*) FROM jobs WHERE project_id=? AND state='queued'",
                (project_id,),
            ).fetchone()[0]
            if queued >= limits.max_queued_jobs:
                raise JobQueueFull("project queue limit exceeded")
            job_id = uuid.uuid4().hex
            con.execute(
                """INSERT INTO jobs(job_id,project_id,subject,idempotency_key,fingerprint,
                   request_json,config_json,state,created_at,progress_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    project_id,
                    subject,
                    idempotency_key,
                    fingerprint,
                    request.model_dump_json(),
                    json.dumps(effective_config, sort_keys=True, separators=(",", ":")),
                    "queued",
                    created_at,
                    json.dumps(_EMPTY_PROGRESS, separators=(",", ":")),
                ),
            )
            row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self._event(con, row, "queued", created_at)
            return SubmitOutcome(self._status(row), created=True)

    def list_jobs(self, project_id: str, offset: int, limit: int) -> list[JobStatus]:
        self._project(project_id)
        if offset < 0 or not 1 <= limit <= 101:
            raise ValueError("invalid pagination")
        with self._db() as con:
            rows = con.execute(
                """SELECT * FROM jobs WHERE project_id=? AND retention_state='active'
                   ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?""",
                (project_id, limit, offset),
            ).fetchall()
        return [self._status(row) for row in rows]

    def get_job(self, project_id: str, job_id: str) -> JobStatus | None:
        self._project(project_id)
        with self._db() as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE project_id=? AND job_id=? AND retention_state='active'",
                (project_id, job_id),
            ).fetchone()
        return self._status(row) if row is not None else None

    def cancel_job(self, project_id: str, job_id: str) -> JobStatus | None:
        self._project(project_id)
        at = _stamp(self.now())
        with self._db(write=True) as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE project_id=? AND job_id=? AND retention_state='active'",
                (project_id, job_id),
            ).fetchone()
            if row is None:
                return None
            if row["state"] == "queued":
                con.execute(
                    "UPDATE jobs SET state='cancelled',finished_at=?,finish_reason='cancelled_before_dispatch',audit_reason='cancelled before dispatch' WHERE job_id=?",
                    (at, job_id),
                )
            elif row["state"] == "running":
                con.execute("UPDATE jobs SET state='cancel_requested' WHERE job_id=?", (job_id,))
            else:
                return self._status(row)
            updated = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self._event(con, updated, updated["state"], at)
            return self._status(updated)

    def get_result(self, project_id: str, job_id: str) -> JobResult | None:
        self._project(project_id)
        with self._db() as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE project_id=? AND job_id=? AND retention_state='active'",
                (project_id, job_id),
            ).fetchone()
            if row is None:
                return None
            if row["state"] not in _TERMINAL:
                raise JobNotReady("job has no terminal result")
            artifacts = con.execute(
                "SELECT * FROM artifacts WHERE project_id=? AND job_id=? ORDER BY kind",
                (project_id, job_id),
            ).fetchall()
        status = self._status(row)
        evidence = None
        scan_path = self._job_dir(project_id, job_id) / "scan.sqlite"
        if scan_path.is_file() and not scan_path.is_symlink():
            try:
                evidence = evidence_from_scan(str(scan_path))
            except Exception:
                evidence = None
        audit_available = bool(row["audit_available"] and evidence is not None)
        coverage = (
            "complete"
            if status.state == "finished" and audit_available
            else "partial"
            if status.state in {"partial", "cancelled"} and evidence is not None
            else "skipped"
            if status.state == "cancelled" and evidence is None
            else "failed"
        )
        references = [
            ArtifactReference(
                artifact_id=item["artifact_id"],
                kind=item["kind"],
                media_type=item["media_type"],
                size_bytes=item["size_bytes"],
            )
            for item in artifacts
            if self.artifact_path(project_id, job_id, item["artifact_id"]) is not None
        ]
        required = {"scan", "audit_json", "audit_md"} if audit_available else {"scan"}
        artifacts_complete = required <= {item.kind for item in references}
        if status.state == "finished" and audit_available and not artifacts_complete:
            coverage = "partial"
        return JobResult(
            job=status,
            coverage=coverage,
            evidence=evidence,
            artifacts=references,
            audit_available=audit_available,
            audit_reason=(
                "required report artifact unavailable"
                if audit_available and not artifacts_complete
                else ""
                if audit_available
                else row["audit_reason"] or "audit unavailable"
            ),
        )

    def _artifact_entry(
        self, project_id: str, job_id: str, artifact_id: str
    ) -> tuple[Path, sqlite3.Row] | None:
        self._project(project_id)
        with self._db() as con:
            row = con.execute(
                """SELECT a.* FROM artifacts a JOIN jobs j ON j.job_id=a.job_id
                   WHERE a.project_id=? AND a.job_id=? AND a.artifact_id=?
                   AND j.project_id=? AND j.retention_state='active'""",
                (project_id, job_id, artifact_id, project_id),
            ).fetchone()
        if row is None:
            return None
        relpath = Path(row["relpath"])
        if len(relpath.parts) != 1 or relpath.name != row["relpath"]:
            return None
        try:
            job_dir = self._job_dir(project_id, job_id)
            path = job_dir / relpath
            if (
                path.is_symlink()
                or not path.is_file()
                or path.resolve().parent != job_dir.resolve()
                or path.stat().st_size != row["size_bytes"]
            ):
                return None
        except (OSError, ValueError):
            return None
        return path, row

    def artifact_path(self, project_id: str, job_id: str, artifact_id: str) -> Path | None:
        """Resolve only a registered, digest-verified project artifact."""
        entry = self._artifact_entry(project_id, job_id, artifact_id)
        if entry is None:
            return None
        path, row = entry
        try:
            return path if _file_sha256(path) == row["sha256"] else None
        except OSError:
            return None

    def open_artifact(
        self, project_id: str, job_id: str, artifact_id: str
    ) -> OpenedArtifact | None:
        """Open before retention can unlink a file; stream only registered bytes."""
        entry = self._artifact_entry(project_id, job_id, artifact_id)
        if entry is None:
            return None
        path, row = entry
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
        handle = os.fdopen(descriptor, "rb")
        try:
            if os.fstat(descriptor).st_size != row["size_bytes"]:
                handle.close()
                return None
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != row["sha256"]:
                handle.close()
                return None
            handle.seek(0)
        except OSError:
            handle.close()
            return None
        return OpenedArtifact(
            handle=handle,
            size_bytes=row["size_bytes"],
            filename=path.name,
            media_type=row["media_type"],
        )

    def events(self, project_id: str, job_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Read bounded URL-free operational events for one authorized project."""
        if self.get_job(project_id, job_id) is None:
            return []
        if not 1 <= limit <= 1_000:
            raise ValueError("event limit must be 1..1000")
        with self._db() as con:
            rows = con.execute(
                "SELECT at,state,code,progress_json FROM events WHERE project_id=? AND job_id=? ORDER BY event_id DESC LIMIT ?",
                (project_id, job_id, limit),
            ).fetchall()
        return [
            {
                "at": row["at"],
                "state": row["state"],
                "code": row["code"],
                "progress": json.loads(row["progress_json"]),
            }
            for row in reversed(rows)
        ]

    def recover_expired(self) -> int:
        """Fence crashed workers after their lease expires, retaining private evidence."""
        now = self.now()
        at = _stamp(now)
        with self._db(write=True) as con:
            stale = con.execute(
                """SELECT * FROM jobs WHERE state IN ('running','cancel_requested')
                   AND lease_until IS NOT NULL AND lease_until<=?""",
                (now,),
            ).fetchall()
            for row in stale:
                cancelled = row["state"] == "cancel_requested"
                con.execute(
                    """UPDATE jobs SET state=?,finished_at=?,finish_reason=?,
                       audit_reason=?,lease_owner=NULL,lease_until=NULL WHERE job_id=?""",
                    (
                        "cancelled" if cancelled else "failed",
                        at,
                        "cancelled_after_worker_loss" if cancelled else "worker_lease_expired",
                        "cancelled before worker recovery"
                        if cancelled
                        else "worker stopped before a terminal scan result",
                        row["job_id"],
                    ),
                )
                updated = con.execute(
                    "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
                ).fetchone()
                self._event(con, updated, updated["finish_reason"], at)
        return len(stale)

    def _claim(self, worker_id: str) -> sqlite3.Row | None:
        if not worker_id or len(worker_id) > 64 or any(ord(char) < 33 for char in worker_id):
            raise ValueError("worker ID must be a short nonempty token")
        self.recover_expired()
        at = _stamp(self.now())
        until = self.now() + self.lease_seconds
        with self._db(write=True) as con:
            active = con.execute(
                "SELECT project_id,COUNT(*) AS n FROM jobs WHERE state IN ('running','cancel_requested') GROUP BY project_id"
            ).fetchall()
            counts = {row["project_id"]: row["n"] for row in active}
            if sum(counts.values()) >= self.max_active_jobs:
                return None
            candidates = con.execute(
                "SELECT * FROM jobs WHERE state='queued' AND retention_state='active' ORDER BY created_at,rowid"
            ).fetchall()
            for row in candidates:
                if (
                    counts.get(row["project_id"], 0)
                    >= self.projects[row["project_id"]].max_active_jobs
                ):
                    continue
                con.execute(
                    """UPDATE jobs SET state='running',started_at=COALESCE(started_at,?),
                       lease_owner=?,lease_until=?,attempts=attempts+1 WHERE job_id=?""",
                    (at, worker_id, until, row["job_id"]),
                )
                claimed = con.execute(
                    "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
                ).fetchone()
                self._event(con, claimed, "running", at)
                return claimed
        return None

    def _heartbeat(self, job_id: str, worker_id: str) -> bool:
        with self._db(write=True) as con:
            changed = con.execute(
                """UPDATE jobs SET lease_until=? WHERE job_id=? AND lease_owner=?
                   AND state IN ('running','cancel_requested')""",
                (self.now() + self.lease_seconds, job_id, worker_id),
            ).rowcount
        return changed == 1

    def _progress(self, job_id: str, worker_id: str, done: int, queued: int) -> None:
        at = _stamp(self.now())
        progress = {
            "queued": max(queued, 0),
            "inflight": 0,
            "done": max(done, 0),
            "excluded": 0,
            "pages": max(done, 0),
        }
        with self._db(write=True) as con:
            row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if (
                row is None
                or row["lease_owner"] != worker_id
                or row["state"] not in {"running", "cancel_requested"}
            ):
                raise WorkerLeaseLost()
            if row["state"] == "cancel_requested":
                raise WorkerCancelled()
            con.execute(
                "UPDATE jobs SET progress_json=?,lease_until=? WHERE job_id=?",
                (
                    json.dumps(progress, separators=(",", ":")),
                    self.now() + self.lease_seconds,
                    job_id,
                ),
            )
            updated = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            self._event(con, updated, "progress", at)

    def _check_running(self, job_id: str, worker_id: str) -> None:
        with self._db() as con:
            row = con.execute(
                "SELECT state,lease_owner FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None or row["lease_owner"] != worker_id:
            raise WorkerLeaseLost()
        if row["state"] == "cancel_requested":
            raise WorkerCancelled()

    def _project_usage(self, project_id: str) -> int:
        directory = self.root / "projects" / project_id
        if not directory.exists():
            return 0
        if directory.is_symlink():
            raise WorkerResourceLimit("project storage path is unsafe")
        return sum(
            path.stat().st_size
            for path in directory.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def _artifact_metadata(
        self, project_id: str, job_id: str, paths: Mapping[str, Path]
    ) -> list[tuple[str, str, str, str, str, str, int, str]]:
        if not paths:
            return []
        types = {
            "scan": "application/vnd.sqlite3",
            "audit_json": "application/json",
            "audit_md": "text/markdown",
        }
        job_dir = self._job_dir(project_id, job_id)
        metadata = []
        for kind, path in paths.items():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.parent != job_dir
                or kind not in types
            ):
                raise ValueError("job artifact path is unsafe")
            artifact_id = hashlib.sha256(f"{job_id}:{kind}".encode()).hexdigest()[:32]
            metadata.append(
                (
                    artifact_id,
                    project_id,
                    job_id,
                    kind,
                    path.name,
                    types[kind],
                    path.stat().st_size,
                    _file_sha256(path),
                )
            )
        return metadata

    def _finalize(
        self,
        row: sqlite3.Row,
        worker_id: str,
        *,
        state: str,
        reason: str,
        audit_available: bool,
        audit_reason: str,
        artifacts: Mapping[str, Path],
    ) -> JobStatus | None:
        at = _stamp(self.now())
        metadata = self._artifact_metadata(row["project_id"], row["job_id"], artifacts)
        with self._db(write=True) as con:
            current = con.execute(
                "SELECT state,lease_owner FROM jobs WHERE job_id=? AND project_id=?",
                (row["job_id"], row["project_id"]),
            ).fetchone()
            if current is None or current["lease_owner"] != worker_id:
                return None
            if current["state"] == "cancel_requested" and state != "cancelled":
                state, reason, audit_available, audit_reason = (
                    "cancelled",
                    "cancelled_by_operator",
                    False,
                    "cancelled before result publication",
                )
            changed = con.execute(
                """UPDATE jobs SET state=?,finished_at=?,finish_reason=?,audit_available=?,
                   audit_reason=?,lease_owner=NULL,lease_until=NULL WHERE job_id=?
                   AND project_id=? AND lease_owner=? AND state IN ('running','cancel_requested')""",
                (
                    state,
                    at,
                    reason,
                    int(audit_available),
                    audit_reason,
                    row["job_id"],
                    row["project_id"],
                    worker_id,
                ),
            ).rowcount
            if changed != 1:
                return None
            con.executemany(
                """INSERT INTO artifacts(artifact_id,project_id,job_id,kind,relpath,media_type,size_bytes,sha256)
                   VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id,kind) DO UPDATE SET
                   size_bytes=excluded.size_bytes,sha256=excluded.sha256""",
                metadata,
            )
            updated = con.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            self._event(con, updated, reason, at)
            return self._status(updated)

    def run_one(self, worker_id: str) -> JobStatus | None:
        """Claim and execute at most one job; return its terminal status.

        A process death leaves a lease that ``recover_expired`` later marks as
        failed. It never starts a second collector against an unverified partial
        scan or silently turns a failed request into a successful job.
        """
        row = self._claim(worker_id)
        if row is None:
            return None
        job_id = row["job_id"]
        project_id = row["project_id"]
        limits = self.projects[project_id]
        job_dir = self.root / "projects" / project_id / job_id
        scan_path = job_dir / "scan.sqlite"
        artifacts: dict[str, Path] = {}
        stop_heartbeat = threading.Event()
        lease_lost = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(self.lease_seconds / 3):
                try:
                    if not self._heartbeat(job_id, worker_id):
                        lease_lost.set()
                        return
                except sqlite3.Error:
                    # The next heartbeat or progress call will retry; expiry
                    # still fences the worker if the database stays down.
                    continue

        def progress(done: int, queued: int) -> None:
            if lease_lost.is_set():
                raise WorkerLeaseLost()
            self._progress(job_id, worker_id, done, queued)
            if self._project_usage(project_id) > limits.max_disk_bytes:
                raise WorkerResourceLimit("project disk quota reached")

        thread = threading.Thread(target=heartbeat, name="seohead-job-heartbeat", daemon=True)
        thread.start()
        try:
            self._job_dir(project_id, job_id, create=True)
            request = ScanSubmission.model_validate_json(row["request_json"])
            config = json.loads(row["config_json"])
            if config != request.options.effective_config():
                raise ValueError("stored effective config disagrees with the submitted request")
            if self._project_usage(project_id) >= limits.max_disk_bytes:
                raise WorkerResourceLimit("project disk quota reached")
            self._check_running(job_id, worker_id)
            runner = self.runner
            if runner is None:
                from seohead.servers.scan_handlers import crawl_site_scan

                runner = crawl_site_scan
            # A fresh policy binds the exact immutable config to both the
            # submission recheck and every HTTP/browser transport constructed
            # by the existing collector. Do not reload file/env overrides.
            with limits.policy(project_id).checked_job(project_id, request.target_url, config):
                outcome = runner(
                    request.target_url,
                    scan_out=str(scan_path),
                    settings=config,
                    producer_build=self.producer_build,
                    progress=progress,
                )
            self._check_running(job_id, worker_id)
            if lease_lost.is_set():
                raise WorkerLeaseLost()
            if scan_path.is_file():
                with sqlite3.connect(scan_path) as scan_con:
                    scan_con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                evidence_from_scan(str(scan_path))
                artifacts["scan"] = scan_path
            else:
                raise ValueError("collector returned without a retained scan")
            audit_available = bool(outcome.get("audit_available"))
            report_ok = audit_available
            if audit_available:
                from seohead.servers.handlers import report_build

                for kind, fmt, filename in (
                    ("audit_json", "json", "audit.json"),
                    ("audit_md", "md", "audit.md"),
                ):
                    final = job_dir / filename
                    temporary = job_dir / (filename + ".partial")
                    built = report_build(str(scan_path), fmt=fmt, out=str(temporary))
                    if not built.get("ok") or not temporary.is_file():
                        report_ok = False
                        break
                    os.replace(temporary, final)
                    artifacts[kind] = final
                    if self._project_usage(project_id) > limits.max_disk_bytes:
                        raise WorkerResourceLimit("project disk quota reached")
            partial = bool(outcome.get("partial")) or not report_ok
            state = "partial" if partial else "finished"
            reason = (
                "report_unavailable"
                if audit_available and not report_ok
                else "audit_unavailable"
                if not audit_available
                else "crawl_partial"
                if partial
                else "finished"
            )
            return self._finalize(
                row,
                worker_id,
                state=state,
                reason=reason,
                audit_available=audit_available,
                audit_reason="" if audit_available else "audit was unavailable after collection",
                artifacts=artifacts,
            )
        except WorkerLeaseLost:
            return None
        except WorkerCancelled:
            if scan_path.is_file() and not scan_path.is_symlink():
                artifacts["scan"] = scan_path
            return self._finalize(
                row,
                worker_id,
                state="cancelled",
                reason="cancelled_by_operator",
                audit_available=False,
                audit_reason="cancelled during collection",
                artifacts=artifacts,
            )
        except WorkerResourceLimit:
            if scan_path.is_file() and not scan_path.is_symlink():
                artifacts["scan"] = scan_path
            return self._finalize(
                row,
                worker_id,
                state="partial" if scan_path.is_file() else "failed",
                reason="project_resource_limit",
                audit_available=False,
                audit_reason="project resource budget reached",
                artifacts=artifacts,
            )
        except OSError:
            if scan_path.is_file() and not scan_path.is_symlink():
                artifacts["scan"] = scan_path
            return self._finalize(
                row,
                worker_id,
                state="failed",
                reason="storage_failure",
                audit_available=False,
                audit_reason="job storage failed before a complete audit",
                artifacts=artifacts,
            )
        except Exception:
            if scan_path.is_file() and not scan_path.is_symlink():
                artifacts["scan"] = scan_path
            return self._finalize(
                row,
                worker_id,
                state="failed",
                reason="worker_failure",
                audit_available=False,
                audit_reason="worker failed before a complete audit",
                artifacts=artifacts,
            )
        finally:
            stop_heartbeat.set()
            thread.join(timeout=self.lease_seconds / 3 + 1)

    def run_forever(self, worker_id: str, stop: threading.Event, poll_seconds: float = 1.0) -> None:
        """Run bounded jobs until explicitly stopped; lifecycle belongs to the caller."""
        if poll_seconds <= 0:
            raise ValueError("poll interval must be positive")
        while not stop.is_set():
            if self.run_one(worker_id) is None:
                stop.wait(poll_seconds)

    def prune_terminal(
        self, project_id: str, *, before: datetime, confirm: bool = False
    ) -> list[str]:
        """Explicit, restartable retention of terminal jobs for one project.

        Default is dry-run. A confirmed pass first tombstones DB records so
        artifact reads fail closed, then removes only service-owned job dirs.
        A crash between those phases leaves a tombstone the next pass resumes.
        """
        self._project(project_id)
        if before.tzinfo is None:
            raise ValueError("retention cutoff must have a timezone")
        cutoff = before.astimezone(timezone.utc).isoformat()
        with self._db(write=confirm) as con:
            rows = con.execute(
                """SELECT job_id FROM jobs WHERE project_id=? AND state IN
                   ('cancelled','finished','partial','failed') AND finished_at<?
                   ORDER BY finished_at,job_id""",
                (project_id, cutoff),
            ).fetchall()
            job_ids = [row["job_id"] for row in rows]
            if confirm:
                con.executemany(
                    "UPDATE jobs SET retention_state='deleting' WHERE project_id=? AND job_id=?",
                    [(project_id, job_id) for job_id in job_ids],
                )
        if not confirm:
            return job_ids
        for job_id in job_ids:
            job_dir = self._job_dir(project_id, job_id)
            if job_dir.exists():
                shutil.rmtree(job_dir)
            with self._db(write=True) as con:
                con.execute(
                    "DELETE FROM jobs WHERE project_id=? AND job_id=? AND retention_state='deleting'",
                    (project_id, job_id),
                )
        return job_ids
