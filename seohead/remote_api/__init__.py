"""Optional authenticated HTTP contracts for self-hosted scan jobs."""

from __future__ import annotations

from typing import Any


def create_app(*args: Any, **kwargs: Any) -> Any:
    """Load the HTTP adapter only when the optional remote extra is requested."""
    try:
        from seohead.remote_api.app import create_app as build
    except ModuleNotFoundError as exc:
        if exc.name == "fastapi":
            raise RuntimeError("install seohead-seotools[remote] to use the HTTP API") from exc
        raise
    return build(*args, **kwargs)


__all__ = ["create_app"]
