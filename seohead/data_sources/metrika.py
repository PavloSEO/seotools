"""Yandex Metrica API client for traffic, goals, counter settings, and raw logs.

A crawl shows what a website **contains**, Wordstat shows the **demand** for it, and Metrica
shows what visitors **actually did**. Without analytics, a client report relies on assumptions:
a technically excellent page may receive no visits, while a weaker page may attract substantial
traffic. Metrica therefore completes the client-onboarding data model by joining analytics with
crawl evidence in the knowledge system.

The client includes three operational safeguards:

* retries inspect the real HTTP status and **honor the ``Retry-After`` header** on HTTP 429;
* ``offset``/``limit`` pagination has a row ceiling so an accidental query cannot download a
  million rows, plus an inter-page delay so thousands of sequential pages do not exhaust quota;
* exceptions carry the status and message returned by the API instead of a generic
  ``request failed`` string.

⚠️ **Privacy.** Logs API exports can contain raw ``ClientID`` values, which are visitor personal
data. Never commit these exports or include them in client reports. This downloader returns text
only; callers choose where to persist it, and that path must be covered by ``.gitignore``.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from seohead.data_sources import spend
from seohead.data_sources.credentials import metrika_token

API_BASE = "https://api-metrika.yandex.net"
API_MANAGEMENT = "management/v1"
API_REPORTS = "stat/v1/data"
SOURCE = "metrika"

TIMEOUT = 30
RETRIES = 3
PAGE_PAUSE = 0.15  # Delay between automatically paginated requests.
ROW_CAP = 100_000  # Row ceiling that prevents unbounded downloads.
MAX_BACKOFF = 30.0


class MetrikaError(RuntimeError):
    """API error carrying the HTTP status and the service's own message."""

    def __init__(self, status: int, message: str):
        super().__init__(f"Metrica {status}: {message}")
        self.status = status
        self.message = message


class MetrikaClient:
    def __init__(self, token: str | None = None):
        self.token = token or metrika_token()

    # --- transport ---------------------------------------------------------

    def _request(self, url: str, method: str = "GET", raw: bool = False) -> Any:
        """Request with retries for HTTP 429, 5xx, and network failures.

        Other failures raise immediately. The open-ended loop is intentional: every branch
        either retries, returns, or raises, so control cannot fall through the bottom.
        """
        attempt = 0
        while True:
            attempt += 1
            request = urllib.request.Request(
                url,
                method=method,
                headers={"Authorization": f"OAuth {self.token}", "Accept": "application/json"},
            )
            try:
                # The request URL is built from the fixed HTTPS provider base.
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
                    body = response.read().decode("utf-8", "replace")
                    return body if raw else json.loads(body)
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                if exc.code in (429, 500, 502, 503, 504) and attempt <= RETRIES:
                    time.sleep(self._backoff(attempt, exc.headers.get("Retry-After")))
                    continue
                raise MetrikaError(exc.code, _api_message(text)) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt <= RETRIES:
                    time.sleep(self._backoff(attempt, None))
                    continue
                raise MetrikaError(0, f"network: {exc}") from None

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        """Calculate retry delay, preferring the service's ``Retry-After`` header."""
        if retry_after:
            try:
                seconds = float(retry_after)
                if seconds > 0:
                    return min(seconds, 60.0)
            except ValueError:
                pass
        return min(1.0 * 2 ** (attempt - 1), MAX_BACKOFF)

    @staticmethod
    def _url(path: str, params: dict[str, Any] | None = None) -> str:
        query = {k: str(v) for k, v in (params or {}).items() if v is not None and v != ""}
        return f"{API_BASE}/{path}" + (f"?{urllib.parse.urlencode(query)}" if query else "")

    # --- counter configuration (Management API) ----------------------------

    def counters(self) -> list[dict]:
        """Return all counters visible to the token."""
        counters = (self._request(self._url(f"{API_MANAGEMENT}/counters")) or {}).get(
            "counters", []
        )
        spend.record(SOURCE, "management.counters", cost=1, unit="requests", items=len(counters))
        return counters

    def counter(self, counter_id: str | int) -> dict:
        body = self._request(self._url(f"{API_MANAGEMENT}/counter/{counter_id}"))
        spend.record(SOURCE, "management.counter", cost=1, unit="requests", items=1)
        return body

    def goals(self, counter_id: str | int) -> list[dict]:
        """Return configured goals; an empty list means "no goals", not a failed request."""
        body = self._request(self._url(f"{API_MANAGEMENT}/counter/{counter_id}/goals"))
        goals = (body or {}).get("goals", [])
        spend.record(SOURCE, "management.goals", cost=1, unit="requests", items=len(goals))
        return goals

    def filters(self, counter_id: str | int) -> dict:
        body = self._request(self._url(f"{API_MANAGEMENT}/counter/{counter_id}/filters"))
        filters = (body or {}).get("filters", [])
        spend.record(SOURCE, "management.filters", cost=1, unit="requests", items=len(filters))
        return body

    def operations(self, counter_id: str | int) -> dict:
        """Return data operations, such as URL-parameter removal, which can alter reports silently."""
        body = self._request(self._url(f"{API_MANAGEMENT}/counter/{counter_id}/operations"))
        operations = (body or {}).get("operations", [])
        spend.record(
            SOURCE, "management.operations", cost=1, unit="requests", items=len(operations)
        )
        return body

    # --- reports (Reporting API) -------------------------------------------

    def report(
        self, params: dict[str, Any], *, paginate: bool = False, limit: int = 100, offset: int = 0
    ) -> dict:
        """Request ``stat/v1/data``.

        With ``paginate=True``, fetch all pages and combine rows up to :data:`ROW_CAP`. Without
        this ceiling, one grouping typo can accidentally request a million rows.
        """
        base = dict(params, accuracy="full")
        if not paginate:
            body = self._request(self._url(API_REPORTS, dict(base, limit=limit, offset=offset)))
            spend.record(
                SOURCE,
                "report",
                cost=1,
                unit="requests",
                items=len((body or {}).get("data") or []),
                extra={"metrics": params.get("metrics")},
            )
            return body

        page_size = min(max(limit, 100), 1000)
        first: dict | None = None
        rows: list = []
        cursor = offset
        pages = 0
        try:
            while True:
                page = self._request(
                    self._url(API_REPORTS, dict(base, limit=page_size, offset=cursor))
                )
                pages += 1
                if first is None:
                    first = page
                chunk = page.get("data") or []
                rows.extend(chunk)
                collected = offset + len(rows)
                # A response without ``data`` has nothing else to aggregate; stop cleanly.
                if len(chunk) < page_size:
                    break
                if collected >= ROW_CAP:
                    break
                total = (first or {}).get("total_rows")
                if total and collected >= total:
                    break
                cursor += page_size
                time.sleep(PAGE_PAUSE)
        except MetrikaError:
            # The page that raised still consumed a request against quota, even though it
            # never returned rows, so it counts alongside the pages that already succeeded.
            # Losing this entry would make an interrupted collection look like it never
            # touched the API at all, hiding exactly the usage a diagnosis needs.
            spend.record(
                SOURCE,
                "report.paginated",
                cost=pages + 1,
                unit="requests",
                items=len(rows),
                extra={"metrics": params.get("metrics"), "pages": pages, "outcome": "failed"},
            )
            raise

        spend.record(
            SOURCE,
            "report.paginated",
            cost=pages,
            unit="requests",
            items=len(rows),
            extra={"metrics": params.get("metrics"), "pages": pages},
        )
        result = dict(first or {})
        result["data"] = rows
        result["query"] = dict((first or {}).get("query", {}), limit=limit, offset=offset)
        result["capped"] = len(rows) >= ROW_CAP
        return result

    def by_time(self, params: dict[str, Any], *, limit: int = 100, offset: int = 0) -> dict:
        """Return a time trend from ``stat/v1/data/bytime`` rather than a point-in-time slice."""
        body = self._request(
            self._url(
                f"{API_REPORTS}/bytime", dict(params, accuracy="full", limit=limit, offset=offset)
            )
        )
        spend.record(
            SOURCE,
            "report.bytime",
            cost=1,
            unit="requests",
            items=len((body or {}).get("data") or []),
        )
        return body

    # --- raw logs (Logs API) -----------------------------------------------

    def create_log_request(
        self, counter_id: str | int, source: str, date1: str, date2: str, fields: list[str]
    ) -> dict:
        """Request a raw-log export; ``source`` is either ``visits`` or ``hits``.

        ⚠️ Fields may include ``ym:s:clientID``, which is personal data. Never commit the result
        or expose it in a client-facing report.
        """
        spend.record(
            SOURCE,
            "logs.create",
            cost=1,
            unit="requests",
            items=len(fields),
            extra={"source": source, "period": f"{date1}..{date2}"},
        )
        body = self._request(
            self._url(
                f"{API_MANAGEMENT}/counter/{counter_id}/logrequests",
                {"source": source, "date1": date1, "date2": date2, "fields": ",".join(fields)},
            ),
            method="POST",
        )
        return (body or {}).get("log_request", body)

    def log_requests(self, counter_id: str | int) -> list[dict]:
        return (
            self._request(self._url(f"{API_MANAGEMENT}/counter/{counter_id}/logrequests")) or {}
        ).get("requests", [])

    def log_request(self, counter_id: str | int, request_id: int) -> dict:
        """Find one request by ID; the API provides no dedicated single-request endpoint."""
        for item in self.log_requests(counter_id):
            if item.get("request_id") == request_id:
                return item
        raise MetrikaError(404, f"log request {request_id} not found")

    def download_log_part(self, counter_id: str | int, request_id: int, part: int) -> str:
        """Download one completed export part as raw TSV text.

        The caller decides where to persist it, and that path must be covered by ``.gitignore``.
        """
        return self._request(
            self._url(
                f"{API_MANAGEMENT}/counter/{counter_id}/logrequest/{request_id}"
                f"/part/{part}/download"
            ),
            raw=True,
        )

    def cancel_log_request(self, counter_id: str | int, request_id: int) -> dict:
        """Cancel a request to release one of the limited Logs API request slots."""
        body = self._request(
            self._url(
                f"{API_MANAGEMENT}/counter/{counter_id}/logrequest/{request_id}/cancel",
                {"request_id": request_id},
            ),
            method="POST",
        )
        return (body or {}).get("log_request", body)


def _api_message(text: str) -> str:
    """Extract a readable API message from Metrica's ``message`` or ``errors`` fields."""
    try:
        body = json.loads(text)
    except ValueError:
        return text[:300] or "empty response"
    if isinstance(body, dict):
        if body.get("message"):
            return str(body["message"])
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message") or first)
            return str(first)
    return text[:300]


def rows_to_records(report: dict) -> list[dict]:
    """Flatten a report into ``{dimension: name, metric: number}`` records.

    Metrica returns dimensions and metrics as parallel arrays rather than pairs. Centralizing the
    mapping prevents a caller from shifting columns during one-off parsing.
    """
    query = report.get("query") or {}
    dimensions = [d.split(":")[-1] for d in (query.get("dimensions") or [])]
    metrics = [m.split(":")[-1] for m in (query.get("metrics") or [])]
    records = []
    for row in report.get("data") or []:
        record: dict[str, Any] = {}
        for index, dimension in enumerate(row.get("dimensions") or []):
            key = dimensions[index] if index < len(dimensions) else f"dimension_{index}"
            record[key] = dimension.get("name") if isinstance(dimension, dict) else dimension
        for index, value in enumerate(row.get("metrics") or []):
            key = metrics[index] if index < len(metrics) else f"metric_{index}"
            record[key] = value
        records.append(record)
    return records
