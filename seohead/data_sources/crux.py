"""Chrome UX Report (CrUX): Core Web Vitals as real users experienced them.

Issue #59 deliberately implements only the Lighthouse audits computable without a browser trace
and forbids synthesising a Performance score. CrUX is the honest way to report the metrics that
need one: not synthesised, not lab, measured on real Chrome visits at origin or URL level. It
turns ``SLOW_RESPONSE`` from "the server took a while for us" into "users experience this as
slow" (issue #97).

**This is a credential-gated skeleton, not an exercised client.** CrUX needs a Google Cloud API
key; nothing in this environment can obtain or verify one. Parsing is built and tested against a
recorded response shape, but the request has never reached the live API. A missing key returns
an explicit, truthful failure — never a fabricated result.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from seohead.data_sources.http import open_no_redirect

HOST = "https://chromeuxreport.googleapis.com/v1/records:queryRecord"
HISTORY_HOST = "https://chromeuxreport.googleapis.com/v1/records:queryHistoryRecord"
TIMEOUT = 30
MAX_SAMPLES = 25
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CACHE_BYTES = 64 * 1024


class ResponseTooLarge(ValueError):
    """The CrUX response exceeded the bounded provider input budget."""


# payload, api key -> response body text
Fetcher = Callable[[dict[str, Any], str], str]


def _default_fetcher(payload: dict[str, Any], api_key: str) -> str:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        HOST,
        data=data,
        method="POST",
        # The key travels in a header, never in the query string, so it can never end up
        # echoed into a URL that lands in a log line or an exception message.
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
    )
    with open_no_redirect(request, timeout=TIMEOUT) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge("CrUX response exceeded the 2 MiB limit")
        return raw.decode("utf-8")


def _history_fetcher(payload: dict[str, Any], api_key: str) -> str:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        HISTORY_HOST,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "X-goog-api-key": api_key},
    )
    with open_no_redirect(request, timeout=TIMEOUT) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge("CrUX response exceeded the 2 MiB limit")
        return raw.decode("utf-8")


def _api_error(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            return "CrUX error response exceeded the 2 MiB limit"
        body = json.loads(raw.decode("utf-8", "replace"))
        return str(body.get("error", {}).get("message") or exc.reason)
    except ValueError:
        return str(exc.reason)


def _response_object(raw: str) -> dict[str, Any] | None:
    try:
        body = json.loads(raw)
    except (AttributeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def query(
    *,
    url: str | None = None,
    origin: str | None = None,
    form_factor: str | None = None,
    metrics: list[str] | None = None,
    api_key: str | None = None,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """Return field Core Web Vitals for a URL or an entire origin, at the 75th percentile.

    CrUX reports at either level, never both at once: pass exactly one of ``url``/``origin``.
    A target with too little real-user traffic to be reported is not an error — CrUX returns
    ``NOT_FOUND`` for it, which comes back here as ``ok: true`` with an empty ``metrics``.
    """
    from seohead.data_sources.credentials import MissingCredential, crux_api_key
    from seohead.data_sources.cwv import assess

    if bool(url) == bool(origin):
        raise ValueError("exactly one of url or origin is required")
    context = {
        "target": url or origin,
        "target_kind": "url" if url else "origin",
        "form_factor": form_factor or "ALL_FORM_FACTORS",
        "metric_source": "CrUX current field data",
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    def finish(result: dict[str, Any]) -> dict[str, Any]:
        result = {**context, **result}
        result["assessment"] = assess(result)
        if result.get("state") == "complete" and result["assessment"]["overall"] in {
            "partial",
            "unavailable",
        }:
            result["state"] = "partial"
        return result

    try:
        api_token = api_key or crux_api_key()
    except MissingCredential as exc:
        return finish({"ok": False, "state": "not_configured", "error": str(exc)})

    payload: dict[str, Any] = {"url": url} if url else {"origin": origin}
    if form_factor:
        payload["formFactor"] = form_factor
    if metrics:
        payload["metrics"] = metrics

    fetch = fetcher or _default_fetcher
    try:
        raw = fetch(payload, api_token)
    except ResponseTooLarge as exc:
        return finish({"ok": False, "state": "response_too_large", "error": str(exc)})
    except UnicodeError:
        return finish({"ok": False, "state": "failed", "error": "CrUX malformed response"})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return finish(
                {"ok": True, "state": "no_field_data", "metrics": {}, "note": "no CrUX data"}
            )
        return finish(
            {"ok": False, "state": "failed", "error": _api_error(exc), "status": exc.code}
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        return finish({"ok": False, "state": "failed", "error": f"CrUX request failed: {exc}"})

    if isinstance(raw, str) and len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return finish(
            {
                "ok": False,
                "state": "response_too_large",
                "error": "CrUX response exceeded the 2 MiB limit",
            }
        )
    body = _response_object(raw)
    if body is None:
        return finish({"ok": False, "state": "failed", "error": "CrUX malformed response"})
    record = body.get("record")
    if not isinstance(record, dict):
        return finish({"ok": False, "state": "failed", "error": "CrUX malformed response"})
    record_key = record.get("key", {})
    metric_data = record.get("metrics", {})
    if not isinstance(record_key, dict) or not isinstance(metric_data, dict):
        return finish({"ok": False, "state": "failed", "error": "CrUX malformed response"})
    values_by_metric: dict[str, dict[str, Any]] = {}
    for name, values in metric_data.items():
        if not isinstance(values, dict):
            return finish({"ok": False, "state": "failed", "error": "CrUX malformed response"})
        percentiles = values.get("percentiles", {})
        if not isinstance(percentiles, dict):
            return finish({"ok": False, "state": "failed", "error": "CrUX malformed response"})
        values_by_metric[name] = percentiles
    returned_target = record_key.get("url") if url else record_key.get("origin")
    if record_key.get("origin" if url else "url"):
        return finish({"ok": False, "state": "failed", "error": "CrUX target kind mismatch"})
    if not isinstance(returned_target, str) or not returned_target:
        return finish({"ok": False, "state": "failed", "error": "CrUX malformed response"})
    if record_key.get("formFactor") != form_factor:
        return finish({"ok": False, "state": "failed", "error": "CrUX form factor mismatch"})
    return finish(
        {
            "ok": True,
            "state": "complete",
            "record_target": returned_target,
            "form_factor": record_key.get("formFactor") or form_factor or "ALL_FORM_FACTORS",
            "collection_period": record.get("collectionPeriod"),
            "metrics": {
                name: {"p75": percentiles.get("p75")}
                for name, percentiles in values_by_metric.items()
            },
        }
    )


def _cache_failure(target: str, form_factor: str | None, state: str) -> dict[str, Any]:
    from seohead.data_sources.cwv import assess

    result: dict[str, Any] = {
        "ok": False,
        "state": state,
        "error": state.replace("_", " "),
        "target": target,
        "target_kind": "url",
        "form_factor": form_factor or "ALL_FORM_FACTORS",
        "metric_source": "CrUX current field data",
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "metrics": {},
        "cache": "unavailable",
    }
    result["assessment"] = assess(result)
    return result


def sample_urls(
    urls: list[str],
    *,
    form_factor: str | None = None,
    max_samples: int = MAX_SAMPLES,
    cache_dir: str | Path | None = None,
    cache_max_age_hours: float = 24,
    api_key: str | None = None,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """Explicit bounded CrUX URL sample; optional cache never stores credentials."""
    if (
        not isinstance(urls, list)
        or not urls
        or not all(isinstance(u, str) and u.startswith(("https://", "http://")) for u in urls)
    ):
        raise ValueError("urls must be a nonempty list of absolute HTTP(S) URL strings")
    if (
        isinstance(max_samples, bool)
        or not isinstance(max_samples, int)
        or not 1 <= max_samples <= MAX_SAMPLES
        or not isinstance(cache_max_age_hours, (int, float))
        or not math.isfinite(cache_max_age_hours)
        or cache_max_age_hours <= 0
    ):
        raise ValueError("sample budget must be 1..25 and cache age must be positive")
    targets = list(dict.fromkeys(urls))
    sampled = targets[:max_samples]
    root = Path(cache_dir) if cache_dir else None
    if root and root.is_symlink():
        raise ValueError("cache directory must not be a symlink")
    records: list[dict[str, Any]] = []
    requests = hits = 0
    for target in sampled:
        cache = (
            root
            / ("crux-" + hashlib.sha256(f"{target}\0{form_factor}".encode()).hexdigest() + ".json")
            if root
            else None
        )
        if cache and (cache.exists() or cache.is_symlink()):
            try:
                if cache.is_symlink() or not cache.is_file():
                    raise ValueError("invalid cache path")
                stat = cache.stat()
                if stat.st_size > MAX_CACHE_BYTES:
                    records.append(_cache_failure(target, form_factor, "cache_too_large"))
                    continue
                if time.time() - stat.st_mtime <= cache_max_age_hours * 3600:
                    with cache.open("rb") as stream:
                        raw_cache = stream.read(MAX_CACHE_BYTES + 1)
                    if len(raw_cache) > MAX_CACHE_BYTES:
                        records.append(_cache_failure(target, form_factor, "cache_too_large"))
                        continue
                    cached = json.loads(raw_cache.decode("utf-8"))
                    if not (
                        isinstance(cached, dict)
                        and cached.get("metric_source") == "CrUX current field data"
                        and cached.get("target") == target
                        and cached.get("target_kind") == "url"
                        and cached.get("form_factor") == (form_factor or "ALL_FORM_FACTORS")
                        and isinstance(cached.get("record_target"), (str, type(None)))
                    ):
                        raise ValueError("cache identity mismatch")
                    from seohead.data_sources.cwv import assess

                    cached["assessment"] = assess(cached)
                    cached["cache"] = "hit"
                    records.append(cached)
                    hits += 1
                    continue
            except (OSError, UnicodeError, ValueError):
                records.append(_cache_failure(target, form_factor, "cache_invalid"))
                continue
        result = query(url=target, form_factor=form_factor, api_key=api_key, fetcher=fetcher)
        result["cache"] = "miss" if cache else "disabled"
        requests += 1 if result.get("state") != "not_configured" else 0
        if cache and result.get("ok"):
            encoded = json.dumps(result, sort_keys=True).encode("utf-8")
            if len(encoded) > MAX_CACHE_BYTES:
                result["cache"] = "not_written_too_large"
            else:
                root.mkdir(parents=True, mode=0o700, exist_ok=True)
                descriptor, staged = tempfile.mkstemp(prefix=".crux-", dir=root)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(encoded)
                    os.chmod(staged, 0o600)
                    os.replace(staged, cache)
                finally:
                    Path(staged).unlink(missing_ok=True)
        records.append(result)
    complete = sum(r["assessment"]["overall"] not in {"partial", "unavailable"} for r in records)
    measured_targets = [
        r.get("record_target") or r["target"]
        for r in records
        if r["assessment"]["overall"] not in {"partial", "unavailable"}
    ]
    duplicate_record_targets = len(measured_targets) - len(set(measured_targets))
    return {
        "provider": "crux",
        "metric_source": "field",
        "state": "unavailable"
        if not complete
        else "partial"
        if complete < len(targets) or duplicate_record_targets
        else "complete",
        "requested": len(targets),
        "sampled": len(sampled),
        "omitted": len(targets) - len(sampled),
        "requests": requests,
        "cache_hits": hits,
        "duplicate_record_targets": duplicate_record_targets,
        "cache_max_age_hours": cache_max_age_hours if root else None,
        "cost_mode": "free_within_quota",
        "quota_mode": "Google Cloud API quota",
        "records": records,
    }


def history(
    *,
    url: str | None = None,
    origin: str | None = None,
    form_factor: str | None = None,
    metrics: list[str] | None = None,
    api_key: str | None = None,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """Return reportable CrUX History field trends, distinct from current-record failures."""
    from seohead.data_sources.credentials import MissingCredential, crux_api_key

    if bool(url) == bool(origin):
        raise ValueError("exactly one of url or origin is required")
    try:
        key = api_key or crux_api_key()
    except MissingCredential as exc:
        return {"ok": False, "state": "not_configured", "error": str(exc)}
    payload: dict[str, Any] = {"url": url} if url else {"origin": origin}
    if form_factor:
        payload["formFactor"] = form_factor
    if metrics:
        payload["metrics"] = metrics
    try:
        raw = (fetcher or _history_fetcher)(payload, key)
    except ResponseTooLarge as exc:
        return {"ok": False, "state": "response_too_large", "error": str(exc)}
    except UnicodeError:
        return {"ok": False, "state": "failed", "error": "CrUX History malformed response"}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "ok": True,
                "state": "no_field_data",
                "target": url or origin,
                "records": [],
            }
        return {"ok": False, "state": "failed", "error": _api_error(exc), "status": exc.code}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "state": "failed", "error": f"CrUX request failed: {exc}"}
    if isinstance(raw, str) and len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return {
            "ok": False,
            "state": "response_too_large",
            "error": "CrUX response exceeded the 2 MiB limit",
        }
    body = _response_object(raw)
    record = (body or {}).get("record")
    if not isinstance(record, dict):
        return {"ok": False, "state": "failed", "error": "CrUX History malformed response"}
    key_data = record.get("key")
    metric_data = record.get("metrics")
    if not isinstance(key_data, dict) or not isinstance(metric_data, dict):
        return {"ok": False, "state": "failed", "error": "CrUX History malformed response"}
    return {
        "ok": True,
        "state": "complete",
        "target": url or origin,
        "target_kind": "url" if url else "origin",
        "form_factor": key_data.get("formFactor"),
        "collection_period": record.get("collectionPeriods"),
        "metric_source": "CrUX History field data",
        "metrics": metric_data,
    }
