"""DataForSEO client for Google volume, keyword ideas, difficulty, and SERPs.

Yandex Wordstat and Arsenkin cover Yandex and the Russian-language search market. Projects
outside that market, including English-language, Indian, and Gulf-region sites, primarily depend
on Google data. This client adds that demand layer to the technical evidence collected elsewhere.

**Provider boundaries are strict.** DataForSEO serves Google and international markets. Yandex
and Russian-language markets use :mod:`yandex_cloud` and :mod:`arsenkin`. Do not mix these paths:
DataForSEO does not support locations in Russia or Belarus across its services. The geographic
guard therefore prevents Russian or Belarusian requests
from reaching the network and returns suitable alternatives instead. Without the guard, a paid
request can return an empty result, charging the account for no usable data.

**Sandbox is the default.** Responses use the provider's schema but contain synthetic data and do
not incur charges. Production mode requires the explicit ``DATAFORSEO_ENV=prod`` setting or
``env="prod"`` argument. The client never switches automatically: pipelines should be built and
validated in the sandbox, while spending money must be a deliberate decision.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

from seohead.data_sources import spend
from seohead.data_sources.credentials import (
    MissingCredential,
    dataforseo_login,
    dataforseo_password,
)

PROD_BASE = "https://api.dataforseo.com/"
SANDBOX_BASE = "https://sandbox.dataforseo.com/"
SOURCE = "dataforseo"
TIMEOUT = 120
RETRIES = 3

# These countries are unavailable, not merely poorly supported. Russian aliases below are
# functional input data and intentionally remain localized.
UNSUPPORTED = {
    "RU": "Russia is unavailable in Labs, Google Ads, and SERP",
    "BY": "Belarus locations are unavailable across DataForSEO services",
}
COUNTRY_ALIASES = {
    "россия": "RU",
    "russia": "RU",
    "russian federation": "RU",
    "рф": "RU",
    "беларусь": "BY",
    "белоруссия": "BY",
    "belarus": "BY",
    "рб": "BY",  # noqa: RUF001 - Functional Russian abbreviation for Belarus.
}
# ``location_code`` is the field DataForSEO actually bills on; ``country`` is only advisory text
# a caller may never fill in. These are the country-level Google Ads geo-target IDs for Russia
# and Belarus (the two markets DataForSEO excludes).
UNSUPPORTED_LOCATION_CODES: dict[int, str] = {
    2643: "RU",
    2112: "BY",
}
FALLBACK_TOOLS = [
    "Yandex Wordstat via Yandex Cloud for volume and expansion (keywords-expand)",
    "Arsenkin for exact !W volume and SERP clustering (keywords-exact)",
    "Yandex SERP via serp-fetch",
]

# Google endpoints currently required by the public toolkit.
ENDPOINTS = {
    "search_volume": "v3/keywords_data/google_ads/search_volume/live",
    "keyword_ideas": "v3/dataforseo_labs/google/keyword_ideas/live",
    "keyword_difficulty": "v3/dataforseo_labs/google/bulk_keyword_difficulty/live",
    "serp": "v3/serp/google/organic/live/advanced",
}


class DataForSEOError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"DataForSEO {status}: {message}")
        self.status = status
        self.message = message


def _rejection(iso: str) -> dict:
    return {
        "ok": False,
        "unsupported_geo": iso,
        "error": f"DataForSEO does not cover {iso}: {UNSUPPORTED[iso]}",
        "use_instead": FALLBACK_TOOLS,
    }


def geo_guard(country: str | None, location_code: int | None = None) -> dict | None:
    """Return a rejection for unsupported geographies, or ``None`` when a call is allowed.

    This is a cost guard, not defensive overreach. Without it, a Russian request can reach the
    paid API, incur a charge, and return an empty list. ``location_code`` is checked first
    because it is the field actually transmitted in the request body; ``country`` is only an
    advisory string that a caller can supply a numeric geo-target without ever filling in.
    """
    if location_code is not None:
        try:
            code = int(location_code)
        except (TypeError, ValueError):
            code = None
        if code in UNSUPPORTED_LOCATION_CODES:
            return _rejection(UNSUPPORTED_LOCATION_CODES[code])
    if not country:
        return None
    iso = COUNTRY_ALIASES.get(country.strip().lower(), country.strip().upper())
    if iso not in UNSUPPORTED:
        return None
    return _rejection(iso)


class DataForSEOClient:
    def __init__(
        self, login: str | None = None, password: str | None = None, env: str | None = None
    ):
        import os

        self.env = (env or os.environ.get("DATAFORSEO_ENV") or "sandbox").lower()
        self.base = PROD_BASE if self.env == "prod" else SANDBOX_BASE
        self._login = login
        self._password = password

    @property
    def _auth(self) -> str:
        login = self._login or dataforseo_login()
        password = self._password or dataforseo_password()
        return "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()

    def post(self, endpoint: str, payload: list[dict], *, operation: str | None = None) -> dict:
        """POST to an endpoint; DataForSEO always expects a task list, even for one task.

        An HTTP error response (429/5xx) means the provider replied, so retrying it is the
        ordinary safe case and is unchanged. A network-level exception means the response was
        lost, not that the request never arrived: with no idempotency key on this endpoint, the
        provider may already have created and billed the task, so it is not retried. The attempt
        is logged before the exception is raised, so a lost response is not an untracked charge.
        """
        url = self.base + endpoint.lstrip("/")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        label = operation or endpoint
        attempt = 0
        while True:
            attempt += 1
            request = urllib.request.Request(
                url,
                data=data,
                method="POST",
                headers={"Authorization": self._auth, "Content-Type": "application/json"},
            )
            try:
                # The request URL is built from the fixed HTTPS provider base.
                with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
                    raw = response.read().decode("utf-8")
                    try:
                        return json.loads(raw)
                    except ValueError:
                        # The response was received — a receipt must exist even though it
                        # cannot be parsed, so a real charge is never silently untracked. This
                        # is neither a confirmed charge nor a confirmed zero cost, so cost stays
                        # unmeasured and both flags say so explicitly.
                        spend.record(
                            SOURCE,
                            label,
                            cost=0.0,
                            unit="usd",
                            extra={
                                "response_received": True,
                                "response_malformed": True,
                                "charge_status": "unknown",
                                "cost_unknown": True,
                                "status": response.status,
                            },
                        )
                        raise
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace")
                if exc.code in (429, 500, 502, 503) and attempt <= RETRIES:
                    time.sleep(2**attempt)
                    continue
                raise DataForSEOError(exc.code, _message(text)) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                spend.record(
                    SOURCE,
                    label,
                    cost=0.0,
                    unit="usd",
                    extra={"attempt_failed": "network_error", "detail": str(exc)},
                )
                raise DataForSEOError(
                    0, f"network error, response lost; task may already be billed: {exc}"
                ) from None

    def balance(self) -> dict:
        """Return account balance; the sandbox value is synthetic by design."""
        url = self.base + "v3/appendix/user_data"
        request = urllib.request.Request(url, headers={"Authorization": self._auth})
        # The request URL is built from the fixed HTTPS provider base.
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
            body = json.loads(response.read().decode("utf-8"))
        info = ((body.get("tasks") or [{}])[0].get("result") or [{}])[0]
        return {
            "env": self.env,
            "balance": (info.get("money") or {}).get("balance"),
            "limits": info.get("rates"),
        }


def _message(raw: str) -> str:
    try:
        body = json.loads(raw)
    except ValueError:
        return raw[:300] or "empty response"
    if isinstance(body, dict):
        if body.get("status_message"):
            return str(body["status_message"])
        tasks = body.get("tasks")
        if isinstance(tasks, list) and tasks and isinstance(tasks[0], dict):
            return str(tasks[0].get("status_message") or raw[:300])
    return raw[:300]


def task_items(body: dict) -> list[dict]:
    """Flatten ``tasks[].result[].items[]`` into one list.

    DataForSEO has three nested levels, and each level may contain ``None`` instead of a list: a
    task can finish without a result, and a result can contain no items. Direct indexing through
    ``[0]["result"][0]["items"]`` fails on those valid responses.
    """
    items: list[dict] = []
    for task in body.get("tasks") or []:
        for result in task.get("result") or []:
            if isinstance(result, dict):
                if "items" in result:
                    nested = result["items"]
                    if isinstance(nested, list):
                        items.extend(x for x in nested if isinstance(x, dict))
                else:
                    items.append(result)
    return items


def task_errors(body: dict) -> list[str]:
    """Return failed task messages; status code 20000 is success and all others are reported."""
    errors = []
    for task in body.get("tasks") or []:
        code = task.get("status_code")
        if code and int(code) != 20000:
            errors.append(f"{code}: {task.get('status_message')}")
    return errors


def _run(
    client: DataForSEOClient, kind: str, payload: list[dict], items_count: int
) -> tuple[list[dict], list[str], float]:
    body = client.post(ENDPOINTS[kind], payload, operation=f"{kind}.{client.env}")
    cost = float(body.get("cost") or 0)
    spend.record(
        SOURCE,
        f"{kind}.{client.env}",
        cost=cost,
        unit="usd",
        items=items_count,
        extra={"env": client.env},
    )
    return task_items(body), task_errors(body), cost


# --- operations -------------------------------------------------------------


def search_volume(
    keywords: list[str],
    *,
    location_code: int = 2840,
    language: str = "en",
    country: str | None = None,
    env: str | None = None,
) -> dict:
    """Return Google search volume for keywords; ``location_code`` 2840 is the United States."""
    blocked = geo_guard(country, location_code)
    if blocked:
        return blocked
    try:
        client = DataForSEOClient(env=env)
        items, errors, cost = _run(
            client,
            "search_volume",
            [
                {
                    "keywords": list(keywords),
                    "location_code": location_code,
                    "language_code": language,
                }
            ],
            len(keywords),
        )
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except DataForSEOError as exc:
        return {"ok": False, "error": exc.message, "status": exc.status}
    return {
        "ok": True,
        "env": client.env,
        "cost_usd": cost,
        "errors": errors,
        "keywords": [
            {
                "phrase": i.get("keyword"),
                "volume": i.get("search_volume"),
                "cpc": i.get("cpc"),
                "competition": i.get("competition"),
            }
            for i in items
        ],
    }


def keyword_ideas(
    seed: str,
    *,
    location_code: int = 2840,
    language: str = "en",
    limit: int = 100,
    country: str | None = None,
    env: str | None = None,
) -> dict:
    """Expand a seed phrase, analogous to Wordstat's left column but for Google."""
    blocked = geo_guard(country, location_code)
    if blocked:
        return blocked
    try:
        client = DataForSEOClient(env=env)
        items, errors, cost = _run(
            client,
            "keyword_ideas",
            [
                {
                    "keywords": [seed],
                    "location_code": location_code,
                    "language_code": language,
                    "limit": int(limit),
                }
            ],
            1,
        )
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except DataForSEOError as exc:
        return {"ok": False, "error": exc.message, "status": exc.status}
    return {
        "ok": True,
        "env": client.env,
        "seed": seed,
        "cost_usd": cost,
        "errors": errors,
        "found": len(items),
        "keywords": [
            {
                "phrase": i.get("keyword"),
                "volume": ((i.get("keyword_info") or {}).get("search_volume")),
                "difficulty": ((i.get("keyword_properties") or {}).get("keyword_difficulty")),
            }
            for i in items
        ],
    }


def keyword_difficulty(
    keywords: list[str],
    *,
    location_code: int = 2840,
    language: str = "en",
    country: str | None = None,
    env: str | None = None,
) -> dict:
    """Return bulk keyword difficulty: the estimated effort required to reach top results."""
    blocked = geo_guard(country, location_code)
    if blocked:
        return blocked
    try:
        client = DataForSEOClient(env=env)
        items, errors, cost = _run(
            client,
            "keyword_difficulty",
            [
                {
                    "keywords": list(keywords),
                    "location_code": location_code,
                    "language_code": language,
                }
            ],
            len(keywords),
        )
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except DataForSEOError as exc:
        return {"ok": False, "error": exc.message, "status": exc.status}
    return {
        "ok": True,
        "env": client.env,
        "cost_usd": cost,
        "errors": errors,
        "keywords": [
            {"phrase": i.get("keyword"), "difficulty": i.get("keyword_difficulty")} for i in items
        ],
    }


def serp(
    query: str,
    *,
    location_code: int = 2840,
    language: str = "en",
    depth: int = 10,
    country: str | None = None,
    env: str | None = None,
) -> dict:
    """Return the live Google organic results that currently rank for a query."""
    blocked = geo_guard(country, location_code)
    if blocked:
        return blocked
    try:
        client = DataForSEOClient(env=env)
        items, errors, cost = _run(
            client,
            "serp",
            [
                {
                    "keyword": query,
                    "location_code": location_code,
                    "language_code": language,
                    "depth": int(depth),
                }
            ],
            1,
        )
    except MissingCredential as exc:
        return {"ok": False, "error": str(exc)}
    except DataForSEOError as exc:
        return {"ok": False, "error": exc.message, "status": exc.status}
    organic = [i for i in items if i.get("type") == "organic"] or items
    return {
        "ok": True,
        "env": client.env,
        "query": query,
        "cost_usd": cost,
        "errors": errors,
        "docs": [
            {
                "pos": i.get("rank_absolute"),
                "url": i.get("url"),
                "domain": i.get("domain"),
                "title": i.get("title"),
            }
            for i in organic
        ],
    }
