"""Review security headers, version disclosure, and cookies for an SEO audit.

This is not a penetration test. It uses ordinary GET requests to inspect data
the site already exposes publicly: response headers, cookie flags, and the HTTP
to HTTPS redirect. It neither mutates the target nor guesses credentials.

Probing service paths such as ``.git/HEAD`` and ``.env`` is separate and
**disabled by default**. It requires explicit ``probe_paths=True`` because even
read-only requests to another site should be a deliberate action, not a hidden
side effect of an audit.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from seohead.recon.net import http_client, normalize_url

# Header -> (score weight, security purpose).
SECURITY_HEADERS: dict[str, tuple[int, str]] = {
    "strict-transport-security": (20, "forces browsers to use HTTPS"),
    "content-security-policy": (20, "controls allowed script and style sources"),
    "x-content-type-options": (15, "prevents MIME type sniffing"),
    "x-frame-options": (15, "prevents clickjacking, or use frame-ancestors in CSP"),
    "referrer-policy": (15, "limits URL information sent to other sites"),
    "permissions-policy": (15, "controls access to camera, geolocation, and other APIs"),
}

# Version-bearing headers are not vulnerabilities alone, but reduce an attacker's research cost.
DISCLOSURE_HEADERS = (
    "server",
    "x-powered-by",
    "x-aspnet-version",
    "x-aspnetmvc-version",
    "x-generator",
    "x-drupal-dynamic-cache",
    "x-runtime",
)

# Service paths commonly left exposed during deployment.
SENSITIVE_PATHS = (
    "/.git/HEAD",
    "/.env",
    "/.DS_Store",
    "/.svn/entries",
    "/phpinfo.php",
    "/server-status",
    "/backup.sql",
    "/.well-known/security.txt",
)

# Markers that distinguish exposed content from a soft or custom 404 response.
_PATH_MARKERS = {
    "/.git/HEAD": "ref:",
    "/.env": "=",
    # Classic (pre-1.7) SVN entries files open with a lone format-version line,
    # a blank line, then the literal "dir" kind for the root entry.
    "/.svn/entries": "\ndir\n",
    "/phpinfo.php": "phpinfo()",
    "/server-status": "Server Version",
}


def _grade(score: int) -> str:
    for threshold, letter in ((90, "A"), (75, "B"), (60, "C"), (40, "D"), (20, "E")):
        if score >= threshold:
            return letter
    return "F"


def _check_https_redirect(client, domain: str) -> dict[str, Any]:
    """Check whether HTTP upgrades to HTTPS; HSTS cannot protect the first visit otherwise."""
    try:
        resp = client.get(urlunsplit(("http", domain, "/", "", "")))
    except Exception as exc:  # Redirect-check failures are result data.
        return {"checked": False, "reason": str(exc)}
    final = str(resp.url)
    return {
        "checked": True,
        "final_url": final,
        "upgrades": final.startswith("https://"),
        "status_code": resp.status_code,
    }


def _cookie_flags(resp) -> list[dict[str, Any]]:
    """Read Set-Cookie flags from raw headers because httpx combines parsed cookies."""
    out: list[dict[str, Any]] = []
    for raw in resp.headers.get_list("set-cookie"):
        low = raw.lower()
        name = raw.split("=", 1)[0].strip()
        same_site = None
        for part in low.split(";"):
            if part.strip().startswith("samesite="):
                same_site = part.split("=", 1)[1].strip()
        attrs = {part.strip() for part in low.split(";")}
        out.append(
            {
                "name": name,
                "secure": "secure" in attrs,
                "http_only": "httponly" in low,
                "same_site": same_site,
            }
        )
    return out


def _probe(client, base: str) -> list[dict[str, Any]]:
    """Probe service paths using read-only GET requests without changing the target."""
    exposed: list[dict[str, Any]] = []
    for path in SENSITIVE_PATHS:
        try:
            resp = client.get(base.rstrip("/") + path)
        except Exception:  # An unavailable path is an expected result.
            continue
        if resp.status_code != 200:
            continue
        marker = _PATH_MARKERS.get(path)
        body = resp.text[:400]
        # An HTML response with status 200 is usually a custom 404, not an exposed file.
        looks_real = (
            (marker in body)
            if marker
            else "html" not in resp.headers.get("content-type", "").lower()
        )
        if looks_real:
            exposed.append(
                {
                    "path": path,
                    "status_code": resp.status_code,
                    "content_type": resp.headers.get("content-type"),
                    "bytes": len(resp.content),
                }
            )
    return exposed


def check_security(url: str, *, probe_paths: bool = False, timeout: float = 25.0) -> dict[str, Any]:
    """Build a page security profile; probe service paths only when explicitly requested."""
    target = normalize_url(url)
    if not target:
        return {"ok": False, "error": f"not a valid URL: {url!r}"}
    try:
        client, _ = http_client(timeout)
    except ImportError:
        return {"ok": False, "error": "httpx is required"}

    try:
        with client:
            resp = client.get(target)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            cookies = _cookie_flags(resp)
            https = _check_https_redirect(client, urlsplit(str(resp.url)).netloc)
            exposed = _probe(client, str(resp.url)) if probe_paths else None
    except Exception as exc:  # Network failures are returned as result data.
        return {"ok": False, "url": target, "error": str(exc)}

    present: dict[str, str] = {}
    missing: list[dict[str, str]] = []
    score = 0
    for header, (weight, purpose) in SECURITY_HEADERS.items():
        if header in headers:
            present[header] = headers[header][:300]
            score += weight
        elif (
            header == "x-frame-options"
            and "frame-ancestors" in headers.get("content-security-policy", "").lower()
        ):
            present[header] = "covered by CSP frame-ancestors"
            score += weight
        else:
            missing.append({"header": header, "purpose": purpose})

    disclosure = {h: headers[h] for h in DISCLOSURE_HEADERS if h in headers}
    result: dict[str, Any] = {
        "ok": True,
        "url": target,
        "final_url": str(resp.url),
        "status_code": resp.status_code,
        "score": score,
        "grade": _grade(score),
        "headers_present": present,
        "headers_missing": missing,
        "version_disclosure": disclosure,
        "cookies": cookies,
        "https_redirect": https,
        "exposed_paths": exposed,
        "probed": bool(probe_paths),
    }
    result["findings"] = _findings(result)
    return result


def _findings(result: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in result["headers_missing"]:
        out.append(f"missing {item['header']}: {item['purpose']}")

    for header, value in result["version_disclosure"].items():
        if any(ch.isdigit() for ch in value):
            out.append(f"{header} discloses version information: {value}")

    https = result["https_redirect"]
    if https.get("checked") and not https.get("upgrades"):
        out.append("http does not redirect to https; some traffic and signals remain unencrypted")

    insecure = [c["name"] for c in result["cookies"] if not c["secure"]]
    if insecure:
        out.append(f"cookies without the Secure flag: {', '.join(insecure[:5])}")
    no_same_site = [c["name"] for c in result["cookies"] if not c["same_site"]]
    if no_same_site:
        out.append(f"cookies without SameSite: {', '.join(no_same_site[:5])}")

    for item in result["exposed_paths"] or []:
        out.append(
            f"publicly exposed path {item['path']} ({item['bytes']} bytes); restrict it immediately"
        )
    return out
