"""Audit mirror consolidation across every common variant of a site's address.

The matrix covers scheme (HTTP/HTTPS), host (bare/www), index files
(``/index.php`` and ``/index.html``), path case, and trailing slashes. Redirect
chains are collected without automatic following so the result exposes HTTPS
to HTTP downgrades, multi-hop chains, and variants that return a duplicate 200
instead of consolidating to the canonical URL.

The ``www`` hostname is checked separately through DNS over HTTPS (DoH). A
missing record and an unavailable web server are different diagnoses. DoH also
avoids misleading answers occasionally returned by local resolvers behind a VPN.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .net import UA, doh, http_client, normalize_url

MAX_HOPS = 6


# --------------------------------------------------------------- variants


def build_variants(url: str) -> list[dict[str, str]]:
    """Build addresses that should all resolve to one canonical URL.

    Pure function: URL -> list of ``{variant, url, group}`` dictionaries.
    """
    parts = urlsplit(normalize_url(url))
    host = parts.netloc.lower()
    bare = host.removeprefix("www.")
    www = "www." + bare
    path = parts.path or "/"

    out: list[dict[str, str]] = []

    def add(variant: str, scheme: str, netloc: str, vpath: str, group: str) -> None:
        out.append(
            {"variant": variant, "group": group, "url": urlunsplit((scheme, netloc, vpath, "", ""))}
        )

    # 1. Origin mirrors: scheme x bare/www host.
    add("https://" + bare + "/", "https", bare, "/", "origin")
    add("http://" + bare + "/", "http", bare, "/", "origin")
    add("https://" + www + "/", "https", www, "/", "origin")
    add("http://" + www + "/", "http", www, "/", "origin")

    # 2. Index files are a common source of CMS homepage duplicates.
    for idx in ("/index.php", "/index.html"):
        add(idx, "https", bare, idx, "index")

    # 3. Trailing-slash and case variants for a supplied non-root path.
    if path != "/":
        slashless = path.rstrip("/")
        if slashless:
            add(slashless, "https", bare, slashless, "path")
            add(slashless + "/", "https", bare, slashless + "/", "path")
            upper = slashless.upper()
            if upper != slashless:
                add(upper, "https", bare, upper, "case")
    return out


# --------------------------------------------------------------- requests


def _fetch_chain(client, url: str) -> dict[str, Any]:
    """Collect a redirect chain one hop at a time without automatic following."""
    hops: list[dict[str, Any]] = []
    current = url
    for _ in range(MAX_HOPS):
        try:
            resp = client.get(current)
        except Exception as exc:  # Network failures are result data.
            return {
                "url": url,
                "reachable": False,
                "error": str(exc)[:200],
                "hops": hops,
                "status": None,
                "final_url": None,
            }
        status = resp.status_code
        location = resp.headers.get("location")
        if status in (301, 302, 303, 307, 308) and location:
            import urllib.parse

            nxt = urllib.parse.urljoin(current, location)
            hops.append({"url": current, "status": status, "location": nxt})
            current = nxt
            continue
        return {
            "url": url,
            "reachable": True,
            "hops": hops,
            "status": status,
            "final_url": current,
            "redirects": len(hops),
        }
    return {
        "url": url,
        "reachable": True,
        "hops": hops,
        "status": None,
        "final_url": current,
        "loop_suspect": True,
        "redirects": len(hops),
    }


# --------------------------------------------------------------- analysis


def _norm_final(u: str | None) -> str | None:
    if not u:
        return None
    p = urlsplit(u)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path.rstrip("/") or "/", "", ""))


def analyze(url: str, results: list[dict[str, Any]], www_dns: dict[str, Any]) -> dict[str, Any]:
    """Analyze collected chains with a pure function that requires no network."""
    parts = urlsplit(normalize_url(url))
    host = parts.netloc.lower()
    bare = host.removeprefix("www.")

    canon_https = _norm_final(f"https://{bare}/")
    findings: list[str] = []

    # Determine the final URL to which each origin mirror converges.
    origin_finals = {
        r["variant"]: _norm_final(r.get("final_url"))
        for r in results
        if r["group"] == "origin" and r.get("reachable")
    }
    finals = {v for v in origin_finals.values() if v}
    consolidated = len(finals) == 1
    # Once origins converge, judge duplicates against the destination they
    # actually converged on, not a hard-coded bare-https guess: a converged
    # www-primary site must not be flagged against an origin nobody uses.
    canonical_destination = next(iter(finals)) if consolidated else canon_https

    duplicates_200: list[str] = []
    downgrade: list[str] = []
    long_chains: list[dict[str, Any]] = []
    dead: list[dict[str, Any]] = []
    path_200: dict[str, bool] = {}

    for r in results:
        variant = r["variant"]
        if not r.get("reachable"):
            dead.append({"variant": variant, "error": r.get("error")})
            continue
        answered_200_here = r.get("status") == 200 and not r.get("hops")
        if (
            r["group"] == "origin"
            and answered_200_here
            and _norm_final(r["url"]) != canonical_destination
        ):
            duplicates_200.append(variant)  # this mirror remains independently accessible
        if r["group"] in ("index", "case") and answered_200_here:
            duplicates_200.append(variant)  # index or case variant returns a duplicate
        if r["group"] == "path":
            path_200[variant] = answered_200_here
        for hop in r.get("hops", []):
            if hop["url"].startswith("https://") and str(hop.get("location", "")).startswith(
                "http://"
            ):
                downgrade.append(f"{variant}: {hop['url']} → {hop['location']}")
        if r.get("redirects", 0) > 1:
            long_chains.append(
                {
                    "variant": variant,
                    "hops": r["redirects"],
                    "chain": [h["url"] for h in r["hops"]] + [r.get("final_url")],
                }
            )
        if r.get("loop_suspect"):
            findings.append(f"{variant}: more than {MAX_HOPS} redirects; possible loop")

    # Both trailing-slash variants return an independent 200 response: duplicate.
    if sum(1 for ok in path_200.values() if ok) > 1:
        pair = " and ".join(sorted(path_200))
        duplicates_200.append(pair)
        findings.append(f"trailing-slash duplicate: {pair} both return 200 without consolidation")

    if not consolidated and len(finals) > 1:
        findings.append(
            "origin mirrors do not consolidate to one address: "
            + "; ".join(f"{k} → {v}" for k, v in origin_finals.items())
        )
    for d in duplicates_200:
        findings.append(
            f"{d}: returns 200 instead of redirecting to the canonical URL; live duplicate"
        )
    for d in downgrade:
        findings.append("redirect downgrades HTTPS to HTTP: " + d)
    for c in long_chains:
        findings.append(f"{c['variant']}: redirect chain contains {c['hops']} hops")
    if www_dns.get("resolvable") is False:
        findings.append(
            "www hostname does not resolve (no A or CNAME record); the mirror is unreachable"
        )
    for d in dead:
        findings.append(f"{d['variant']}: unavailable ({d['error']})")

    return {
        "ok": True,
        "url": normalize_url(url),
        "canonical_origin": (sorted(finals)[0] if consolidated and finals else None),
        "consolidated": consolidated,
        "www_dns": www_dns,
        "variants": results,
        "duplicates_200": duplicates_200,
        "downgrade_redirects": downgrade,
        "long_chains": long_chains,
        "unreachable": dead,
        "findings": findings or ["all checked variants consolidate correctly"],
    }


def check_mirrors(url: str, timeout: float = 12.0) -> dict[str, Any]:
    """Request the mirror matrix, returning network failures as result data."""
    target = normalize_url(url)
    if not target:
        return {"ok": False, "error": f"not a valid URL: {url!r}"}
    parts = urlsplit(target)
    bare = parts.netloc.lower().removeprefix("www.")

    # Resolve www through DoH to avoid misleading answers from VPN-aware local resolvers.
    a = doh("www." + bare, "A")
    cname = doh("www." + bare, "CNAME")
    www_dns = {"resolvable": bool(a or cname), "a": a, "cname": cname, "source": "doh"}

    variants = build_variants(target)
    results: list[dict[str, Any]] = []
    client, _http2_capable = http_client(
        timeout, headers={"User-Agent": UA}, follow_redirects=False, verify=True
    )
    with client:
        for v in variants:
            row = _fetch_chain(client, v["url"])
            row["variant"], row["group"] = v["variant"], v["group"]
            results.append(row)
    return analyze(target, results, www_dns)
