"""Profile domain infrastructure: registration, DNS, hosting, ASN, country, and TLS.

These details are not available from a page crawl: domain age and expiry,
authoritative DNS and mail providers, the network behind a CDN, and certificate
status. They provide useful SEO risk context, including a recently registered
domain, imminent expiry, unexpected hosting jurisdiction, or an invalid
certificate.

Geolocation is intentionally limited to country level. Reporting a city or
coordinates without a licensed database such as MaxMind would present a guess
as data.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import re
import socket
import ssl
from typing import Any

from seohead.recon.net import (
    doh,
    normalize_domain,
    rdap,
    resolve_socket_addresses,
    whois_lookup,
)

# Domain age is a weak trust signal; a 30-day expiry window warrants renewal attention.
YOUNG_DOMAIN_DAYS = 180
EXPIRY_WARN_DAYS = 30
TLS_EXPIRY_WARN_DAYS = 14

_WHOIS_CREATED = (
    "creation date",
    "created",
    "created on",
    "registered on",
    "domain_dateregistered",
)
_WHOIS_EXPIRES = ("registry expiry date", "expiration date", "paid-till", "expires", "expire")
_WHOIS_REGISTRAR = ("registrar", "sponsoring registrar", "registrar name")


def _parse_date(value: str) -> _dt.date | None:
    """Parse an RDAP or WHOIS date, trying ISO first and known fallback formats next."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y.%m.%d", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(raw[:11].strip(), fmt).date()
        except ValueError:
            continue
    return None


def _days_from(day: _dt.date | None) -> int | None:
    return None if day is None else (_dt.date.today() - day).days


def _from_rdap_domain(data: dict[str, Any]) -> dict[str, Any]:
    events = {
        str(e.get("eventAction", "")).lower(): e.get("eventDate", "")
        for e in data.get("events", [])
        if isinstance(e, dict)
    }
    registrar = None
    for entity in data.get("entities", []) or []:
        roles = [str(r).lower() for r in (entity.get("roles") or [])]
        if "registrar" not in roles:
            continue
        # vCard: ["vcard", [["version", ...], ["fn", {}, "text", "Registrar name"], ...]]
        for field in (entity.get("vcardArray") or [None, []])[1]:
            if isinstance(field, list) and len(field) >= 4 and field[0] == "fn":
                registrar = str(field[3])
                break
        break
    return {
        "registrar": registrar,
        "created": events.get("registration"),
        "expires": events.get("expiration"),
        "updated": events.get("last changed") or events.get("last update of rdap database"),
        "status": [str(s) for s in (data.get("status") or [])],
        "nameservers": sorted(
            {
                str(ns.get("ldhName", "")).lower().rstrip(".")
                for ns in (data.get("nameservers") or [])
                if isinstance(ns, dict) and ns.get("ldhName")
            }
        ),
    }


# Keys a whois record uses to name the object it describes.
_WHOIS_IDENTITY_KEYS = ("domain", "domain name", "domain_name")


def whois_record_is_about(text: str, domain: str) -> bool:
    """True when the record names the domain that was asked about.

    A resolver that answers with the zone record returns a perfectly normal
    looking registration — creation date, expiry, nameservers — for a different
    object. Parsing it produced a confident wrong domain age, which is worse
    than no answer: age decides whether a site's authority is treated as an
    asset worth preserving.
    """
    from seohead.recon.net import _whois_field

    stated = _whois_field(text, _WHOIS_IDENTITY_KEYS)
    if not stated:
        return False
    stated = stated.strip().rstrip(".").lower()
    return stated == domain.strip().rstrip(".").lower()


def _from_whois_text(text: str) -> dict[str, Any]:
    """Parse essential fields for ccTLDs without RDAP, such as .by and some .ru domains."""
    # Keep the first value for scalar fields and every repeated nameserver value.
    fields: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    for line in text.splitlines():
        if ":" not in line or line.lstrip().startswith("%"):
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if not value:
            continue
        fields.setdefault(key, value)
        multi.setdefault(key, []).append(value)

    def pick(names: tuple[str, ...]) -> str | None:
        return next((fields[n] for n in names if n in fields), None)

    nameservers = sorted(
        {
            v.split()[0].lower().rstrip(".")
            for k in ("nserver", "name server")
            for v in multi.get(k, [])
        }
    )
    return {
        "registrar": pick(_WHOIS_REGISTRAR),
        "created": pick(_WHOIS_CREATED),
        "expires": pick(_WHOIS_EXPIRES),
        "updated": None,
        "status": [],
        "nameservers": nameservers,
    }


def _is_ipv6(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 6
    except ValueError:
        return False


def _cymru_query_name(ip: str) -> str | None:
    """DNS name that asks Team Cymru's keyless service for this address's origin ASN."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        return f"{'.'.join(reversed(ip.split('.')))}.origin.asn.cymru.com"
    nibbles = address.exploded.replace(":", "")
    return f"{'.'.join(reversed(nibbles))}.origin6.asn.cymru.com"


def _reverse_dns_name(ip: str) -> str | None:
    """The PTR query name for an IPv4 or IPv6 address."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv4Address):
        return ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
    nibbles = address.exploded.replace(":", "")
    return ".".join(reversed(nibbles)) + ".ip6.arpa"


def _asn_via_cymru(ip: str) -> dict[str, Any]:
    """Resolve an IPv4 or IPv6 address to an ASN through Team Cymru's keyless DNS service.

    The response has the form ``15169 | 8.8.8.0/24 | US | arin | 1992-12-01``.
    """
    query = _cymru_query_name(ip)
    if not query:
        return {}
    answer = doh(query, "TXT")
    if not answer:
        return {}
    parts = [p.strip() for p in answer[0].split("|")]
    if len(parts) < 3:
        return {}
    asn = parts[0].split()[0] if parts[0] else None
    out: dict[str, Any] = {
        "asn": f"AS{asn}" if asn else None,
        "prefix": parts[1] or None,
        "country": parts[2] or None,
    }
    name = doh(f"AS{asn}.asn.cymru.com", "TXT") if asn else []
    if name:
        tail = [p.strip() for p in name[0].split("|")]
        out["as_name"] = tail[-1] or None
    return out


def _ip_owner(ip: str) -> dict[str, Any]:
    """Return the RDAP network owner, which may reveal the provider behind a CDN."""
    res = rdap(f"ip/{ip}")
    if not res.get("supported"):
        return {}
    data = res.get("data") or {}
    return {
        "network": data.get("name"),
        "handle": data.get("handle"),
        "country": data.get("country"),
        "type": data.get("type"),
    }


def _tls(domain: str, timeout: float = 8.0) -> dict[str, Any]:
    """Inspect certificate issuer, covered names, negotiated protocol, and expiry."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        addresses = resolve_socket_addresses(domain, 443)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    cert = None
    protocol = None
    last_error: OSError | ssl.SSLError | None = None
    for family, socktype, proto, sockaddr in addresses:
        try:
            with socket.socket(family, socktype, proto) as raw:
                raw.settimeout(timeout)
                raw.connect(sockaddr)
                with context.wrap_socket(raw, server_hostname=domain) as tls:
                    cert = tls.getpeercert()
                    protocol = tls.version()
            break
        except ssl.SSLCertVerificationError as exc:
            detail = getattr(exc, "verify_message", None) or str(exc)
            return {
                "ok": False,
                "valid": False,
                "error": f"certificate verification failed: {detail}",
            }
        except (OSError, ssl.SSLError) as exc:
            last_error = exc
    else:
        return {"ok": False, "error": str(last_error or "TLS connection failed")}

    if not cert:
        return {"ok": False, "error": "the server did not provide a certificate"}

    issuer = {k: v for part in cert.get("issuer", ()) for k, v in part}
    not_after = _parse_cert_date(cert.get("notAfter", ""))
    days_left = None if not_after is None else (not_after - _dt.date.today()).days
    return {
        "ok": True,
        "valid": True,
        "protocol": protocol,
        "issuer": issuer.get("organizationName") or issuer.get("commonName"),
        "expires": None if not_after is None else not_after.isoformat(),
        "days_left": days_left,
        "san": sorted({v for typ, v in cert.get("subjectAltName", ()) if typ == "DNS"})[:20],
    }


def _parse_cert_date(value: str) -> _dt.date | None:
    """Parse the fixed certificate date format, for example ``Jun  1 12:00:00 2027 GMT``."""
    try:
        return _dt.datetime.strptime(value.strip(), "%b %d %H:%M:%S %Y %Z").date()
    except (ValueError, TypeError):
        return None


def _dns_provider(nameservers: list[str]) -> str | None:
    known = {
        "cloudflare": "Cloudflare",
        "awsdns": "AWS Route 53",
        "azure-dns": "Azure DNS",
        "googledomains": "Google",
        "google": "Google Cloud DNS",
        "nsone": "NS1",
        "dnsimple": "DNSimple",
        "yandex": "Yandex",
        "reg.ru": "REG.RU",
        "beget": "Beget",
        "hetzner": "Hetzner",
        "digitalocean": "DigitalOcean",
        "vercel-dns": "Vercel",
        "registrar-servers": "Namecheap",
    }
    joined = " ".join(nameservers).lower()
    return next((label for marker, label in known.items() if marker in joined), None)


def _mail_provider(mx: list[str]) -> str | None:
    known = {
        "google": "Google Workspace",
        "outlook": "Microsoft 365",
        "yandex": "Yandex 360",
        "mail.ru": "VK/Mail.ru",
        "zoho": "Zoho",
        "protonmail": "Proton",
        "mailgun": "Mailgun",
        "yamdex": "Yandex",
    }
    joined = " ".join(mx).lower()
    return next((label for marker, label in known.items() if marker in joined), None)


def profile_domain(domain: str, *, with_tls: bool = True) -> dict[str, Any]:
    """Build one domain profile whose data sources degrade independently."""
    name = normalize_domain(domain)
    if not name:
        return {"ok": False, "error": f"not a valid domain: {domain!r}"}

    res = rdap(f"domain/{name}")
    if res.get("supported"):
        registration = _from_rdap_domain(res.get("data") or {})
        registration["source"] = "rdap"
    else:
        empty = {
            "registrar": None,
            "created": None,
            "expires": None,
            "updated": None,
            "status": [],
            "nameservers": [],
        }
        text, whois_server = whois_lookup(name)
        if text and whois_record_is_about(text, name):
            registration = _from_whois_text(text)
            registration["source"] = "whois"
            if whois_server:
                registration["whois_server"] = whois_server
        else:
            # Either nothing answered, or what answered was about another object
            # (typically the zone). Reporting "none" is the same discipline the
            # toolkit already applies when RDAP and whois both fail.
            registration = dict(empty)
            registration["source"] = "none"
            if text:
                registration["whois_note"] = (
                    "whois answered with a record for another object "
                    "(usually the zone); no registration data was taken from it"
                )
        registration["rdap_note"] = res.get("error")

    created = _parse_date(registration.get("created") or "")
    expires = _parse_date(registration.get("expires") or "")
    age_days = _days_from(created)
    registration["age_days"] = age_days
    registration["age_years"] = None if age_days is None else round(age_days / 365.25, 1)
    registration["expires_in_days"] = None if expires is None else (expires - _dt.date.today()).days

    a_records = doh(name, "A")
    dns = {
        "a": a_records,
        "aaaa": doh(name, "AAAA"),
        "ns": sorted(registration.get("nameservers") or []) or sorted(doh(name, "NS")),
        "mx": sorted(doh(name, "MX")),
        "txt": doh(name, "TXT")[:20],
        "cname": doh(f"www.{name}", "CNAME"),
    }
    dns["dns_provider"] = _dns_provider(dns["ns"])
    dns["mail_provider"] = _mail_provider(dns["mx"])
    dns["spf"] = next((t for t in dns["txt"] if t.lower().startswith("v=spf1")), None)
    dns["dmarc"] = next(iter(doh(f"_dmarc.{name}", "TXT")), None)

    hosting: dict[str, Any] = {}
    ip = next((r for r in a_records if re.match(r"^\d+\.\d+\.\d+\.\d+$", r)), None)
    ip_source = "a" if ip else None
    if not ip:
        # No A record: fall back to an observed AAAA address rather than treating
        # IPv4 as a hidden prerequisite for hosting/ASN/owner enrichment.
        ip = next((r for r in dns["aaaa"] if _is_ipv6(r)), None)
        ip_source = "aaaa" if ip else None
    if ip:
        hosting = {
            "ip": ip,
            "ip_source": ip_source,
            **_asn_via_cymru(ip),
            **{k: v for k, v in _ip_owner(ip).items() if v},
        }
        ptr_name = _reverse_dns_name(ip)
        ptr = doh(ptr_name, "PTR") if ptr_name else []
        hosting["reverse_dns"] = ptr[0] if ptr else None
    elif dns["a"] or dns["aaaa"]:
        # An address was observed but did not parse as a usable IPv4/IPv6 literal;
        # say so instead of returning an empty hosting object with no evidence.
        hosting = {
            "unavailable_reason": "observed A/AAAA records did not parse as a usable IP address",
        }

    profile: dict[str, Any] = {
        "ok": True,
        "domain": name,
        "registration": registration,
        "dns": dns,
        "hosting": hosting,
    }
    if with_tls:
        profile["tls"] = _tls(name)

    profile["flags"] = _flags(profile)
    return profile


def _flags(profile: dict[str, Any]) -> list[str]:
    """Summarize actionable risks for inclusion in a client report."""
    out: list[str] = []
    reg, tls = profile["registration"], profile.get("tls") or {}
    age = reg.get("age_days")
    if age is not None and age < YOUNG_DOMAIN_DAYS:
        out.append(f"young domain: {age} days old, with little history for search engines")
    left = reg.get("expires_in_days")
    if left is not None and left < 0:
        out.append("domain registration has expired")
    elif left is not None and left < EXPIRY_WARN_DAYS:
        out.append(f"domain registration expires in {left} days")
    if any("hold" in str(s).lower() for s in reg.get("status") or []):
        out.append(f"domain has a hold status: {', '.join(reg['status'])}")
    if reg.get("source") == "none":
        if reg.get("whois_note"):
            out.append(f"registration data is unavailable: {reg['whois_note']}")
        else:
            out.append(
                "registration data is unavailable: the registry has no RDAP service and WHOIS "
                "is not installed"
            )
    if not profile["dns"]["a"] and not profile["dns"]["aaaa"]:
        out.append("domain does not resolve to an IP address")
    if not profile["dns"]["spf"]:
        out.append("SPF record is missing")
    if not profile["dns"]["dmarc"]:
        out.append("DMARC record is missing")
    if tls.get("valid") is False:
        out.append(f"TLS: {tls.get('error')}")
    days_left = tls.get("days_left")
    if days_left is not None and days_left < TLS_EXPIRY_WARN_DAYS:
        out.append(f"certificate expires in {days_left} days")
    return out
