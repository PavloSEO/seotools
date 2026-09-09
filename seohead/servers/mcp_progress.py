"""SDK-backed, request-token-only MCP progress reporters for async tool bindings."""

from __future__ import annotations

import math
import time
from contextlib import AbstractAsyncContextManager
from typing import Any, Callable

MIN_INTERVAL_SECONDS = 0.5


def _has_progress_token(context: Any) -> bool:
    try:
        metadata = context.request_context.meta
        return metadata is not None and metadata.progressToken is not None
    except (AttributeError, LookupError, ValueError):
        return False


class ProgressReporter(AbstractAsyncContextManager["ProgressReporter"]):
    """Emit throttled standard notifications only while an active request supplied a token."""

    def __init__(self, context: Any, label: str, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.context = context
        self.label = label
        self.clock = clock
        self.enabled = _has_progress_token(context)
        self._started = clock()
        self._last_emit: float | None = None
        self._last_progress = 0.0
        self._closed = False

    async def __aenter__(self) -> "ProgressReporter":
        return self

    async def __aexit__(self, *_: object) -> None:
        self._closed = True

    async def known(self, progress: float, total: float, message: str | None = None) -> None:
        """Report a measured monotonic unit count; callers may not invent a percentage."""
        if (
            not math.isfinite(progress)
            or not math.isfinite(total)
            or total <= 0
            or progress < self._last_progress
            or progress > total
        ):
            raise ValueError("progress must be monotonic and within a positive measured total")
        self._last_progress = progress
        await self._send(progress, total, message or f"{self.label}: {progress:g}/{total:g}")

    async def elapsed(self, message: str | None = None) -> None:
        """Report actual elapsed time when no completion total is available."""
        seconds = max(self._last_progress, self.clock() - self._started)
        self._last_progress = seconds
        await self._send(seconds, None, message or f"{self.label}: {seconds:.1f}s elapsed; total unknown")

    async def _send(self, progress: float, total: float | None, message: str) -> None:
        if not self.enabled or self._closed:
            return
        now = self.clock()
        if self._last_emit is not None and now - self._last_emit < MIN_INTERVAL_SECONDS:
            return
        self._last_emit = now
        await self.context.report_progress(progress=progress, total=total, message=message)


class ProgressInstallation:
    """Factory installed on a FastMCP instance for production bindings after registration."""

    def scope(self, context: Any, label: str) -> ProgressReporter:
        return ProgressReporter(context, label)


def install_progress(server: Any, *, enabled: bool = True) -> ProgressInstallation | None:
    """Attach a binding factory without changing current synchronous tool functions.

    Production bindings use the returned factory while registering async tools. Direct build_server
    may omit this and retain synchronous function objects for unit-level compatibility.
    """
    installation = ProgressInstallation() if enabled else None
    setattr(server, "_seohead_progress", installation)
    return installation
