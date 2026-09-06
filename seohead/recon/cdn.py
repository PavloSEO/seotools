"""Detect the CDN in front of a site and observe actual cache behavior.

Headers can suggest that caching is configured even when it is ineffective in
practice. This module therefore performs three consecutive requests instead of
judging ``Cache-Control`` in isolation:

1. a regular GET for TTFB, protocol version, and cache headers;
2. a repeated GET through the same client to detect CDN warm-up (MISS -> HIT);
3. a conditional request with ``If-None-Match`` or ``If-Modified-Since`` to see
   whether the server returns 304 instead of transferring the body again.

A separate Brotli probe explicitly advertises ``br`` and streams no response
body. This avoids depending on whether the optional codec is installed for
httpx's default ``Accept-Encoding`` behavior.
"""

from __future__ import annotations

import time
from typing import Any

from seohead.recon.net import http_client, normalize_url

# Signature marker and where to look for it. Order matters: the first match wins.
CDN_SIGNATURES: tuple[tuple[str, str, str], ...] = (
    ("Cloudflare", "header", "cf-ray"),
    ("Cloudflare", "server", "cloudflare"),
    ("Fastly", "header", "x-fastly-request-id"),
    ("Fastly", "value", "fastly"),
    ("Akamai", "header", "x-akamai-transformed"),
    ("Akamai", "server", "akamaighost"),
    ("Amazon CloudFront", "header", "x-amz-cf-id"),
    ("Vercel", "header", "x-vercel-id"),
    ("Netlify", "header", "x-nf-request-id"),
    ("Imperva Incapsula", "header", "x-iinfo"),
    ("Sucuri", "header", "x-sucuri-id"),
    ("BunnyCDN", "server", "bunnycdn"),
    ("KeyCDN", "server", "keycdn-engine"),
    ("Gcore", "server", "gcore"),
    ("Qrator", "server", "qrator"),
    ("DDoS-Guard", "server", "ddos-guard"),
    ("Azure CDN", "header", "x-azure-ref"),
    ("Alibaba Cloud CDN", "header", "x-swift-savetime"),
    ("Yandex Cloud CDN", "header", "x-yandex-req-id"),
    ("Google Cloud CDN", "value", "1.1 google"),
    ("StackPath", "header", "x-hw"),
)

# Headers used by different CDNs to report cache status.
CACHE_STATUS_HEADERS = (
    "cf-cache-status",
    "x-cache",
    "x-cache-status",
    "x-proxy-cache",
    "x-vercel-cache",
    "cdn-cache",
    "x-served-by",
    "x-drupal-cache",
)
_HIT_WORDS = ("hit", "stale", "revalidated")
_MISS_WORDS = ("miss", "expired", "bypass", "dynamic", "none")


def _detect_cdn(headers: dict[str, str]) -> str | None:
    server = headers.get("server", "").lower()
    via = " ".join(headers.get(h, "") for h in ("via", "x-cache", "x-served-by")).lower()
    for name, kind, marker in CDN_SIGNATURES:
        if kind == "header" and marker in headers:
            return name
        if kind == "server" and marker in server:
            return name
        if kind == "value" and marker in via:
            return name
    return None


def _cache_status(headers: dict[str, str]) -> str | None:
    for header in CACHE_STATUS_HEADERS:
        value = headers.get(header)
        if value:
            return f"{header}: {value}"
    return None


def _classify(status: str | None) -> str | None:
    if not status:
        return None
    low = status.lower()
    if any(w in low for w in _HIT_WORDS):
        return "hit"
    if any(w in low for w in _MISS_WORDS):
        return "miss"
    return "unknown"


def _parse_cache_control(value: str) -> dict[str, Any]:
    out: dict[str, Any] = {"raw": value or None, "directives": {}}
    for part in (value or "").split(","):
        token = part.strip().lower()
        if not token:
            continue
        key, _, val = token.partition("=")
        out["directives"][key.strip()] = val.strip() or True
    directives = out["directives"]
    max_age = directives.get("max-age")
    try:
        out["max_age"] = int(max_age) if isinstance(max_age, str) else None
    except ValueError:
        out["max_age"] = None
    out["public"] = "public" in directives
    out["no_store"] = "no-store" in directives
    out["no_cache"] = "no-cache" in directives
    out["immutable"] = "immutable" in directives
    return out


def _brotli_probe(client, url: str) -> str | None:
    """Return the encoding selected when Brotli is requested, without reading the body."""
    try:
        request = client.build_request("GET", url, headers={"Accept-Encoding": "br, gzip, deflate"})
        resp = client.send(request, stream=True)
        encoding = resp.headers.get("content-encoding")
        resp.close()
        return encoding
    except Exception:  # This optional probe must fail silently.
        return None


def check_cdn(url: str, timeout: float = 25.0) -> dict[str, Any]:
    """Inspect CDN, cache, and transport behavior for one URL."""
    target = normalize_url(url)
    if not target:
        return {"ok": False, "error": f"not a valid URL: {url!r}"}
    try:
        client, http2_capable = http_client(timeout)
    except ImportError:
        return {"ok": False, "error": "httpx is required"}

    try:
        with client:
            start = time.perf_counter()
            first = client.get(target)
            ttfb_first = round((time.perf_counter() - start) * 1000, 1)

            start = time.perf_counter()
            second = client.get(target)
            ttfb_second = round((time.perf_counter() - start) * 1000, 1)

            head1 = {k.lower(): v for k, v in first.headers.items()}
            head2 = {k.lower(): v for k, v in second.headers.items()}

            revalidation = _revalidate(client, target, head1)
            brotli = _brotli_probe(client, target)
    except Exception as exc:  # Network failures are returned as result data.
        return {"ok": False, "url": target, "error": str(exc)}

    cache_control = _parse_cache_control(head1.get("cache-control", ""))
    first_status, second_status = _cache_status(head1), _cache_status(head2)
    result: dict[str, Any] = {
        "ok": True,
        "url": target,
        "final_url": str(first.url),
        "status_code": first.status_code,
        "cdn": _detect_cdn(head1),
        "server": head1.get("server"),
        "transport": {
            "http_version": getattr(first, "http_version", "?"),
            # Without the h2 extra, the client cannot negotiate and measure HTTP/2.
            "http_version_measurable": http2_capable,
            "http3_advertised": "h3" in head1.get("alt-svc", "").lower(),
            "alt_svc": head1.get("alt-svc"),
            "content_encoding": head1.get("content-encoding"),
            "brotli_supported": None if brotli is None else brotli == "br",
            "brotli_probe": brotli,
            "ttfb_first_ms": ttfb_first,
            "ttfb_second_ms": ttfb_second,
        },
        "cache": {
            "cache_control": cache_control,
            "etag": head1.get("etag"),
            "last_modified": head1.get("last-modified"),
            "expires": head1.get("expires"),
            "vary": head1.get("vary"),
            "age": head1.get("age"),
            "status_first": first_status,
            "status_second": second_status,
            "hit_first": _classify(first_status),
            "hit_second": _classify(second_status),
            "warmed_up": _classify(first_status) == "miss" and _classify(second_status) == "hit",
            "revalidation": revalidation,
        },
    }
    result["findings"] = _findings(result)
    return result


def _revalidate(client, url: str, headers: dict[str, str]) -> dict[str, Any]:
    """Check whether conditional requests return 304 and save bandwidth and crawl budget."""
    conditional: dict[str, str] = {}
    if headers.get("etag"):
        conditional["If-None-Match"] = headers["etag"]
    if headers.get("last-modified"):
        conditional["If-Modified-Since"] = headers["last-modified"]
    if not conditional:
        return {"supported": False, "reason": "neither ETag nor Last-Modified is available"}
    try:
        resp = client.get(url, headers=conditional)
    except Exception as exc:  # Revalidation failure is an observable result.
        return {"supported": False, "reason": str(exc)}
    return {
        "supported": resp.status_code == 304,
        "status_code": resp.status_code,
        "sent": sorted(conditional),
    }


def _findings(result: dict[str, Any]) -> list[str]:
    out: list[str] = []
    cache, transport = result["cache"], result["transport"]
    cc = cache["cache_control"]

    if not cc["raw"]:
        out.append("Cache-Control is missing; browsers and intermediaries must choose a policy")
    elif cc["no_store"]:
        out.append("Cache-Control: no-store prevents the response from being cached")
    elif cc["max_age"] == 0 and not cc["immutable"]:
        out.append("max-age=0 requires every visit to contact the server")

    if not cache["etag"] and not cache["last_modified"]:
        out.append("neither ETag nor Last-Modified is present for efficient revalidation")
    elif cache["revalidation"].get("supported") is False and cache["revalidation"].get(
        "status_code"
    ):
        out.append(
            f"conditional request returned {cache['revalidation']['status_code']} instead of "
            "304; revalidation transfers the response body again"
        )

    if result["cdn"] and cache["hit_first"] is None:
        out.append(
            f"{result['cdn']} is in front of the site, but exposes no cache status; "
            "external cache hits cannot be verified"
        )
    if cache["hit_second"] == "miss":
        out.append("the repeated request was another MISS; the CDN did not retain the page")

    if not transport["http_version_measurable"]:
        out.append(
            "protocol version is not measurable without the h2 package; "
            "install seohead-seotools[all]"
        )
    elif str(transport["http_version"]).startswith("HTTP/1"):
        out.append(f"served over {transport['http_version']}; HTTP/2 would provide multiplexing")
    if not transport["http3_advertised"]:
        out.append("HTTP/3 is not advertised through Alt-Svc")
    if not transport["content_encoding"]:
        out.append("the response is uncompressed: neither gzip nor Brotli is enabled")
    elif transport["brotli_supported"] is None:
        out.append("Brotli support could not be probed")
    elif not transport["brotli_supported"]:
        out.append(
            f"using {transport['content_encoding']} compression; Brotli is not enabled "
            "and is often 15-20% smaller"
        )

    if transport["ttfb_first_ms"] > 800:
        out.append(f"TTFB is {transport['ttfb_first_ms']} ms, indicating a slow server response")
    return out
