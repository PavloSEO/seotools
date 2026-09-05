"""Arsenkin API client for search volume, SERP clustering, and Yandex top results.

The client follows this provider task lifecycle::

    /set   -> {"code":"SET_TASK_OK","task_id":int,"cost":int}   # cost = charged credits
    /check -> {"code":"TASK_STATUS","status":"process"|"finish","progress":0..100}
    /get   -> {"code":"TASK_RESULT","task_id":int,"result":{...}}
    /info  -> {"query":"limits"} => {"code":"TOTAL_LIMITS","limits_total":int}
    429    -> {"status":"Error","code":"429","error":"Too Many Requests"}

Hard API limits are **30 requests per minute across all endpoints** and **5 concurrent tasks**.
The rate limiter keeps a safety margin below the published ceiling; otherwise several requests
can receive HTTP 429 at once.

Two safeguards keep paid results recoverable:

* ``waiting`` means that a task is queued. Every non-terminal status means "continue waiting";
  a queued task must not be abandoned after credits have been charged.
* ``task_id`` is written to the spend log **when the task is created**, before result parsing.
  The paid result can then be fetched again without another charge; see :meth:`refetch`.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from seohead.data_sources import spend
from seohead.data_sources.credentials import arsenkin_token

BASE = "https://arsenkin.ru/api/tools"
MAX_RPM = 30  # API ceiling across all endpoints, in requests per minute.
MAX_CONCURRENT = 5  # API ceiling for concurrently running tasks.
SOURCE = "arsenkin"


class RateLimiter:
    """Thread-safe sliding window capped at ``max_calls`` per ``period`` seconds."""

    def __init__(self, max_calls: int = MAX_RPM, period: float = 60.0, safety: int = 3):
        self.max_calls = max(1, max_calls - safety)  # Leave headroom below 30 requests/minute.
        self.period = period
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._calls = [t for t in self._calls if now - t < self.period]
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                sleep_for = self.period - (now - self._calls[0]) + 0.05
            time.sleep(max(sleep_for, 0.1))


class ArsenkinError(Exception):
    def __init__(self, code: str, message: str, payload: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.payload = payload


class ArsenkinClient:
    def __init__(self, token: str | None = None, limiter: RateLimiter | None = None):
        self.token = token or arsenkin_token()
        self.limiter = limiter or RateLimiter()

    def _post(self, endpoint: str, body: dict, retries: int = 5, *, billed: bool = False) -> Any:
        """POST to an endpoint; ``billed`` marks one that creates and charges a task.

        HTTP 429 means the provider replied without creating a task, so retrying it is safe and
        unchanged. A network-level exception is different: the response was lost, not refused,
        and Arsenkin offers no idempotency key to deduplicate a resent ``/set``. For a billed
        endpoint the attempt is logged and the call fails outright instead of resending a payload
        that may already have created and charged a task.
        """
        url = f"{BASE}/{endpoint}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last: ArsenkinError | None = None
        for attempt in range(retries):
            self.limiter.acquire()
            request = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
            )
            try:
                # The request URL is built from the fixed HTTPS provider base.
                with urllib.request.urlopen(request, timeout=90) as response:  # nosec B310
                    raw = response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                if exc.code == 429:
                    time.sleep(2**attempt + 1)
                    last = ArsenkinError("429", raw)
                    continue
                raise ArsenkinError(str(exc.code), raw)  # noqa: B904 - Preserve the provider body.
            except urllib.error.URLError as exc:
                if billed:
                    spend.record(
                        SOURCE,
                        endpoint,
                        cost=0.0,
                        unit="limits",
                        extra={"attempt_failed": "network_error", "detail": str(exc)},
                    )
                    raise ArsenkinError(
                        "NETWORK",
                        f"{endpoint}: response lost; task may already be billed: {exc}",
                    ) from None
                last = ArsenkinError("NETWORK", str(exc))
                time.sleep(2**attempt + 1)
                continue

            payload = json.loads(raw)
            if isinstance(payload, dict):
                if str(payload.get("code")) == "429":
                    time.sleep(2**attempt + 1)
                    last = ArsenkinError("429", raw)
                    continue
                if payload.get("status") == "Error":
                    raise ArsenkinError(
                        str(payload.get("code")), payload.get("error", raw), payload
                    )
            return payload
        raise last or ArsenkinError("RETRY", f"{endpoint}: retries exhausted")

    # --- account state ---

    def limits(self) -> int | None:
        """Return the remaining account credits."""
        return self._post("info", {"query": "limits"}).get("limits_total")

    def status(self) -> dict:
        return self._post("info", {"query": "status"})

    # --- task lifecycle ---

    def set_task(self, tools_name: str, data: dict) -> dict:
        """Create a task and immediately log its charge together with ``task_id``.

        A ``SET_TASK_OK`` response is only useful for recovery if its ``task_id`` is a positive
        integer: that identifier is the sole mechanism ``get``/``refetch`` have for retrieving an
        already-paid result. A missing, null, non-numeric, boolean, or non-positive ID cannot be
        used that way, so it must never be advertised as a recoverable task. The reported charge
        is still real and is journaled as a temporary entry with no invented ID, and the caller
        gets a structured failure instead of a task it cannot recover.
        """
        result = self._post("set", {"tools_name": tools_name, "data": data}, billed=True)
        raw_task_id, cost = result.get("task_id"), result.get("cost")
        task_id = _valid_task_id(raw_task_id)
        if task_id is None:
            spend.record(
                SOURCE,
                tools_name,
                cost=float(cost or 0),
                unit="limits",
                items=_count_items(data),
                extra={
                    "temporary": True,
                    "reason": "set_task_ok_missing_task_id",
                    "received_task_id": _sanitize_for_journal(raw_task_id),
                },
            )
            raise ArsenkinError(
                "INVALID_TASK_ID",
                f"{tools_name}: SET_TASK_OK response had no usable task_id "
                f"(received {raw_task_id!r}); cost {cost!r} was billed and journaled",
                result,
            )
        spend.record(
            SOURCE,
            tools_name,
            cost=float(cost or 0),
            unit="limits",
            task_id=task_id,
            items=_count_items(data),
        )
        return {"task_id": task_id, "cost": cost, "raw": result}

    def check(self, task_id: int) -> tuple[str | None, dict]:
        result = self._post("check", {"task_id": int(task_id)})
        return result.get("status"), result

    def get(self, task_id: int) -> dict:
        """Fetch a result without another charge; the task was paid for at creation time."""
        return self._post("get", {"task_id": int(task_id)})

    refetch = get  # Explicit alias for retrieving an already-paid result again.

    def delete(self, task_id: int) -> dict:
        return self._post("tasks", {"action": "delete", "task_id": int(task_id)})

    def restart(self, task_id: int) -> dict:
        return self._post("tasks", {"action": "restart", "task_id": int(task_id)})

    def wait(self, task_id: int, timeout: int = 900, interval: int = 8) -> dict:
        """Wait for task completion and return its result.

        Only terminal failures are enumerated explicitly. Every other state, including
        ``process``, queued states such as ``waiting`` and ``queue``, and an empty status means
        "keep waiting" so an already-paid result remains recoverable.
        """
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            status, _ = self.check(task_id)
            if status == "finish":
                return self.get(task_id)
            if status in ("error", "failed", "cancelled", "canceled"):
                raise ArsenkinError("STATUS", f"task {task_id}: status {status}")
            time.sleep(interval)
        raise ArsenkinError(
            "TIMEOUT",
            f"task {task_id} did not finish within {timeout}s; the result is already paid for, "
            f"so retrieve it later with get({task_id})",
        )

    def run(self, tools_name: str, data: dict, **kwargs) -> tuple[dict, Any]:
        """Run set → wait → get and return ``(result, charged_credits)``."""
        task = self.set_task(tools_name, data)
        return self.wait(task["task_id"], **kwargs), task["cost"]


def _valid_task_id(value: Any) -> int | None:
    """Return ``value`` as a positive ``int`` task ID, or ``None`` if it cannot recover a task.

    Booleans are excluded even though ``bool`` is a subclass of ``int`` in Python: ``True``/
    ``False`` are never task identifiers. A numeric string such as ``"123"`` is accepted because
    ``check()``/``get()`` coerce it with ``int()`` regardless of the received JSON type.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.isascii() or not text.isdigit():
            return None
        try:
            parsed = int(text.lstrip("0") or "0")
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _sanitize_for_journal(value: Any) -> Any:
    """Keep a received ``task_id`` shape journal-safe: primitives only, no arbitrary objects."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)[:200]


def _count_items(data: dict) -> int:
    """Estimate request item count for the ledger; an empty estimate is safer than guessing."""
    for key in ("keywords", "words", "queries", "phrases", "urls"):
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            return len(value)
        if isinstance(value, str):
            return len([line for line in value.splitlines() if line.strip()])
    return 0


class BatchRunner:
    """Run an Arsenkin batch with at most five active tasks and one shared rate limiter.

    A separate runner is necessary because the API enforces two independent ceilings:
    30 requests per minute and 5 concurrently running tasks. A naive ten-worker
    ``ThreadPoolExecutor`` violates the second limit on its sixth task. The semaphore enforces
    concurrency while the client rate limiter remains shared across all worker threads.

    Most importantly, **one failed task does not abort the batch**. A task-specific exception is
    stored in that task's result while the remaining tasks continue, and every paid task keeps
    its identifier for later retrieval.
    """

    def __init__(self, client: ArsenkinClient | None = None, max_concurrent: int = MAX_CONCURRENT):
        self.client = client or ArsenkinClient()
        self._slots = threading.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent

    def _run_one(self, tools_name: str, data: dict, label: str) -> dict:
        with self._slots:
            try:
                task = self.client.set_task(tools_name, data)
            except ArsenkinError as exc:
                return {"label": label, "error": str(exc), "code": exc.code}
            try:
                result = self.client.wait(task["task_id"])
            except ArsenkinError as exc:
                # Preserve the paid task ID so its result can be retrieved later.
                return {
                    "label": label,
                    "task_id": task["task_id"],
                    "cost": task["cost"],
                    "error": str(exc),
                    "code": exc.code,
                }
            return {
                "label": label,
                "task_id": task["task_id"],
                "cost": task["cost"],
                "result": result,
            }

    def run(self, jobs: list[dict]) -> list[dict]:
        """Run ``{"tools_name", "data", "label"}`` jobs and preserve input order."""
        import concurrent.futures

        results: list[Any] = [None] * len(jobs)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_concurrent) as pool:
            futures = {
                pool.submit(
                    self._run_one, job["tools_name"], job["data"], job.get("label", str(index))
                ): index
                for index, job in enumerate(jobs)
            }
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def refetch(self, task_id: int) -> dict:
        """Fetch an already-paid result by task ID without another charge."""
        return self.client.get(task_id)
