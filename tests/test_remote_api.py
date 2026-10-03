"""Offline HTTP contract and authorization tests for the optional remote API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from seohead.remote_api.app import TokenAuthenticator, create_app
from seohead.remote_api.contracts import (
    JobConflict,
    JobNotReady,
    JobProgress,
    JobResult,
    JobStatus,
    Principal,
    ScanSubmission,
    SubmitOutcome,
    evidence_from_scan,
)
from seohead.servers.history_handlers import scan_status
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _metadata, _record, _runtime

TOKEN = "synthetic-token-with-ample-randomness-1"
OTHER_TOKEN = "synthetic-token-with-ample-randomness-2"
KEY = "synthetic-request-key"
PATH = "/api/v1/projects/alpha/scans"


class FakeBackend:
    """Synthetic adapter that exercises the #785 storage interface, not a worker."""

    def __init__(self):
        self.jobs = {}
        self.keys = {}
        self.results = {}
        self.last_config = None

    def submit(self, project_id, subject, idempotency_key, fingerprint, request, effective_config):
        identity = (project_id, subject, idempotency_key)
        if identity in self.keys:
            old_digest, job_id = self.keys[identity]
            if old_digest != fingerprint:
                raise JobConflict
            return SubmitOutcome(self.jobs[job_id], False)
        self.last_config = effective_config
        job = JobStatus(
            job_id=str(uuid4()),
            project_id=project_id,
            state="queued",
            created_at=datetime.now(timezone.utc),
            progress=JobProgress(queued=0, inflight=0, done=0, excluded=0, pages=0),
        )
        self.jobs[job.job_id] = job
        self.keys[identity] = (fingerprint, job.job_id)
        return SubmitOutcome(job, True)

    def list_jobs(self, project_id, offset, limit):
        return [job for job in self.jobs.values() if job.project_id == project_id][
            offset : offset + limit
        ]

    def get_job(self, project_id, job_id):
        job = self.jobs.get(job_id)
        return job if job and job.project_id == project_id else None

    def cancel_job(self, project_id, job_id):
        job = self.get_job(project_id, job_id)
        if job is not None:
            job = job.model_copy(update={"state": "cancel_requested"})
            self.jobs[job_id] = job
        return job

    def get_result(self, project_id, job_id):
        job = self.get_job(project_id, job_id)
        if job is None:
            return None
        if job.state in {"queued", "running", "cancel_requested"}:
            raise JobNotReady
        return self.results.get(job_id)

    def artifact_path(self, project_id, job_id, artifact_id):
        return None

    def open_artifact(self, project_id, job_id, artifact_id):
        return None


class FakePolicy:
    def __init__(self):
        self.calls = []

    def authorize_submission(self, project_id, target_url, effective_config):
        self.calls.append((project_id, target_url, effective_config))
        if target_url.endswith("/denied"):
            raise ValueError("synthetic internal decision; never return this to a caller")


def client(*, policy=True, permissions=None):
    backend = FakeBackend()
    target_policy = FakePolicy() if policy else None
    all_permissions = frozenset(
        {"scan:submit", "scan:list", "scan:read", "scan:cancel", "scan:result"}
    )
    grants = {
        TokenAuthenticator.digest(TOKEN): Principal(
            "operator-a", {"alpha": permissions if permissions is not None else all_permissions}
        ),
        TokenAuthenticator.digest(OTHER_TOKEN): Principal("operator-b", {"beta": all_permissions}),
    }
    return (
        TestClient(create_app(backend, TokenAuthenticator(grants), target_policy=target_policy)),
        backend,
        target_policy,
    )


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def submit(api, *, url="https://example.test/", key=KEY, options=None, token=TOKEN):
    return api.post(
        PATH,
        headers={**auth(token), "Idempotency-Key": key},
        json={"target_url": url, "options": options or {}},
    )


def test_openapi_requires_auth_and_describes_versioned_contracts():
    api, _, _ = client()
    assert api.get("/api/v1/openapi.json").status_code == 401
    assert (
        api.get(
            "/api/v1/openapi.json", headers=auth("wrong-token-value-with-ample-length")
        ).status_code
        == 401
    )
    schema = api.get("/api/v1/openapi.json", headers=auth()).json()
    assert schema["info"]["version"] == "1.0.0"
    route = "/api/v1/projects/{project_id}/scans"
    for suffix in ("", "/{job_id}", "/{job_id}/cancel", "/{job_id}/result"):
        assert route + suffix in schema["paths"]
    assert schema["components"]["securitySchemes"]["HTTPBearer"]
    assert "target_url" in schema["components"]["schemas"]["ScanSubmission"]["properties"]
    assert {"200", "202"} <= set(schema["paths"][route]["post"]["responses"])
    assert any(
        item["name"] == "idempotency-key" and item["required"]
        for item in schema["paths"][route]["post"]["parameters"]
    )


def test_submit_denies_anonymous_foreign_and_missing_policy_before_backend():
    api, backend, policy = client()
    assert api.post(PATH, json={"target_url": "https://example.test/"}).status_code == 401
    foreign = submit(api, token=OTHER_TOKEN)
    assert foreign.status_code == 404
    assert not backend.jobs and not policy.calls

    api, backend, _ = client(policy=False)
    denied = submit(api)
    assert denied.status_code == 503
    assert denied.json()["error"]["code"] == "target_policy_unavailable"
    assert not backend.jobs


def test_missing_or_incomplete_backend_is_rejected_when_app_is_created():
    grants = {TokenAuthenticator.digest(TOKEN): Principal("operator-a", {"alpha": frozenset()})}
    auth = TokenAuthenticator(grants)
    with pytest.raises(ValueError, match="job backend"):
        create_app(None, auth, target_policy=FakePolicy())
    with pytest.raises(ValueError, match="job backend"):
        create_app(object(), auth, target_policy=FakePolicy())


def test_submit_uses_validated_cli_settings_and_policy_before_enqueue():
    api, backend, policy = client()
    response = submit(
        api,
        options={
            "max_urls": 50,
            "max_requests": 75,
            "max_crawl_seconds": 120,
            "concurrency": 2,
            "rendering_mode": "raw",
        },
    )
    assert response.status_code == 202
    assert response.headers["Idempotent-Replay"] == "false"
    assert response.json()["state"] == "queued"
    assert backend.last_config["limits"]["max_urls"] == 50
    assert backend.last_config["limits"]["max_requests"] == 75
    assert backend.last_config["speed"]["concurrency"] == 2
    assert policy.calls[0][:2] == ("alpha", "https://example.test/")
    assert TOKEN not in response.text and "/Users/" not in response.text


def test_js_request_keeps_a_bounded_render_budget_for_worker_policy():
    api, backend, policy = client()
    response = submit(
        api,
        options={"rendering_mode": "js", "max_crawl_seconds": 120},
    )
    assert response.status_code == 202
    assert backend.last_config["rendering"]["mode"] == "js"
    assert backend.last_config["rendering"]["escalation"]["max_render_seconds"] == 120
    assert policy.calls[0][2]["rendering"]["mode"] == "js"


def test_idempotent_replay_and_changed_request_conflict():
    api, backend, _ = client()
    first = submit(api)
    second = submit(api)
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.headers["Idempotent-Replay"] == "true"
    assert len(backend.jobs) == 1
    conflict = submit(api, url="https://example.test/changed")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_request_boundary_hides_secret_values_and_rejects_unsafe_inputs():
    api, backend, policy = client()
    bad = submit(api, url="https://user:secret@example.test/")
    assert bad.status_code == 422 and "secret" not in bad.text
    assert submit(api, url="http://example.test/#fragment").status_code == 422
    assert submit(api, url="file:///etc/passwd").status_code == 422
    assert submit(api, options={"max_urls": 50_001}).status_code == 422
    assert submit(api, options={"http": {"credential_headers": []}}).status_code == 422
    denied = submit(api, url="https://example.test/denied")
    assert denied.status_code == 403
    assert "internal decision" not in denied.text
    assert not backend.jobs and len(policy.calls) == 1


def test_body_size_and_idempotency_key_are_bounded():
    api, backend, _ = client()
    assert submit(api, key="bad key").status_code == 422
    assert (
        api.post(PATH, headers=auth(), json={"target_url": "https://example.test/"}).status_code
        == 422
    )
    oversized = api.post(
        PATH,
        headers={**auth(), "Idempotency-Key": KEY},
        content=b"{" + b"x" * 20_000 + b"}",
    )
    assert oversized.status_code == 413
    understated = api.post(
        PATH,
        headers={**auth(), "Idempotency-Key": KEY, "Content-Length": "1"},
        content=b"{" + b"x" * 20_000 + b"}",
    )
    assert understated.status_code == 413
    assert understated.json()["error"]["code"] == "body_too_large"
    assert not backend.jobs


def test_actual_asgi_chunks_are_bounded_even_with_false_content_length():
    api, backend, _ = client()
    messages = iter(
        (
            {"type": "http.request", "body": b"{" + b"x" * 8_000, "more_body": True},
            {"type": "http.request", "body": b"x" * 9_000, "more_body": False},
        )
    )
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": PATH,
        "headers": [(b"content-length", b"1")],
        "query_string": b"",
        "http_version": "1.1",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 12345),
    }
    asyncio.run(api.app(scope, receive, send))
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413
    assert not backend.jobs


def test_each_operation_has_a_project_scope_and_foreign_jobs_do_not_leak():
    api, backend, _ = client(permissions=frozenset({"scan:submit"}))
    job_id = submit(api).json()["job_id"]
    assert api.get(PATH, headers=auth()).status_code == 403
    assert api.get(f"{PATH}/{job_id}", headers=auth()).status_code == 403
    assert api.post(f"{PATH}/{job_id}/cancel", headers=auth()).status_code == 403
    assert api.get(f"{PATH}/{job_id}/result", headers=auth()).status_code == 403

    api, backend, _ = client()
    job_id = submit(api).json()["job_id"]
    assert api.get(f"{PATH}/{job_id}", headers=auth(OTHER_TOKEN)).status_code == 404
    assert (
        api.get(f"/api/v1/projects/beta/scans/{job_id}", headers=auth(OTHER_TOKEN)).status_code
        == 404
    )
    assert len(backend.jobs) == 1


def test_backend_cannot_swap_another_job_from_the_same_project(monkeypatch):
    api, backend, _ = client()
    requested = submit(api, key="request-key-a").json()["job_id"]
    other = submit(api, key="request-key-b", url="https://example.test/other").json()["job_id"]
    original_get = backend.get_job
    monkeypatch.setattr(
        backend,
        "get_job",
        lambda project_id, job_id: (
            backend.jobs[other] if job_id == requested else original_get(project_id, job_id)
        ),
    )
    assert api.get(f"{PATH}/{requested}", headers=auth()).status_code == 503
    monkeypatch.setattr(backend, "cancel_job", lambda _project_id, _job_id: backend.jobs[other])
    assert api.post(f"{PATH}/{requested}/cancel", headers=auth()).status_code == 503

    monkeypatch.setattr(backend, "get_job", original_get)
    backend.jobs[requested] = backend.jobs[requested].model_copy(update={"state": "finished"})
    backend.results[requested] = JobResult(
        job=backend.jobs[other],
        coverage="partial",
        audit_available=False,
        audit_reason="synthetic mismatch",
    )
    result = api.get(f"{PATH}/{requested}/result", headers=auth())
    assert result.status_code == 503
    assert result.json()["error"]["code"] == "backend_identity_failure"


def test_list_status_cancel_and_result_contracts():
    api, backend, _ = client()
    job_id = submit(api).json()["job_id"]
    listed = api.get(PATH, headers=auth(), params={"limit": 1}).json()
    assert listed["items"][0]["job_id"] == job_id
    assert listed["has_more"] is False
    assert api.get(PATH, headers=auth(), params={"limit": 101}).status_code == 422
    assert api.get(f"{PATH}/{job_id}", headers=auth()).json()["state"] == "queued"
    pending = api.get(f"{PATH}/{job_id}/result", headers=auth())
    assert pending.status_code == 409 and pending.json()["error"]["code"] == "result_not_ready"
    cancelled = api.post(f"{PATH}/{job_id}/cancel", headers=auth())
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancel_requested"
    backend.jobs[job_id] = backend.jobs[job_id].model_copy(update={"state": "cancelled"})
    backend.results[job_id] = JobResult(
        job=backend.jobs[job_id],
        coverage="partial",
        audit_available=False,
        audit_reason="cancelled before a saved audit",
    )
    result = api.get(f"{PATH}/{job_id}/result", headers=auth())
    assert result.status_code == 200 and result.json()["coverage"] == "partial"
    assert result.json()["audit_available"] is False


def test_evidence_projection_matches_local_scan_status(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([("https://example.test/", 0)])
        scan.commit_page(scan.claim(1)[0], _record(), runtime=_runtime())
        assert scan.finish_capture()
    local = scan_status(str(path))
    remote = evidence_from_scan(str(path)).model_dump(mode="json", by_alias=True)
    for key in ("source", "frontier", "committed_page_outcomes"):
        assert remote[key] == local[key]

    api, backend, _ = client()
    job_id = submit(api).json()["job_id"]
    backend.jobs[job_id] = backend.jobs[job_id].model_copy(update={"state": "finished"})
    backend.results[job_id] = JobResult(
        job=backend.jobs[job_id],
        coverage="partial",
        evidence=evidence_from_scan(str(path)),
        audit_available=False,
        audit_reason="synthetic scan has no saved audit",
    )
    result = api.get(f"{PATH}/{job_id}/result", headers=auth()).json()
    assert result["evidence"] == remote


def test_submission_fingerprint_is_canonical_and_excludes_bearer():
    first = ScanSubmission.model_validate({"target_url": "https://example.test/"})
    second = ScanSubmission.model_validate({"options": {}, "target_url": "https://example.test/"})
    assert first.fingerprint() == second.fingerprint()
    assert TOKEN not in first.fingerprint()


def test_complete_result_requires_retained_evidence_and_audit():
    api, backend, _ = client()
    job_id = submit(api).json()["job_id"]
    with pytest.raises(ValidationError, match="complete result requires"):
        JobResult(
            job=backend.jobs[job_id],
            coverage="complete",
            audit_available=False,
            audit_reason="missing",
        )
