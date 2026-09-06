"""Politeness that reacts to the origin, not just to a configured ceiling.

Measured on a real shared-hosting catalogue: under a polite 1.5 URL/s the origin
degraded from 1 196 ms to 16 455 ms TTFB and then began refusing TLS handshakes,
without ever returning an error status. A throttle that only widens on non-200
would have kept pushing, so latency widens the delay and a timeout widens it
hard — a timeout is the strongest signal available, never a reason to retry
immediately.

The same object also tracks how many requests may be in flight to this origin
at once. That number starts conservative and only widens on sustained success;
a timeout or a server refusal collapses it back to one immediately — the same
asymmetry the delay itself uses, and for the same reason: fetching more from an
origin that just showed it is struggling turns an audit into a load test.

Both the delay and the concurrency level are read and mutated from whichever
thread is fetching the corresponding URL when the crawler runs several requests
at once, so every mutating method is serialized behind one lock.

The two adapt together, not independently: the delay is nudged toward
``latency / concurrency``, not toward raw latency. At concurrency 1 that is
just latency, unchanged from a strictly sequential crawler. At a higher earned
concurrency it is the spacing that keeps that many requests landing back to
back — the pacing a level-batched crawler needs to turn overlapped wait time
into real throughput instead of quietly re-serializing dispatch to the same
one-at-a-time rate.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from typing import Any

TIMEOUT_PENALTY = 4.0
MAX_DELAY_S = 60.0

# How many consecutive good responses it takes to trust the origin with one
# more concurrent request. Slow to grow, fast to collapse.
WIDEN_AFTER_CONSECUTIVE_OK = 3

# A ceiling on the *configured* value, not on what the adaptive throttle will
# actually use — ``concurrency`` starts low and earns its way up to whichever
# of this or the caller's request is smaller (#14: "a hard ceiling on
# concurrency that a config file alone cannot raise"). Enforced here, inside
# the constructor, rather than only at the one call site that currently reads
# a config value — so any caller building a ``Throttle`` directly, not only
# ``crawl_site()``, is bound by it too.
MAX_CONCURRENCY_CEILING = 16


class DispatchGate:
    """Thread-safe shared dispatch clock for every request to one origin."""

    def __init__(
        self,
        throttle: Throttle,
        sleeper: Callable[[float], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._throttle = throttle
        self._sleeper = sleeper
        self._clock = clock
        self._lock = threading.Lock()
        self._last_at: float | None = None

    def wait_turn(self) -> None:
        with self._lock:
            now = self._clock()
            # Read the current delay when reserving a turn: a timeout or a newly
            # learned robots delay must apply to the next request, not one later.
            start_at = (
                now if self._last_at is None else max(now, self._last_at + self._throttle.delay)
            )
            self._last_at = start_at
            wait = start_at - now
        if wait > 0:
            self._sleeper(wait)


class Throttle:
    """Adaptive delay and concurrency for one origin."""

    def __init__(
        self,
        start_delay: float = 0.0,
        min_delay: float = 0.0,
        max_delay: float = MAX_DELAY_S,
        max_concurrency: int = 1,
        adaptive: bool = True,
    ) -> None:
        self._min_delay = max(0.0, float(min_delay))
        self._max_delay = max(self._min_delay, float(max_delay))
        self.delay = max(self._min_delay, float(start_delay))
        self.timeouts = 0
        self.server_errors = 0
        # The ceiling is a configured, bounded fact; ``concurrency`` is what the
        # origin has earned so far, never more than the ceiling allows.
        self.max_concurrency = max(1, min(int(max_concurrency), MAX_CONCURRENCY_CEILING))
        self.concurrency = min(2, self.max_concurrency)
        self._consecutive_ok = 0
        # speed.adaptive. When False the delay stays exactly where it was configured and the
        # concurrency stays at the earned starting level: the crawl is paced by the operator,
        # not by the origin. The timeout and server-error counters keep running either way —
        # they are a separate "give up" mechanism, and a non-adaptive crawl still has to stop
        # when the origin stops answering rather than keep hammering it.
        self.adaptive = bool(adaptive)
        if not self.adaptive:
            self.concurrency = self.max_concurrency
        self._lock = threading.Lock()

    @property
    def min_delay(self) -> float:
        return self._min_delay

    @min_delay.setter
    def min_delay(self, value: float) -> None:
        """Raising the floor above the current ceiling raises the ceiling too.

        A robots.txt ``Crawl-delay`` above ``max_delay_seconds`` (issue #150) is the
        motivating case: every adaptive clamp below is ``min(max_delay, max(min_delay,
        x))``, which silently collapses to ``max_delay`` forever once the floor
        exceeds it. Keeping the invariant here, on assignment, means no call site —
        today's or a future one — has to remember to touch both fields by hand.
        """
        self._min_delay = max(0.0, float(value))
        if self._max_delay < self._min_delay:
            self._max_delay = self._min_delay

    @property
    def max_delay(self) -> float:
        return self._max_delay

    @max_delay.setter
    def max_delay(self, value: float) -> None:
        self._max_delay = max(0.0, float(value))
        if self._min_delay > self._max_delay:
            self._min_delay = self._max_delay

    def record_response(self, latency_s: float, ok: bool) -> None:
        """Fold one completed response into the delay and the concurrency level.

        A non-2xx response may raise the delay but never lower it: a fast 500 is
        not evidence that the origin is healthy.
        """
        with self._lock:
            if not self.adaptive:
                return
            target = max(latency_s, 0.0) / self.concurrency
            new_delay = (self.delay + target) / 2
            if not ok:
                new_delay = max(new_delay, self.delay)
            self.delay = min(self.max_delay, max(self.min_delay, new_delay))
            if ok:
                self._consecutive_ok += 1
                if (
                    self._consecutive_ok >= WIDEN_AFTER_CONSECUTIVE_OK
                    and self.concurrency < self.max_concurrency
                ):
                    self.concurrency += 1
                    self._consecutive_ok = 0
            else:
                self._consecutive_ok = 0

    def record_timeout(self) -> None:
        """A connection, TLS or read timeout: the origin is failing, back off hard."""
        with self._lock:
            self.timeouts += 1
            if not self.adaptive:
                return
            base = max(self.delay, self.min_delay, 0.5)
            self.delay = min(self.max_delay, base * TIMEOUT_PENALTY)
            self._consecutive_ok = 0
            self.concurrency = 1

    def should_stop(self, limit: int = 5) -> bool:
        """Consecutive timeouts mean the origin is down; stop rather than hammer it.

        The count is shared across every concurrent worker: three of four
        workers seeing a timeout is the same "origin is failing" signal as one
        worker seeing three in a row.
        """
        with self._lock:
            return self.timeouts >= limit

    def record_success(self) -> None:
        with self._lock:
            self.timeouts = 0
            self.server_errors = 0

    def record_server_error(self, status_code: int, retry_after: float | None = None) -> None:
        """A host answering 429 or 5xx is already struggling.

        Treat a single 429 as an overload signal rather than a retryable blip:
        it is the server explicitly asking for less, and continuing at the same
        rate turns an audit into a load test.
        """
        with self._lock:
            self.server_errors += 1
            if not self.adaptive:
                return
            base = max(self.delay, self.min_delay, 0.5)
            widened = base * TIMEOUT_PENALTY if status_code == 429 else base * 2
            if retry_after is not None:
                widened = max(widened, retry_after)
            self.delay = min(self.max_delay, widened)
            self._consecutive_ok = 0
            self.concurrency = 1

    def host_is_failing(self, limit: int = 5) -> bool:
        """Consecutive server refusals mean stop, not retry harder.

        Shared across workers for the same reason as ``should_stop``: the
        signal is about the origin, not about which worker happened to see it.
        """
        with self._lock:
            return self.server_errors >= limit

    def snapshot_state(self) -> dict[str, float | int]:
        """Return the adaptive state persisted independently of circuit streaks.

        Timeout and server-refusal streaks belong to the crawler's deterministic
        queue-order fold, not this completion-order adaptive throttle state.
        Once concurrency has reached its configured ceiling, additional successes
        cannot widen it further; cap their persisted streak at the largest value
        that can still affect a future response. A timeout, refusal or failed
        response resets the live streak either way.
        """
        with self._lock:
            return {
                "delay_seconds": self.delay,
                "concurrency": self.concurrency,
                "consecutive_ok": min(self._consecutive_ok, WIDEN_AFTER_CONSECUTIVE_OK - 1),
            }

    def restore_state(self, state: dict[str, Any]) -> None:
        """Restore a validated :meth:`snapshot_state` payload.

        The payload is deliberately closed: accepting a missing/defaulted field
        would turn a resumed crawl into a different adaptive policy.
        """
        if not isinstance(state, dict) or set(state) != {
            "delay_seconds",
            "concurrency",
            "consecutive_ok",
        }:
            raise ValueError("invalid throttle state keys")
        delay = state["delay_seconds"]
        concurrency = state["concurrency"]
        consecutive_ok = state["consecutive_ok"]
        if type(delay) not in (int, float) or not math.isfinite(float(delay)):
            raise ValueError("throttle delay_seconds must be finite")
        if type(concurrency) is not int or not 1 <= concurrency <= self.max_concurrency:
            raise ValueError("throttle concurrency is outside configured bounds")
        if type(consecutive_ok) is not int or not 0 <= consecutive_ok < WIDEN_AFTER_CONSECUTIVE_OK:
            raise ValueError("throttle consecutive_ok is outside supported bounds")
        if not self.min_delay <= float(delay) <= self.max_delay:
            raise ValueError("throttle delay_seconds is outside configured bounds")
        if not self.adaptive and concurrency != self.max_concurrency:
            raise ValueError("non-adaptive throttle concurrency must equal its configured maximum")
        with self._lock:
            self.delay = float(delay)
            self.concurrency = concurrency
            self._consecutive_ok = consecutive_ok
