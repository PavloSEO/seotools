"""Thin shared-handler boundary for explicit provider evidence operations."""

from __future__ import annotations

from typing import Any

from seohead.data_sources.providers import (
    provider_collect as _collect,
)
from seohead.data_sources.providers import (
    provider_join as _join,
)
from seohead.data_sources.providers import (
    provider_registry as _registry,
)
from seohead.data_sources.providers import (
    provider_verify as _verify,
)
from seohead.data_sources.providers import (
    sources_doctor as _doctor,
)


def provider_registry() -> dict[str, Any]:
    return _registry()


def provider_doctor() -> dict[str, Any]:
    return _doctor()


def provider_verify(provider: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
    """Explicit, bounded, read-only credential verification; it never mutates provider state."""
    return _verify(provider, request)


def provider_collect(
    provider: str,
    operation: str,
    request: dict[str, Any],
    artifact_dir: str | None = None,
) -> dict[str, Any]:
    """Collect one declared read-only operation, with raw data retained only when requested locally."""
    return _collect(provider, operation, request, artifact_dir=artifact_dir)


def provider_join(
    crawl_pages: list[dict[str, Any]],
    evidence_rows: list[dict[str, Any]],
    review_external_only: bool = False,
    adjustments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _join(
        crawl_pages,
        evidence_rows,
        review_external_only=review_external_only,
        adjustments=adjustments,
    )
