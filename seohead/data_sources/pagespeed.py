"""Bounded PageSpeed Insights v5 samples, separate from CrUX and local measurements."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

HOST = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
MAX_URLS = 25
TIMEOUT = 90
Fetcher = Callable[[str, str | None], str]


def _default_fetcher(url: str, api_key: str | None) -> str:
    headers = {"X-goog-api-key": api_key} if api_key else {}
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
        return response.read().decode("utf-8")


def _credential(value: str | None) -> str | None:
    from seohead.data_sources.credentials import MissingCredential, read

    try:
        return value or read("pagespeed/api_key", "PAGESPEED_API_KEY")
    except MissingCredential:
        return None


def _cache_path(cache_dir: str | Path, url: str, strategy: str) -> Path:
    digest = hashlib.sha256(f"{url}\0{strategy}".encode()).hexdigest()
    return Path(cache_dir) / f"psi-v5-{digest}.json"


def _write_cache(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, staged = tempfile.mkstemp(prefix=".psi-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        Path(staged).unlink(missing_ok=True)


def _parse(body: dict[str, Any], url: str, strategy: str) -> dict[str, Any] | None:
    lighthouse = body.get("lighthouseResult")
    if not isinstance(lighthouse, dict):
        return None
    categories = lighthouse.get("categories")
    audits = lighthouse.get("audits")
    if not isinstance(categories, dict) or not isinstance(audits, dict):
        return None
    selected_categories = {
        name: {"score": category.get("score"), "title": category.get("title")}
        for name, category in categories.items()
        if isinstance(category, dict)
    }
    selected_audits = {
        name: {
            "score": audit.get("score"),
            "display_value": audit.get("displayValue"),
            "numeric_value": audit.get("numericValue"),
            "score_display_mode": audit.get("scoreDisplayMode"),
        }
        for name, audit in audits.items()
        if isinstance(audit, dict)
    }
    return {
        "url": url,
        "strategy": strategy,
        "final_url": lighthouse.get("finalUrl"),
        "fetch_time": lighthouse.get("fetchTime"),
        "lighthouse_version": lighthouse.get("lighthouseVersion"),
        "categories": selected_categories,
        "audits": selected_audits,
        "lab_only": True,
        "note": "PSI lab samples are not CrUX field data, local browser timings, or a site score.",
    }


def sample(
    urls: list[str],
    *,
    desktop: bool = False,
    api_key: str | None = None,
    cache_dir: str | Path | None = None,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """Collect bounded mobile-first templates; an explicit cache may retain sanitized responses."""
    if not isinstance(urls, list) or not urls or len(urls) > MAX_URLS or not all(
        isinstance(url, str) and url.startswith(("http://", "https://")) for url in urls
    ):
        raise ValueError(f"urls must contain 1..{MAX_URLS} absolute HTTP(S) URLs")
    key = _credential(api_key)
    if key is None:
        return {"ok": False, "state": "not_configured", "verified": False}
    samples = []
    for url in list(dict.fromkeys(urls)):
        for strategy in (["mobile", "desktop"] if desktop else ["mobile"]):
            cache = _cache_path(cache_dir, url, strategy) if cache_dir else None
            if cache and cache.is_file():
                try:
                    samples.append({"cache": "hit", **json.loads(cache.read_text())})
                    continue
                except (OSError, ValueError):
                    pass
            endpoint = f"{HOST}?{urllib.parse.urlencode({'url': url, 'strategy': strategy})}"
            try:
                raw = (fetcher or _default_fetcher)(endpoint, key)
                parsed = _parse(json.loads(raw), url, strategy)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as exc:
                samples.append({"url": url, "strategy": strategy, "state": "failed", "error": str(exc)})
                continue
            if parsed is None:
                samples.append({"url": url, "strategy": strategy, "state": "failed", "error": "malformed PSI v5 response"})
                continue
            if cache:
                _write_cache(cache, parsed)
            samples.append({"cache": "miss", "state": "complete", **parsed})
    return {
        "ok": all(sample.get("state") == "complete" for sample in samples),
        "state": "complete" if all(sample.get("state") == "complete" for sample in samples) else "partial",
        "provider": "pagespeed",
        "samples": samples,
        "mobile_first": True,
        "cache": "explicit local cache" if cache_dir else "disabled",
    }
