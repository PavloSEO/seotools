"""Restricted local storage and explicit refresh for read-only OAuth grants."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from seohead.data_sources.credentials import CONFIG_ROOT, MissingCredential
from seohead.data_sources.http import open_no_redirect

TOKEN_HOST = "https://oauth2.googleapis.com/token"
RefreshTransport = Callable[[dict[str, str]], dict[str, Any]]


def _path(provider: str) -> Path:
    if provider != "gsc":
        raise ValueError("unsupported OAuth provider")
    return CONFIG_ROOT / provider / "oauth.json"


def save_grant(provider: str, grant: dict[str, Any]) -> None:
    """Explicitly persist a read-only grant locally with restrictive permissions."""
    required = {"refresh_token", "client_id", "client_secret", "scopes"}
    if not isinstance(grant, dict) or set(grant) != required or not all(isinstance(grant[key], str) and grant[key] for key in required - {"scopes"}):
        raise ValueError("OAuth grant has an unsupported shape")
    if not isinstance(grant["scopes"], list) or grant["scopes"] != ["https://www.googleapis.com/auth/webmasters.readonly"]:
        raise ValueError("GSC grants must have only the webmasters.readonly scope")
    path = _path(provider)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("OAuth storage must not use symlinks")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        raise ValueError("OAuth grant already exists; revoke or disconnect it before reconnecting")
    descriptor, staged = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(grant, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    finally:
        Path(staged).unlink(missing_ok=True)


def _grant(provider: str) -> dict[str, Any]:
    path = _path(provider)
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_size > 65536:
        raise MissingCredential("durable OAuth grant is not configured")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MissingCredential("durable OAuth grant is unreadable") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"refresh_token", "client_id", "client_secret", "scopes"}
        or any(not isinstance(value.get(key), str) or not value[key] for key in ("refresh_token", "client_id", "client_secret"))
        or value.get("scopes") != ["https://www.googleapis.com/auth/webmasters.readonly"]
    ):
        raise MissingCredential("durable OAuth grant is invalid")
    return value


def _default_refresh(payload: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        TOKEN_HOST,
        data=urllib.parse.urlencode(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with open_no_redirect(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def refresh_access_token(provider: str, *, transport: RefreshTransport | None = None) -> dict[str, Any]:
    """Exchange a stored refresh token only when an explicit live operation needs it."""
    grant = _grant(provider)
    payload = {
        "grant_type": "refresh_token", "refresh_token": grant["refresh_token"],
        "client_id": grant["client_id"], "client_secret": grant["client_secret"],
    }
    body = (transport or _default_refresh)(payload)
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise MissingCredential("OAuth refresh did not return an access token")
    return {"access_token": token, "scopes": grant["scopes"], "expires_in": body.get("expires_in")}


def grant_available(provider: str = "gsc") -> bool:
    try:
        _grant(provider)
    except (MissingCredential, OSError, ValueError):
        return False
    return True


def manage_grant(provider: str, action: str = "status", grant_file: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """Manage a local read-only grant without returning OAuth material.

    Connect imports a grant obtained through the provider's consent flow; it
    does not bypass consent or claim property access. Revoke is a separate,
    confirmed Google write; disconnect only removes the local stored grant.
    """
    path = _path(provider)
    if action == "status":
        return {"ok": True, "configured": grant_available(provider), "access_verified": False}
    if action == "connect":
        if not isinstance(grant_file, str):
            raise ValueError("connect requires a private grant_file")
        source = Path(grant_file).expanduser()
        if source.is_symlink() or not source.is_file() or source.stat().st_mode & 0o077 or source.stat().st_size > 65536:
            raise ValueError("grant_file must be a private bounded regular JSON file")
        try:
            grant = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("grant_file is unreadable or invalid JSON") from exc
        save_grant(provider, grant)
        return {"ok": True, "configured": True, "access_verified": False}
    if action == "refresh":
        refreshed = refresh_access_token(provider)
        return {"ok": True, "refreshed": True, "scopes": refreshed["scopes"], "expires_in": refreshed["expires_in"], "property_access_verified": False}
    if action in {"disconnect", "revoke"}:
        if confirm is not True:
            raise ValueError("disconnect/revoke requires confirm=true")
        grant = _grant(provider)
        if action == "revoke":
            request = urllib.request.Request(
                "https://oauth2.googleapis.com/revoke",
                data=urllib.parse.urlencode({"token": grant["refresh_token"]}).encode(),
                method="POST", headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with open_no_redirect(request, timeout=30) as response:
                    if response.status != 200:
                        raise ValueError("Google did not confirm revocation")
            except OSError as exc:
                raise ValueError("Google revocation failed; local grant preserved") from exc
        path.unlink()
        return {"ok": True, "configured": False, "remote_revoked": action == "revoke", "scope": "stored OAuth grant only; environment bearers and service accounts are unchanged"}
    raise ValueError("action must be status, connect, refresh, disconnect, or revoke")
