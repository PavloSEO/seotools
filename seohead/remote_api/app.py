"""Authenticated, versioned HTTP adapter over the future durable job backend.

This module creates an ASGI app but never starts a listener. The worker/storage
backend (#785) and submission/worker egress policy (#786) are required inputs;
without either, no remote crawl is admitted.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.background import BackgroundTask

from seohead.remote_api.contracts import (
    ApiErrorResponse,
    JobBackend,
    JobBudgetExceeded,
    JobConflict,
    JobList,
    JobNotReady,
    JobQueueFull,
    JobResult,
    JobStatus,
    Permission,
    Principal,
    RemoteTargetPolicy,
    ScanSubmission,
)

_PROJECT = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")
_JOB = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._~-]{8,128}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_SUBMISSION_BYTES = 16 * 1024


class TokenAuthenticator:
    """Verify high-entropy bearer tokens against configured SHA-256 digests."""

    def __init__(self, grants_by_digest: Mapping[str, Principal]):
        if not grants_by_digest or any(not _DIGEST.fullmatch(key) for key in grants_by_digest):
            raise ValueError("bearer grants require lowercase SHA-256 token digests")
        self._grants = dict(grants_by_digest)

    @staticmethod
    def digest(token: str) -> str:
        if not isinstance(token, str) or not 24 <= len(token) <= 256:
            raise ValueError("bearer token must contain 24-256 characters")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def authenticate(self, bearer: str) -> Principal | None:
        try:
            digest = self.digest(bearer)
        except ValueError:
            return None
        # Compare every configured digest, rather than leaking which key matched.
        principal = None
        for expected, candidate in self._grants.items():
            if hmac.compare_digest(digest, expected):
                principal = candidate
        return principal


class ApiFault(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


class BoundedSubmissionBody:
    """Check actual ASGI body bytes before FastAPI parses a scan submission."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not re.fullmatch(r"/api/v1/projects/[^/]+/scans", scope["path"])
        ):
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        lengths = [value for name, value in headers if name.lower() == b"content-length"]
        transfer = any(name.lower() == b"transfer-encoding" for name, _ in headers)
        if transfer or len(lengths) != 1 or not lengths[0].isdigit():
            await _error(411, "length_required", "a bounded Content-Length is required")(
                scope, receive, send
            )
            return
        if len(lengths[0]) > 5 or int(lengths[0]) > MAX_SUBMISSION_BYTES:
            await _error(413, "body_too_large", "scan request exceeds the body limit")(
                scope, receive, send
            )
            return
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > MAX_SUBMISSION_BYTES:
                await _error(413, "body_too_large", "scan request exceeds the body limit")(
                    scope, receive, send
                )
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        if received != int(lengths[0]):
            await _error(
                400, "invalid_content_length", "Content-Length disagrees with request body"
            )(scope, receive, send)
            return
        body = b"".join(chunks)
        replayed = False

        async def replay() -> dict[str, Any]:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(
    backend: JobBackend,
    authenticator: TokenAuthenticator,
    *,
    target_policy: RemoteTargetPolicy | None = None,
) -> FastAPI:
    """Build an optional remote API; caller supplies durable, isolated services.

    The backend must atomically deduplicate `(project_id, subject,
    idempotency_key)`, returning the original job for an identical fingerprint
    and raising ``JobConflict`` for a changed request. Every backend lookup must
    also scope its query by project. The API enforces both project grants and a
    second project-id check on returned records.
    """
    required = (
        "submit",
        "list_jobs",
        "get_job",
        "cancel_job",
        "get_result",
        "artifact_path",
        "open_artifact",
    )
    if backend is None or any(not callable(getattr(backend, name, None)) for name in required):
        raise ValueError("a remote job backend with all job operations is required")
    if authenticator is None or not callable(getattr(authenticator, "authenticate", None)):
        raise ValueError("a bearer authenticator is required")
    app = FastAPI(
        title="SEOHEAD Remote Scan API",
        version="1.0.0",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        description=(
            "Optional authenticated contract for self-hosted scan jobs. The durable worker "
            "backend and egress policy are separate components; this app starts no listener."
        ),
    )
    app.add_middleware(BoundedSubmissionBody)
    bearer_scheme = HTTPBearer(auto_error=False)
    router = APIRouter(
        prefix="/api/v1",
        responses={
            code: {"model": ApiErrorResponse}
            for code in (400, 401, 403, 404, 409, 411, 413, 422, 503)
        },
    )

    @app.exception_handler(ApiFault)
    async def fault_handler(_request: Request, exc: ApiFault) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default response echoes invalid input, which may contain a
        # secret-bearing URL or token. The schema remains explicit and stable.
        return _error(422, "invalid_request", "request does not match the API schema")

    def principal(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise ApiFault(401, "unauthorized", "a valid bearer token is required")
        found = authenticator.authenticate(credentials.credentials)
        if found is None:
            raise ApiFault(401, "unauthorized", "a valid bearer token is required")
        return found

    def access(project_id: str, actor: Principal, permission: Permission) -> None:
        if not _PROJECT.fullmatch(project_id) or project_id not in actor.grants:
            raise ApiFault(404, "not_found", "project or job was not found")
        if not actor.allows(project_id, permission):
            raise ApiFault(403, "forbidden", "operation is not permitted for this project")

    def visible(job: JobStatus | None, project_id: str, job_id: str | None = None) -> JobStatus:
        if job is None or job.project_id != project_id:
            raise ApiFault(404, "not_found", "project or job was not found")
        if job_id is not None and job.job_id != job_id:
            raise ApiFault(503, "backend_identity_failure", "job identity could not be verified")
        return job

    def checked_job_id(job_id: str) -> str:
        if not _JOB.fullmatch(job_id):
            raise ApiFault(404, "not_found", "project or job was not found")
        return job_id

    @router.get("/openapi.json", include_in_schema=False)
    def schema(actor: Principal = Depends(principal)) -> dict[str, Any]:
        _ = actor
        return app.openapi()

    @router.post(
        "/projects/{project_id}/scans",
        response_model=JobStatus,
        status_code=202,
        responses={200: {"model": JobStatus}},
    )
    def submit(
        project_id: str,
        submission: ScanSubmission,
        actor: Principal = Depends(principal),
        idempotency_key: str = Header(),
    ) -> JSONResponse:
        access(project_id, actor, "scan:submit")
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise ApiFault(422, "invalid_idempotency_key", "a valid Idempotency-Key is required")
        if target_policy is None:
            raise ApiFault(503, "target_policy_unavailable", "remote target policy is unavailable")
        config = submission.options.effective_config()
        try:
            target_policy.authorize_submission(project_id, submission.target_url, config)
        except JobBudgetExceeded:
            raise ApiFault(422, "budget_exceeded", "scan settings exceed project limits") from None
        except ValueError:
            raise ApiFault(
                403, "target_denied", "target is not authorized for this project"
            ) from None
        try:
            outcome = backend.submit(
                project_id,
                actor.subject,
                idempotency_key,
                submission.fingerprint(),
                submission,
                config,
            )
        except JobConflict as exc:
            raise ApiFault(
                409, "idempotency_conflict", "key was used for a different scan request"
            ) from exc
        except JobQueueFull:
            raise ApiFault(503, "queue_full", "project scan queue is full") from None
        job = visible(outcome.job, project_id)
        return JSONResponse(
            status_code=202 if outcome.created else 200,
            content=job.model_dump(mode="json"),
            headers={"Idempotent-Replay": "false" if outcome.created else "true"},
        )

    @router.get("/projects/{project_id}/scans", response_model=JobList)
    def list_jobs(
        project_id: str,
        offset: int = 0,
        limit: int = 100,
        actor: Principal = Depends(principal),
    ) -> JobList:
        access(project_id, actor, "scan:list")
        if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 100:
            raise ApiFault(
                422, "invalid_pagination", "offset or limit is outside the allowed range"
            )
        records = backend.list_jobs(project_id, offset, limit + 1)
        if any(record.project_id != project_id for record in records):
            raise ApiFault(503, "backend_scope_failure", "job listing could not be verified")
        return JobList(
            items=records[:limit], offset=offset, limit=limit, has_more=len(records) > limit
        )

    @router.get("/projects/{project_id}/scans/{job_id}", response_model=JobStatus)
    def status(project_id: str, job_id: str, actor: Principal = Depends(principal)) -> JobStatus:
        access(project_id, actor, "scan:read")
        job_id = checked_job_id(job_id)
        return visible(backend.get_job(project_id, job_id), project_id, job_id)

    @router.post("/projects/{project_id}/scans/{job_id}/cancel", response_model=JobStatus)
    def cancel(project_id: str, job_id: str, actor: Principal = Depends(principal)) -> JobStatus:
        access(project_id, actor, "scan:cancel")
        job_id = checked_job_id(job_id)
        return visible(backend.cancel_job(project_id, job_id), project_id, job_id)

    @router.get("/projects/{project_id}/scans/{job_id}/result", response_model=JobResult)
    def result(project_id: str, job_id: str, actor: Principal = Depends(principal)) -> JobResult:
        access(project_id, actor, "scan:result")
        job_id = checked_job_id(job_id)
        visible(backend.get_job(project_id, job_id), project_id, job_id)
        try:
            record = backend.get_result(project_id, job_id)
        except JobNotReady as exc:
            raise ApiFault(409, "result_not_ready", "job has no terminal result yet") from exc
        if record is None:
            raise ApiFault(404, "not_found", "project or job was not found")
        visible(record.job, project_id, job_id)
        return record

    @router.get("/projects/{project_id}/scans/{job_id}/artifacts/{artifact_id}")
    def artifact(
        project_id: str,
        job_id: str,
        artifact_id: str,
        actor: Principal = Depends(principal),
    ) -> StreamingResponse:
        access(project_id, actor, "scan:result")
        job_id = checked_job_id(job_id)
        visible(backend.get_job(project_id, job_id), project_id, job_id)
        if not re.fullmatch(r"[0-9a-f]{32}", artifact_id):
            raise ApiFault(404, "not_found", "artifact was not found")
        opened = backend.open_artifact(project_id, job_id, artifact_id)
        if opened is None:
            raise ApiFault(404, "not_found", "artifact was not found")

        def chunks():
            remaining = opened.size_bytes
            with opened.handle:
                while remaining:
                    chunk = opened.handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            chunks(),
            media_type=opened.media_type,
            headers={
                "Content-Length": str(opened.size_bytes),
                "Content-Disposition": f'attachment; filename="{opened.filename}"',
            },
            background=BackgroundTask(opened.handle.close),
        )

    app.include_router(router)
    return app
