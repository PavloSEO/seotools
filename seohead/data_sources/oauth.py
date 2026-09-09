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

TOKEN_HOST = "https://oauth2.googleapis.com/token"
RefreshTransport = Callable[[dict[str, str]], dict[str, Any]]


def _path(provider: str) -> Path:
    if provider != "gsc":
        raise ValueError("unsupported OAuth provider")
    return CONFIG_ROOT / provider / "oauth.json"


def save_grant(provider: str, grant: dict[str, Any]) -> None:
    """Explicitly persist a read-only grant locally with restrictive permissions."""
    required = {"refresh_token", "client_id", "client_secret", "scopes"}
    if set(grant) != required or not all(isinstance(grant[key], str) and grant[key] for key in required - {"scopes"}):
        raise ValueError("OAuth grant has an unsupported shape")
    if not isinstance(grant["scopes"], list) or grant["scopes"] != ["https://www.googleapis.com/auth/webmasters.readonly"]:
        raise ValueError("GSC grants must have only the webmasters.readonly scope")
    path = _path(provider)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
    if not path.is_file() or path.is_symlink():
        raise MissingCredential("durable OAuth grant is not configured")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise MissingCredential("durable OAuth grant is unreadable") from exc
    if not isinstance(value, dict):
        raise MissingCredential("durable OAuth grant is invalid")
    return value


def _default_refresh(payload: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        TOKEN_HOST,
        data=urllib.parse.urlencode(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
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
