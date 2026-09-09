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

    def __init__(
        self, context: Any, label: str, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.context = context
        self.label = label
        self.clock = clock
        self.enabled = _has_progress_token(context)
        self._started = clock()
        self._last_emit: float | None = None
        self._last_progress = 0.0
        self._last_sent: float | None = None
        self._mode: str | None = None
        self._closed = False

    async def __aenter__(self) -> "ProgressReporter":
        return self

    async def __aexit__(self, *_: object) -> None:
        self._closed = True

    async def known(self, progress: float, total: float, message: str | None = None) -> None:
        """Report a measured monotonic unit count; callers may not invent a percentage."""
        if self._mode not in {None, "known"}:
            raise ValueError("cannot switch a progress reporter from elapsed to known units")
        if (
            not math.isfinite(progress)
            or not math.isfinite(total)
            or total <= 0
            or progress < self._last_progress
            or progress > total
        ):
            raise ValueError("progress must be monotonic and within a positive measured total")
        self._mode = "known"
        self._last_progress = progress
        await self._send(progress, total, message or f"{self.label}: {progress:g}/{total:g}")

    async def elapsed(self, message: str | None = None) -> None:
        """Report actual elapsed time when no completion total is available."""
        if self._mode not in {None, "elapsed"}:
            raise ValueError("cannot switch a progress reporter from known to elapsed units")
        seconds = self.clock() - self._started
        self._mode = "elapsed"
        self._last_progress = seconds
        await self._send(
            seconds, None, message or f"{self.label}: {seconds:.1f}s elapsed; total unknown"
        )

    async def _send(self, progress: float, total: float | None, message: str) -> None:
        if not self.enabled or self._closed:
            return
        now = self.clock()
        if self._last_emit is not None and now - self._last_emit < MIN_INTERVAL_SECONDS:
            return
        if self._last_sent is not None and progress <= self._last_sent:
            return
        self._last_sent = progress
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


def wrap_long_tools(server: Any) -> None:
    """Use SDK registration for async heartbeat wrappers without changing tool schemas."""
    import asyncio
    import functools
    import inspect
    import typing

    names = {
        "seo_crawl_site",
        "seo_parse",
        "seo_links_check",
        "seo_report_build",
        "seo_project_start",
        "seo_project_prepare",
        "seo_provider_collect",
        "seo_scan_reanalyze",
        "seo_inspect_url",
        "seo_audit_workflow",
        "sf_audit_run",
    }

    def wrapped(function, label):
        @functools.wraps(function)
        async def invoke(*args, **kwargs):
            reporter = ProgressReporter(server.get_context(), label)
            async with reporter:
                work = asyncio.create_task(
                    function(*args, **kwargs)
                    if inspect.iscoroutinefunction(function)
                    else asyncio.to_thread(function, *args, **kwargs)
                )
                try:
                    while not work.done():
                        finished, _ = await asyncio.wait({work}, timeout=1.0)
                        if not finished:
                            await reporter.elapsed()
                    return await work
                finally:
                    if not work.done():
                        work.cancel()

        hints = typing.get_type_hints(function)
        signature = inspect.signature(function)
        invoke.__signature__ = signature.replace(
            parameters=[
                parameter.replace(annotation=hints.get(name, parameter.annotation))
                for name, parameter in signature.parameters.items()
            ],
            return_annotation=hints.get("return", signature.return_annotation),
        )
        invoke.__annotations__ = hints
        return invoke

    for tool in list(server._tool_manager.list_tools()):
        if tool.name not in names:
            continue
        function = wrapped(tool.fn, tool.name)
        server.remove_tool(tool.name)
        server.add_tool(
            function,
            name=tool.name,
            title=tool.title,
            description=tool.description,
            annotations=tool.annotations,
            icons=tool.icons,
            meta=tool.meta,
            structured_output=tool.fn_metadata.output_schema is not None,
        )
