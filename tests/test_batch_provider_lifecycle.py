"""Offline boundaries for restricted OAuth grants and provider replay."""

from __future__ import annotations

import json
import socket

import pytest

from seohead.data_sources import credentials, gsc, oauth, providers
from seohead.storage.native_scan import NativeScan
from tests.test_native_capture import _claim
from tests.test_scan_native import _metadata, _record, _runtime

_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"


def _grant() -> dict[str, object]:
    return {
        "refresh_token": "synthetic-refresh-token",
        "client_id": "synthetic-client-id",
        "client_secret": "synthetic-client-secret",
        "scopes": [_SCOPE],
    }


def _scan(path):
    with NativeScan.create(path, **_metadata()) as scan:
        lease = _claim(scan)
        scan.commit_page(lease, _record(lease.url), runtime=_runtime())
        return scan.con.execute("SELECT scan_uuid FROM scan WHERE singleton=1").fetchone()[0]


def test_gsc_connect_requires_private_file_and_never_returns_secret_values(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth, "CONFIG_ROOT", tmp_path / "config")
    source = tmp_path / "grant.json"
    source.write_text(json.dumps(_grant()), encoding="utf-8")
    source.chmod(0o644)

    with pytest.raises(ValueError, match="private bounded"):
        oauth.manage_grant("gsc", "connect", str(source))

    source.chmod(0o600)
    connected = oauth.manage_grant("gsc", "connect", str(source))
    stored = tmp_path / "config" / "gsc" / "oauth.json"
    assert connected == {"ok": True, "configured": True, "access_verified": False}
    assert stored.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(
        oauth,
        "refresh_access_token",
        lambda _provider: {
            "access_token": "synthetic-access-token",
            "scopes": [_SCOPE],
            "expires_in": 3600,
        },
    )
    refreshed = oauth.manage_grant("gsc", "refresh")
    returned = json.dumps({"connected": connected, "refreshed": refreshed})
    for secret in ("synthetic-refresh-token", "synthetic-client-secret", "synthetic-access-token"):
        assert secret not in returned
    assert refreshed == {
        "ok": True,
        "refreshed": True,
        "scopes": [_SCOPE],
        "expires_in": 3600,
        "property_access_verified": False,
    }


def test_gsc_uses_durable_refresh_only_after_bearer_lookup_fails(monkeypatch):
    monkeypatch.delenv("GSC_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(
        credentials,
        "gsc_access_token",
        lambda: (_ for _ in ()).throw(credentials.MissingCredential("no bearer")),
    )
    monkeypatch.setattr(oauth, "grant_available", lambda _provider: True)
    monkeypatch.setattr(gsc, "durable_oauth_token", lambda: {"access_token": "refreshed-bearer"})
    seen = {}

    result = gsc.search_analytics(
        "sc-domain:example.test",
        token=None,
        fetcher=lambda payload, bearer: (
            seen.update(payload=payload, bearer=bearer) or '{"rows": []}'
        ),
    )

    assert result["ok"] is True
    assert seen["bearer"] == "refreshed-bearer"
    assert "refreshed-bearer" not in json.dumps(result)


def test_failed_remote_revoke_keeps_the_private_local_grant(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth, "CONFIG_ROOT", tmp_path / "config")
    oauth.save_grant("gsc", _grant())
    stored = tmp_path / "config" / "gsc" / "oauth.json"
    before = stored.read_bytes()

    def fail_revoke(*_args, **_kwargs):
        raise OSError("synthetic offline failure")

    monkeypatch.setattr(oauth, "open_no_redirect", fail_revoke)
    with pytest.raises(ValueError, match="local grant preserved"):
        oauth.manage_grant("gsc", "revoke", confirm=True)

    assert stored.exists()
    assert stored.read_bytes() == before


def test_gsc_replay_maps_page_dimension_and_keeps_raw_join_private(tmp_path, monkeypatch):
    scan_path = tmp_path / "scan.sqlite"
    scan_uuid = _scan(scan_path)
    evidence_path = tmp_path / "gsc-private.json"
    evidence_path.write_text(
        json.dumps(
            {
                "evidence": {
                    "format": providers.EVIDENCE_FORMAT,
                    "provider": "gsc",
                    "period": "2026-01-01..2026-01-28",
                    "status": "complete",
                    "sampling": "unknown",
                },
                "result": {
                    "dimensions": ["query", "page"],
                    "rows": [
                        {"keys": ["synthetic query", "https://example.test/"], "clicks": 7},
                        {"keys": ["unkeyable", "/relative"], "clicks": 1},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    evidence_path.chmod(0o600)
    out_dir = tmp_path / "private-joins"
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("provider replay must not open a network connection"),
    )

    replayed = providers.provider_replay(
        str(scan_path), str(evidence_path), str(out_dir), review_external_only=True
    )

    assert replayed["counts"] == {
        "pages": 1,
        "rows": 2,
        "joined": 1,
        "crawl_only": 0,
        "external_only": 0,
        "unkeyable_pages": 0,
        "unkeyable_rows": 1,
    }
    public = json.dumps(replayed)
    assert "example.test" not in public and "synthetic query" not in public
    artifact = json.loads(next(out_dir.glob("provider-*.json")).read_text(encoding="utf-8"))
    assert artifact["source"]["scan_uuid"] == scan_uuid
    assert artifact["join"]["joined"][0]["external"]["url"] == "https://example.test/"
    assert artifact["join"]["joined"][0]["external"]["keys"][0] == "synthetic query"
