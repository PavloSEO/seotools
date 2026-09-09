"""Small stderr-only terminal status helpers for indeterminate local operations."""

from __future__ import annotations

import itertools
import sys
import threading
import time
from contextlib import AbstractContextManager
from typing import TextIO


def show_banner(command: str, quiet: bool = False) -> None:
    """Print one startup line to stderr unless the caller explicitly requested quiet output."""
    if quiet:
        return
    try:
        print(f"seohead: {command}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


class _ElapsedProgress(AbstractContextManager["_ElapsedProgress"]):
    def __init__(self, label: str, enabled: bool) -> None:
        self.label = label
        self.enabled = enabled
        self.stream: TextIO | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = 0.0
        self._painted = False

    def __enter__(self) -> "_ElapsedProgress":
        stream = sys.stderr
        if not self.enabled or not bool(getattr(stream, "isatty", lambda: False)()):
            return self
        self.stream = stream
        self._started = time.monotonic()
        self._thread = threading.Thread(target=self._render, name="seohead-elapsed", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._painted and self.stream is not None:
            try:
                self.stream.write("\n")
                self.stream.flush()
            except (OSError, ValueError):
                pass

    def _render(self) -> None:
        for mark in itertools.cycle("|/-\\"):
            if self._stop.is_set():
                return
            elapsed = int(time.monotonic() - self._started)
            try:
                assert self.stream is not None
                self.stream.write(f"\r{self.label}: {mark} {elapsed}s elapsed")
                self.stream.flush()
                self._painted = True
            except (AssertionError, OSError, ValueError):
                return
            self._stop.wait(0.2)


def elapsed_progress(label: str, enabled: bool = True) -> _ElapsedProgress:
    """Return an elapsed-time spinner context for a TTY; non-TTY streams stay silent."""
    return _ElapsedProgress(label, enabled)
