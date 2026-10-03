"""Stable remote scan request and evidence contracts, independent of job execution."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from seohead.crawl.settings import DEFAULTS
from seohead.crawl.settings import validate as validate_crawl_config

Permission = Literal["scan:submit", "scan:list", "scan:read", "scan:cancel", "scan:result"]
JobState = Literal[
    "queued", "running", "cancel_requested", "cancelled", "finished", "partial", "failed"
]


class ScanOptions(BaseModel):
    """A deliberately bounded, path-free subset of native crawl settings."""

    model_config = ConfigDict(extra="forbid", strict=True)

    max_urls: int = Field(default=200, ge=1, le=10_000)
    max_depth: int = Field(default=5, ge=0, le=20)
    max_requests: int = Field(default=20_000, ge=1, le=100_000)
    max_crawl_seconds: int = Field(default=3_600, ge=1, le=86_400)
    concurrency: int = Field(default=1, ge=1, le=4)
    rendering_mode: Literal["raw", "js"] = "raw"

    def effective_config(self) -> dict[str, Any]:
        """Resolve against the same validated settings consumed by the CLI handler."""
        config = copy.deepcopy(DEFAULTS)
        config["limits"].update(
            max_urls=self.max_urls,
            max_depth=self.max_depth,
            max_requests=self.max_requests,
            max_crawl_seconds=self.max_crawl_seconds,
        )
        config["speed"]["concurrency"] = self.concurrency
        config["rendering"]["mode"] = self.rendering_mode
        if self.rendering_mode == "js":
            config["rendering"]["escalation"]["max_render_seconds"] = min(
                self.max_crawl_seconds, 300
            )
        validate_crawl_config(config)
        return config


class ScanSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    target_url: str = Field(min_length=10, max_length=2_048)
    options: ScanOptions = Field(default_factory=ScanOptions)

    @field_validator("target_url")
    @classmethod
    def valid_target_syntax(cls, value: str) -> str:
        # DNS, redirects, browser subresources and project allowlists are checked
        # by the mandatory target policy, never inferred from URL syntax alone.
        if value != value.strip() or any(ord(char) <= 32 for char in value):
            raise ValueError("target_url must be an absolute HTTP(S) URL without whitespace")
        try:
            parsed = urlsplit(value)
            valid_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError as exc:
            raise ValueError("target_url has an invalid authority") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or not valid_port
        ):
            raise ValueError(
                "target_url must be an absolute HTTP(S) URL without credentials or fragment"
            )
        return value

    def fingerprint(self) -> str:
        """Canonical payload digest for backend-enforced atomic idempotency."""
        body = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class JobProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    queued: int = Field(ge=0)
    inflight: int = Field(ge=0)
    done: int = Field(ge=0)
    excluded: int = Field(ge=0)
    pages: int = Field(ge=0)


class JobStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    job_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=64)
    state: JobState
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    finish_reason: str | None = Field(default=None, max_length=500)
    progress: JobProgress


class JobList(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    items: list[JobStatus]
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    has_more: bool


class ScanSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    scan_uuid: str
    format_version: Literal["scan.v1", "scan.v2"]
    source_kind: Literal["native", "legacy_import", "reanalysis"]
    parent_scan_uuid: str | None
    writer_version: str
    writer_revision: str
    evidence_revision: int = Field(ge=0)
    created_at: str
    finished_at: str | None
    lifecycle: Literal["running", "interrupted", "finished", "failed"]
    finish_reason: str
    crawl_partial: bool
    corpus_partial: bool


class FrontierCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    queued: int = Field(ge=0)
    inflight: int = Field(ge=0)
    done: int = Field(ge=0)
    excluded: int = Field(ge=0)


class ScanFrontier(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    state: Literal["available", "unavailable"]
    reason: str
    counts: FrontierCounts | None


class CommittedPageOutcomes(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status_2xx: int = Field(alias="2xx", ge=0)
    status_3xx: int = Field(alias="3xx", ge=0)
    status_4xx: int = Field(alias="4xx", ge=0)
    status_5xx: int = Field(alias="5xx", ge=0)
    other: int = Field(ge=0)
    no_response: int = Field(ge=0)


class ScanEvidence(BaseModel):
    """The same source/frontier/outcome fields returned by local scan-status."""

    model_config = ConfigDict(extra="forbid", strict=True)

    source: ScanSource
    frontier: ScanFrontier
    committed_page_outcomes: CommittedPageOutcomes


class ArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    artifact_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    media_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=0)


@dataclass
class OpenedArtifact:
    """An already-authorized open file; the response owns closing its handle."""

    handle: BinaryIO
    size_bytes: int
    filename: str
    media_type: str


class JobResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    job: JobStatus
    coverage: Literal["complete", "partial", "failed", "skipped"]
    evidence: ScanEvidence | None = None
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    audit_available: bool
    audit_reason: str = Field(max_length=500)

    @model_validator(mode="after")
    def complete_needs_evidence(self) -> JobResult:
        if self.coverage == "complete" and (self.evidence is None or not self.audit_available):
            raise ValueError("complete result requires retained scan evidence and an audit")
        if not self.audit_available and not self.audit_reason:
            raise ValueError("unavailable audit requires a reason")
        return self


class ApiErrorDetail(BaseModel):
    code: str
    message: str


class ApiErrorResponse(BaseModel):
    error: ApiErrorDetail


@dataclass(frozen=True)
class Principal:
    subject: str
    grants: dict[str, frozenset[Permission]]

    def allows(self, project_id: str, permission: Permission) -> bool:
        return permission in self.grants.get(project_id, frozenset())


@dataclass(frozen=True)
class SubmitOutcome:
    job: JobStatus
    created: bool


class JobConflict(Exception):
    """The same idempotency key was submitted with a different request body."""


class JobQueueFull(Exception):
    """The project queue has reached its configured admission limit."""


class JobBudgetExceeded(ValueError):
    """Trusted project resource limits reject the requested scan settings."""


class JobNotReady(Exception):
    """Result was requested before a terminal job outcome was recorded."""


class JobBackend(Protocol):
    """#785 must enforce project isolation and idempotency atomically at storage."""

    def submit(
        self,
        project_id: str,
        subject: str,
        idempotency_key: str,
        fingerprint: str,
        request: ScanSubmission,
        effective_config: dict[str, Any],
    ) -> SubmitOutcome: ...

    def list_jobs(self, project_id: str, offset: int, limit: int) -> list[JobStatus]: ...

    def get_job(self, project_id: str, job_id: str) -> JobStatus | None: ...

    def cancel_job(self, project_id: str, job_id: str) -> JobStatus | None: ...

    def get_result(self, project_id: str, job_id: str) -> JobResult | None: ...

    def artifact_path(self, project_id: str, job_id: str, artifact_id: str) -> Path | None: ...

    def open_artifact(
        self, project_id: str, job_id: str, artifact_id: str
    ) -> OpenedArtifact | None: ...


class RemoteTargetPolicy(Protocol):
    """#786 supplies policy and worker egress enforcement; missing policy denies submits."""

    def authorize_submission(
        self, project_id: str, target_url: str, effective_config: dict[str, Any]
    ) -> None: ...


def evidence_from_scan(input_path: str) -> ScanEvidence:
    """Build the same core scan-status evidence used by the CLI/MCP handler."""
    from seohead.storage.status import scan_status

    status = scan_status(input_path)
    return ScanEvidence.model_validate(
        {key: status[key] for key in ("source", "frontier", "committed_page_outcomes")}
    )
