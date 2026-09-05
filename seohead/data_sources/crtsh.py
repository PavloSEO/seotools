"""Certificate Transparency (crt.sh): subdomain discovery from public TLS logs.

Every TLS certificate ever issued for a domain is public record, so a certificate naming
`app.example.com` reveals that host even when no page ever links to it. `mirror-check` and
`regions-check` both currently depend on being told where to look (issue #97); this is the
free, keyless source that can find the rest on its own.

crt.sh is a free public service, not an SLA-backed API: it has no key, no documented quota, and
is known to be slow or briefly unavailable. A failure here is reported as ``ok: false`` rather
than raised, so a flaky response does not read as "zero subdomains".
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

HOST = "https://crt.sh/"
TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; SEOHEAD-Tools/3.0; +https://seohead.tech/seotools)"

Fetcher = Callable[[str], str]


def _default_fetcher(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # The request URL is built from the fixed HTTPS crt.sh endpoint plus an encoded query string.
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # nosec B310
        return response.read().decode("utf-8")


def subdomains(domain: str, *, fetcher: Fetcher | None = None) -> dict[str, Any]:
    """Return every distinct hostname under ``domain`` named in a logged certificate.

    ``name_value`` in crt.sh's response can hold several Subject Alternative Names on separate
    lines within one certificate entry, and a wildcard entry (``*.example.com``) is folded back
    to its base domain rather than kept as a literal, unusable ``*.`` hostname.
    """
    if not domain:
        raise ValueError("domain required")
    domain = domain.strip().lower()
    query = urllib.parse.urlencode({"q": f"%.{domain}", "output": "json"})
    fetch = fetcher or _default_fetcher
    try:
        raw = fetch(f"{HOST}?{query}")
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"ok": False, "domain": domain, "error": f"crt.sh request failed: {exc}"}

    if not raw.strip():
        return {"ok": True, "domain": domain, "count": 0, "subdomains": []}
    try:
        rows = json.loads(raw)
    except ValueError:
        # crt.sh serves an HTML error/maintenance page under load instead of its JSON API.
        return {
            "ok": False,
            "domain": domain,
            "error": "crt.sh returned a response that is not JSON (the service may be overloaded)",
        }
    if not isinstance(rows, list):
        return {
            "ok": False,
            "domain": domain,
            "error": "crt.sh returned a JSON body that is not an array",
        }
    if not rows:
        # The documented empty-result shape: a JSON array with nothing in it.
        return {"ok": True, "domain": domain, "count": 0, "subdomains": []}

    names: set[str] = set()
    suffix = "." + domain
    for row in rows:
        if not isinstance(row, dict) or not ("common_name" in row or "name_value" in row):
            return {
                "ok": False,
                "domain": domain,
                "error": "crt.sh response contains a malformed row",
            }
        for field in ("common_name", "name_value"):
            for candidate in str(row.get(field) or "").splitlines():
                name = candidate.strip().lower().removeprefix("*.")
                if name and (name == domain or name.endswith(suffix)):
                    names.add(name)
    return {"ok": True, "domain": domain, "count": len(names), "subdomains": sorted(names)}
