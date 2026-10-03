"""Yandex Metrica API client for traffic, goals, counter settings, and raw logs.

A crawl shows what a website **contains**, Wordstat shows the **demand** for it, and Metrica
shows what visitors **actually did**. Without analytics, a client report relies on assumptions:
a technically excellent page may receive no visits, while a weaker page may attract substantial
traffic. Metrica therefore completes the client-onboarding data model by joining analytics with
crawl evidence in the knowledge system.

The client includes four operational safeguards:

* retries inspect the real HTTP status and **honor the ``Retry-After`` header** on HTTP 429;
* a ``Query is too complicated`` refusal is retried in calendar-month slices with a backoff
  and, when a slice still refuses, at a sampled accuracy; only count metrics that are
  additive over disjoint periods are merged — distinct-visitor, ratio, and unknown metrics
  refuse rather than sum into a wrong full-period answer — and the merge is streamed
  under one global cap on raw rows downloaded across all slices, so once the budget is
  spent the remaining partitions are never requested;
* ``offset``/``limit`` pagination has a row ceiling so an accidental query cannot download a
  million rows, plus an inter-page delay so thousands of sequential pages do not exhaust quota;
* exceptions carry the status and message returned by the API instead of a generic
  ``request failed`` string.

⚠️ **Privacy.** Logs API exports can contain raw ``ClientID`` values, which are visitor personal
data. Never commit these exports or include them in client reports. This downloader returns text
only; callers choose where to persist it, and that path must be covered by ``.gitignore``.
"""

from __future__ import annotations

import calendar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# "Query is too complicated" is a workload refusal, not a syntax error: the same query
# succeeds over a shorter period or a smaller sample. A refused period is retried in
# calendar-month slices; a slice that still refuses is resubmitted at a sampled accuracy.
COMPLEXITY_ATTEMPTS = 3  # Attempts per slice before the accuracy is degraded.
COMPLEXITY_PAUSE = 5.0  # Seconds between attempts; grows toward COMPLEXITY_PAUSE_MAX.
COMPLEXITY_PAUSE_MAX = 15.0
SAMPLED_ACCURACY = 0.1  # Last-resort sample share for a slice that stays too complex.

# Metrics provably additive over disjoint calendar periods: every session and pageview is
# attributed to exactly one day, so monthly counts sum to the full-period count. Unique
# visitors (ym:s:users) overlap between months, percentages and averages cannot be summed,
# and unknown or parameterized ids are unverifiable — none of those may be merged.
ADDITIVE_METRICS = frozenset({"ym:s:visits", "ym:s:pageviews"})

_DAYS_AGO_RE = re.compile(r"^(\d+)daysAgo$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TZ_OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")
_COMPLEXITY_MARKERS = ("too complicated", "слишком сложн")


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
        self, params: dict[str, Any], *, paginate: bool = False, limit: int = 100, offset: int = 1
    ) -> dict:
        """Request ``stat/v1/data``.

        ``offset`` is 1-based, as the Reporting API requires: ``offset=0`` is answered with 400.

        With ``paginate=True``, fetch all pages and combine rows up to :data:`ROW_CAP`. Without
        this ceiling, one grouping typo can accidentally request a million rows.

        A ``Query is too complicated`` refusal is a workload error, not a syntax error: the
        same query succeeds over a shorter period or a smaller sample. The period is then
        retried in calendar-month slices with a backoff, a slice that still refuses is
        resubmitted at :data:`SAMPLED_ACCURACY`, and the merged body states what happened via
        ``split`` and ``accuracy_used``. Only metrics in :data:`ADDITIVE_METRICS` — counts
        provably additive over disjoint periods — may be merged this way; a query that asks
        for anything else is refused instead of returning a summed value that would be
        wrong. Sampling fields reflect what the API actually reported, never the request.
        """
        if offset < 1:
            raise ValueError("offset must be >= 1: the Reporting API numbers report rows from 1")
        base = {"accuracy": "full", **params}
        try:
            return self._fetch_report(base, paginate=paginate, limit=limit, offset=offset)
        except MetrikaError as exc:
            if not _is_too_complicated(exc):
                raise
            return self._split_report(
                base, paginate=paginate, limit=limit, offset=offset, cause=exc
            )

    def _fetch_report(
        self,
        base: dict[str, Any],
        *,
        paginate: bool,
        limit: int,
        offset: int,
        row_budget: int | None = None,
    ) -> dict:
        if not paginate:
            try:
                body = self._request(self._url(API_REPORTS, dict(base, limit=limit, offset=offset)))
            except MetrikaError:
                # A refused request still consumed provider quota; journal it so a
                # complexity refusal does not look like it never reached the API.
                spend.record(
                    SOURCE,
                    "report",
                    cost=1,
                    unit="requests",
                    items=0,
                    extra={"metrics": base.get("metrics"), "outcome": "failed"},
                )
                raise
            spend.record(
                SOURCE,
                "report",
                cost=1,
                unit="requests",
                items=len((body or {}).get("data") or []),
                extra={"metrics": base.get("metrics")},
            )
            return body

        # ``row_budget`` is this call's share of the single raw-row cap: a split
        # period spends what earlier slices left rather than a fresh ROW_CAP each.
        budget = max(1, ROW_CAP if row_budget is None else row_budget)
        page_size = min(max(limit, 100), 1000, budget)
        first: dict | None = None
        rows: list = []
        cursor = offset
        pages = 0
        collected = offset - 1
        try:
            while True:
                # The budget counts downloaded rows, so each page may ask only for
                # the share still left — a full-size request past the remainder
                # would pull rows the cap can no longer hold.
                page_limit = min(page_size, budget - collected)
                if page_limit <= 0:
                    break
                page = self._request(
                    self._url(API_REPORTS, dict(base, limit=page_limit, offset=cursor))
                )
                pages += 1
                if first is None:
                    first = page
                chunk = page.get("data") or []
                rows.extend(chunk)
                collected = offset - 1 + len(rows)
                # A response without ``data`` has nothing else to aggregate; stop cleanly.
                if len(chunk) < page_limit:
                    break
                if collected >= budget:
                    break
                total = (first or {}).get("total_rows")
                if total and collected >= total:
                    break
                cursor += page_limit
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
                extra={"metrics": base.get("metrics"), "pages": pages, "outcome": "failed"},
            )
            raise

        spend.record(
            SOURCE,
            "report.paginated",
            cost=pages,
            unit="requests",
            items=len(rows),
            extra={"metrics": base.get("metrics"), "pages": pages},
        )
        result = dict(first or {})
        result["data"] = rows
        result["query"] = dict((first or {}).get("query", {}), limit=limit, offset=offset)
        # ``capped`` means the budget stopped the download while more rows may exist:
        # a ``total_rows`` the API vouches for and that was fully collected is complete,
        # not capped — hitting the ceiling exactly on the last row is not a truncation.
        total = (first or {}).get("total_rows")
        result["capped"] = collected >= budget and (not _is_number(total) or collected < total)
        return result

    def _report_timezone(self, base: dict[str, Any]) -> tzinfo | None:
        """The timezone the API applies to the report period, or ``None`` when unknown.

        The request's ``timezone`` parameter wins; otherwise the counter's own zone is
        looked up through the Management API. The host clock is never substituted — the
        API resolves ``today``/``yesterday``/``NdaysAgo`` in the counter's zone, so a
        guess here would slice the wrong days.
        """
        tz = _parse_timezone(base.get("timezone"))
        if tz is not None:
            return tz
        first = str(base.get("ids") or "").split(",")[0].strip()
        if not first:
            return None
        try:
            body = self.counter(first) or {}
        except MetrikaError:
            return None
        info = body.get("counter") if isinstance(body.get("counter"), dict) else body
        if not isinstance(info, dict):
            return None
        name = info.get("time_zone_name")
        if isinstance(name, str) and name:
            try:
                return ZoneInfo(name)
            except (ZoneInfoNotFoundError, ValueError):
                pass
        offset = info.get("time_zone_offset")
        if _is_number(offset):
            return timezone(timedelta(minutes=int(offset)))
        return None

    def _split_report(
        self,
        base: dict[str, Any],
        *,
        paginate: bool,
        limit: int,
        offset: int,
        cause: MetrikaError,
    ) -> dict:
        """Collect a refused period in calendar-month slices and merge the bodies.

        Summing slice values is valid only for metrics that are additive over disjoint
        periods (:data:`ADDITIVE_METRICS`); anything else is refused before the first
        slice request so it cannot burn calls and then be mislabeled as complete.
        """
        metrics = _csv_ids(base.get("metrics"))
        unsupported = [m for m in metrics if m not in ADDITIVE_METRICS]
        if unsupported:
            spend.record(
                SOURCE,
                "report.split",
                cost=0,
                unit="requests",
                items=0,
                extra={
                    "metrics": base.get("metrics"),
                    "outcome": "unsupported",
                    "unsupported": unsupported,
                },
            )
            raise MetrikaError(
                cause.status,
                f"cannot merge a split of {base.get('date1')}..{base.get('date2')} "
                f"(cause: {cause.message}): metrics {', '.join(unsupported)} are not "
                "additive over disjoint periods, so a summed full-period value would be "
                "wrong — request the period month by month and aggregate accordingly",
            )
        tz = None
        if _needs_today(base.get("date1")) or _needs_today(base.get("date2")):
            tz = self._report_timezone(base)
        today = _now().astimezone(tz).date() if tz is not None else None
        period = _resolve_period(base.get("date1"), base.get("date2"), today=today)
        # An unresolvable or single-day period cannot be sliced: it is retried as-is and
        # only the accuracy ladder can still help.
        spans = _month_slices(*period) if period else [None]
        merger = _SliceMerger(cap=ROW_CAP, n_metrics=len(metrics))
        slices: list[dict] = []
        unfetched: list[dict] = []
        slice_capped = False
        downloaded = 0
        first_query: dict = {}
        for index, span in enumerate(spans):
            if downloaded >= ROW_CAP:
                # The raw-row budget ran out on earlier partitions: this span and
                # every later one are named as unfetched instead of downloading
                # months x ROW_CAP rows behind a capped answer.
                unfetched = [_slice_span(s) for s in spans[index:] if s is not None]
                break
            chunk = dict(base)
            if span is not None:
                chunk["date1"], chunk["date2"] = span[0].isoformat(), span[1].isoformat()
            try:
                body, accuracy, attempts = self._fetch_slice(
                    chunk, limit=limit, row_budget=ROW_CAP - downloaded
                )
            except MetrikaError as exc:
                completed = ", ".join(f"{s['date1']}..{s['date2']}" for s in slices) or "none"
                spend.record(
                    SOURCE,
                    "report.split",
                    cost=0,
                    unit="requests",
                    items=len(merger.rows()),
                    extra={
                        "metrics": base.get("metrics"),
                        "slices": index,
                        "outcome": "failed",
                    },
                )
                raise MetrikaError(
                    exc.status,
                    f"report split of {base.get('date1')}..{base.get('date2')} failed at "
                    f"partition {chunk.get('date1')}..{chunk.get('date2')}: {exc.message}; "
                    f"completed partitions: {completed}",
                ) from exc
            downloaded += len(body.get("data") or [])
            if index == 0:
                first_query = body.get("query") or {}
            slice_capped = slice_capped or bool(body.get("capped"))
            dropped = merger.add(body)
            slices.append(
                {
                    "date1": chunk.get("date1"),
                    "date2": chunk.get("date2"),
                    "accuracy": accuracy,
                    "attempts": attempts,
                    "rows": len(body.get("data") or []),
                    "rows_dropped": dropped,
                    "capped": bool(body.get("capped")),
                    "sampled": body.get("sampled"),
                    "sample_share": body.get("sample_share"),
                    "sample_size": body.get("sample_size"),
                    "sample_space": body.get("sample_space"),
                    "contains_sensitive_data": body.get("contains_sensitive_data"),
                    "data_lag": body.get("data_lag"),
                }
            )
            if body.get("capped") or merger.overflowed:
                # A truncated slice leaves the tail of its own span unread, and the
                # partitions after it are never requested: name both, because the
                # merged window is partial rather than a proven whole-period top.
                if body.get("capped") and span is not None:
                    unfetched.append(_slice_span(span, partial=True))
                unfetched += [_slice_span(s) for s in spans[index + 1 :] if s is not None]
                break
            if index + 1 < len(spans):
                time.sleep(PAGE_PAUSE)
        rows = merger.rows()
        unapplied_sort = _sort_rows(
            rows, base.get("sort"), base.get("metrics"), base.get("dimensions")
        )
        accuracy = _accuracy_used([s["accuracy"] for s in slices])
        collected = not slice_capped and not merger.overflowed and not unfetched
        # Rows dropped by the cap, slices that hit their own row ceiling, partitions
        # never fetched, unsummable cells, or unverifiable sampling all make the
        # merged evidence incomplete rather than a clean full-period answer.
        incomplete = merger.incomplete or bool(unfetched)
        if any(s.get("sampled") is None or not _is_number(s.get("sample_share")) for s in slices):
            incomplete = True
        totals = None if (merger.totals_missing or unfetched) else merger.totals
        window_end = ROW_CAP if paginate else max(offset - 1, 0) + limit
        result: dict[str, Any] = {
            "data": rows[max(offset - 1, 0) : window_end],
            # A merged total exists only when every row of every slice was collected.
            "total_rows": len(rows) if collected else None,
            "totals": totals,
            "capped": not collected,
            "incomplete": not collected or incomplete,
            "accuracy_used": accuracy,
            "query": dict(
                first_query,
                date1=base.get("date1"),
                date2=base.get("date2"),
                limit=limit,
                offset=offset,
                accuracy=accuracy,
            ),
            "split": {
                "reason": cause.message,
                "requested": {"date1": base.get("date1"), "date2": base.get("date2")},
                "resolved": (
                    {"date1": period[0].isoformat(), "date2": period[1].isoformat()}
                    if period
                    else None
                ),
                "timezone": str(tz) if tz is not None else None,
                "periods": slices,
                "unfetched": unfetched or None,
                "metrics": metrics,
                "rows_dropped": merger.dropped_rows,
                "unapplied_sort": unapplied_sort or None,
                "note": (
                    "rows are summed per dimension key across calendar-month slices; "
                    "only count metrics additive over disjoint periods are merged; "
                    "one raw-row cap bounds what all slices may download together — "
                    "once it is spent the current span's tail and the remaining "
                    "partitions stay unfetched and the returned window is partial, "
                    "not a proven top of the full period"
                ),
            },
        }
        # Sampling and sensitivity describe what the API actually did, not what was
        # requested: a slice asked at accuracy=0.1 may still answer sampled=false, and
        # a slice that says nothing leaves the whole-period state unknown — never an
        # invented "false".
        reported_sampled = [s["sampled"] for s in slices if "sampled" in s]
        if any(reported_sampled):
            result["sampled"] = True
        elif reported_sampled and all(v is False for v in reported_sampled):
            result["sampled"] = False
        shares = [s["sample_share"] for s in slices]
        if shares and all(_is_number(v) for v in shares):
            result["sample_share"] = min(shares)
        reported_sensitive = [
            s["contains_sensitive_data"] for s in slices if "contains_sensitive_data" in s
        ]
        if any(reported_sensitive):
            result["contains_sensitive_data"] = True
        elif reported_sensitive and all(v is False for v in reported_sensitive):
            result["contains_sensitive_data"] = False
        # ``min``/``max`` are recomputed over the merged rows — never copied from a slice,
        # because summed rows have their own bounds; per-slice ``sample_size``,
        # ``sample_space`` and ``data_lag`` stay inside ``split.periods`` since they have
        # no documented whole-period aggregation.
        if collected and not incomplete and rows:
            result["min"], result["max"] = _metric_bounds(rows)
        spend.record(
            SOURCE,
            "report.split",
            cost=0,
            unit="requests",
            items=len(result["data"]),
            extra={
                "metrics": base.get("metrics"),
                "slices": len(slices),
                "accuracy": accuracy,
                "outcome": "complete" if collected and not incomplete else "partial",
            },
        )
        return result

    def _fetch_slice(
        self, chunk: dict[str, Any], *, limit: int, row_budget: int
    ) -> tuple[dict, Any, int]:
        """Fetch one period slice, retrying the complexity error then degrading accuracy.

        Returns ``(body, accuracy, attempts)`` so the merged report can state the
        accuracy and request count each partition needed. The slice is always collected
        paginated — merging needs every row of the slice, not just the caller's window —
        but never more than ``row_budget`` raw rows, its share of the single download
        cap the whole split obeys.
        """
        ladder = _accuracy_ladder(chunk.get("accuracy", "full"))
        attempts = 0
        last: MetrikaError | None = None
        for accuracy in ladder:
            for attempt in range(COMPLEXITY_ATTEMPTS):
                attempts += 1
                try:
                    return (
                        self._fetch_report(
                            dict(chunk, accuracy=accuracy),
                            paginate=True,
                            limit=limit,
                            offset=1,
                            row_budget=row_budget,
                        ),
                        accuracy,
                        attempts,
                    )
                except MetrikaError as exc:
                    if not _is_too_complicated(exc):
                        raise
                    last = exc
                    if attempt + 1 < COMPLEXITY_ATTEMPTS:
                        time.sleep(min(COMPLEXITY_PAUSE * (attempt + 1), COMPLEXITY_PAUSE_MAX))
        tried = ", ".join(str(a) for a in ladder)
        raise MetrikaError(
            last.status if last else 0,
            f"still too complicated after {attempts} attempts (accuracies tried: {tried})"
            + (f": {last.message}" if last else ""),
        )

    def by_time(self, params: dict[str, Any], *, limit: int = 100, offset: int = 1) -> dict:
        """Return a time trend from ``stat/v1/data/bytime`` rather than a point-in-time slice."""
        if offset < 1:
            raise ValueError("offset must be >= 1: the Reporting API numbers report rows from 1")
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


def _is_too_complicated(exc: MetrikaError) -> bool:
    """Match the API's workload refusal in either response language."""
    message = (exc.message or "").lower()
    return any(marker in message for marker in _COMPLEXITY_MARKERS)


def _now() -> datetime:
    """Current instant in UTC, kept behind a function so tests can freeze it."""
    return datetime.now(timezone.utc)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _csv_ids(value: Any) -> list[str]:
    """A comma-separated API id list, or a list that already arrived split."""
    if isinstance(value, list | tuple):
        return [str(item) for item in value]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _needs_today(value: Any) -> bool:
    """Whether a date parameter is relative and needs a resolved "today"."""
    return isinstance(value, str) and (
        value in ("today", "yesterday") or _DAYS_AGO_RE.match(value) is not None
    )


def _parse_timezone(value: Any) -> tzinfo | None:
    """Parse the API ``timezone`` parameter: a ``±hh:mm`` offset or an IANA zone name."""
    if not isinstance(value, str):
        return None
    match = _TZ_OFFSET_RE.match(value)
    if match:
        sign = -1 if match.group(1) == "-" else 1
        hours, minutes = int(match.group(2)), int(match.group(3))
        if hours > 23 or minutes > 59:
            return None
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _resolve_date(value: Any, today: date | None) -> date | None:
    """Map an absolute or relative API date onto a calendar day; ``None`` when unknown.

    Relative forms need ``today`` in the report's timezone; without it they stay
    unresolvable rather than borrowing the host date.
    """
    if not isinstance(value, str):
        return None
    if _ISO_DATE_RE.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    if today is None:
        return None
    if value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    match = _DAYS_AGO_RE.match(value)
    return today - timedelta(days=int(match.group(1))) if match else None


def _resolve_period(
    date1: Any, date2: Any, *, today: date | None = None
) -> tuple[date, date] | None:
    """Resolve the requested period to calendar dates; ``None`` when it cannot be split."""
    start, end = _resolve_date(date1, today), _resolve_date(date2, today)
    if start is None or end is None or start >= end:
        return None
    return start, end


def _month_slices(start: date, end: date) -> list[tuple[date, date]]:
    """Calendar-month-aligned slices covering ``start``..``end`` inclusive."""
    slices = []
    cursor = start
    while cursor <= end:
        month_end = date(
            cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1]
        )
        slice_end = min(month_end, end)
        slices.append((cursor, slice_end))
        cursor = slice_end + timedelta(days=1)
    return slices


def _slice_span(span: tuple[date, date], *, partial: bool = False) -> dict:
    """An unfetched period for ``split.unfetched``; ``partial`` marks a truncated slice."""
    entry: dict[str, Any] = {"date1": span[0].isoformat(), "date2": span[1].isoformat()}
    if partial:
        entry["partial"] = True
    return entry


def _metric_value(row: dict, index: int) -> float:
    values = row.get("metrics") or []
    value = values[index] if index < len(values) else None
    return value if _is_number(value) else 0


def _dimension_value(row: dict, index: int) -> str:
    values = row.get("dimensions") or []
    value = values[index] if index < len(values) else None
    if isinstance(value, dict):
        value = value.get("name")
    return "" if value is None else str(value)


def _sum_metrics(left: list, right: list) -> tuple[list, bool]:
    """Sum two metric vectors cell-wise.

    A cell sums only when both sides are numeric: a missing or suppressed value is not
    zero, so ``(5, None)`` merges to ``None`` plus an incomplete flag rather than a
    confident ``5``.
    """
    out = []
    incomplete = False
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else None
        b = right[index] if index < len(right) else None
        if _is_number(a) and _is_number(b):
            out.append(a + b)
        elif a is None and b is None:
            out.append(None)
        else:
            out.append(None)
            incomplete = True
    return out, incomplete


class _SliceMerger:
    """Streaming accumulator that sums additive metrics per dimension key.

    At most ``cap`` distinct dimension keys are retained, so merged size stays bounded
    no matter how many slices arrive — the months x cap raw bodies are never held at
    once, and the caller's raw-row budget stops the downloads long before this bound
    matters. A row that cannot be kept is counted in ``dropped_rows`` (a count of row
    occurrences, not a stored key set, so the bound holds), and ``overflowed`` tells the
    caller the budget is exhausted and later partitions should not be fetched. Rows
    and ``totals`` vectors are validated against the requested metric count on first
    insertion: a missing or suppressed cell is incomplete evidence, never zero.
    """

    def __init__(self, cap: int, n_metrics: int):
        self._cap = cap
        self._n = n_metrics
        self._merged: dict[str, dict] = {}
        self._order: list[str] = []
        self.totals: list | None = None
        self.totals_missing = False
        self.dropped_rows = 0
        self.overflowed = False
        self.incomplete = False

    def _padded(self, values: Any) -> tuple[list, bool]:
        """Validate a metric vector against the requested count and pad missing cells."""
        if not isinstance(values, list):
            values = []
        bad = len(values) != self._n or not all(_is_number(v) for v in values)
        return [values[i] if i < len(values) else None for i in range(self._n)], bad

    def add(self, body: dict) -> int:
        """Merge one slice body; returns how many rows were dropped by the cap."""
        dropped = 0
        for row in body.get("data") or []:
            values, bad = self._padded(row.get("metrics"))
            self.incomplete = self.incomplete or bad
            key = json.dumps(row.get("dimensions"), sort_keys=True, ensure_ascii=False)
            existing = self._merged.get(key)
            if existing is None:
                if len(self._merged) >= self._cap:
                    dropped += 1
                    self.overflowed = True
                    continue
                self._merged[key] = dict(row, metrics=values)
                self._order.append(key)
            else:
                summed, bad = _sum_metrics(existing["metrics"], values)
                existing["metrics"] = summed
                self.incomplete = self.incomplete or bad
        self.dropped_rows += dropped
        slice_totals = body.get("totals")
        if isinstance(slice_totals, list):
            padded, bad = self._padded(slice_totals)
            self.incomplete = self.incomplete or bad
            if self.totals is None:
                self.totals = padded
            else:
                self.totals, bad = _sum_metrics(self.totals, padded)
                self.incomplete = self.incomplete or bad
        else:
            # A slice without totals cannot contribute to a whole-period total.
            self.totals_missing = True
            self.incomplete = True
        return dropped

    def rows(self) -> list[dict]:
        return [self._merged[key] for key in self._order]


def _accuracy_ladder(accuracy: Any) -> list:
    """Accuracies to try for a refusing slice, most precise first.

    The API's documented forms are ``low`` < ``medium`` < ``high`` < ``full`` and a
    numeric share in (0, 1] where ``1`` equals ``full`` — numeric strings such as
    ``"1"`` are normalized to numbers before comparison. A request for more data
    than the last-resort :data:`SAMPLED_ACCURACY` gets one smaller-sample retry;
    ``low`` or a share at/below the last resort keeps the caller's already lower
    setting instead of being resampled upward.
    """
    return [accuracy, SAMPLED_ACCURACY] if _accuracy_above_last_resort(accuracy) else [accuracy]


def _accuracy_above_last_resort(value: Any) -> bool:
    """Whether an accuracy form asks for more data than :data:`SAMPLED_ACCURACY`."""
    if _is_number(value):
        return float(value) > SAMPLED_ACCURACY
    if isinstance(value, str):
        if value.strip().lower() == "low":
            return False
        try:
            return float(value) > SAMPLED_ACCURACY
        except ValueError:
            pass
    # full/high/medium and unrecognized forms still get the smaller-sample retry: a
    # complexity refusal can only be answered by asking for less data.
    return True


def _sort_rows(rows: list[dict], sort: Any, metrics: Any, dimensions: Any) -> list[str]:
    """Order merged rows like one API answer and return the tokens that did not apply.

    Explicit ``sort`` tokens address either a metric (numeric) or a dimension (string);
    without ``sort`` the API default is the first metric, descending. Each token is a
    separate stable sort applied from last to first, so multi-key ordering survives.
    """
    metric_ids = _csv_ids(metrics)
    dimension_ids = _csv_ids(dimensions)
    tokens = _csv_ids(sort)
    if not tokens and metric_ids:
        tokens = [f"-{metric_ids[0]}"]
    unapplied: list[str] = []
    for token in reversed(tokens):
        descending = token.startswith("-")
        name = token.lstrip("-")
        if name in metric_ids:
            index = metric_ids.index(name)
            rows.sort(key=lambda row, i=index: _metric_value(row, i), reverse=descending)
        elif name in dimension_ids:
            index = dimension_ids.index(name)
            rows.sort(key=lambda row, i=index: _dimension_value(row, i), reverse=descending)
        else:
            unapplied.append(token)
    return unapplied[::-1]  # report tokens in the caller's order, not sort order


def _metric_bounds(rows: list[dict]) -> tuple[list, list]:
    """Elementwise min/max over merged row metrics; ``None`` where no numeric cell exists."""
    width = max((len(row.get("metrics") or []) for row in rows), default=0)
    mins: list = [None] * width
    maxs: list = [None] * width
    for row in rows:
        for index, value in enumerate(row.get("metrics") or []):
            if not _is_number(value):
                continue
            mins[index] = value if mins[index] is None else min(mins[index], value)
            maxs[index] = value if maxs[index] is None else max(maxs[index], value)
    return mins, maxs


def _accuracy_used(accuracies: list) -> Any:
    """The least precise accuracy any slice needed — the honest bound for the whole period."""
    numeric = [a for a in accuracies if isinstance(a, int | float) and not isinstance(a, bool)]
    if numeric:
        return min(numeric)
    return "full" if all(a == "full" for a in accuracies) else accuracies[-1]


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
