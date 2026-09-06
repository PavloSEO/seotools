"""Shared network layer: URL guardrails, DNS-over-HTTPS, and RDAP.

Every user-controlled HTTP request should use :func:`http_client`. Its transport
(see ``_PinningTransport`` below) resolves and validates every hop — the first
request and every redirect — and then connects to the literal address it just
validated, never to a second, independent resolution of the hostname. Before
this, only ``request``/``response`` event hooks ran the *check*; the *socket*
was opened by httpx's own DNS lookup, a separate call a hostile resolver could
answer differently (issue #142). Private, loopback, link-local, multicast,
reserved, and otherwise non-global addresses are blocked by default. Authorized
staging and intranet work requires an explicit ``SEOHEAD_ALLOW_PRIVATE_NETWORKS=1``
opt-in — that opens every private range, for a run that genuinely needs it.
``SEOHEAD_ALLOW_PRIVATE_HOSTS`` is the scoped alternative: a comma-separated list
of exact hostnames (e.g. one staging box) allowed to resolve privately without
opening the rest of RFC 1918 space.

DNS and registration checks use HTTP APIs because ``dig`` and ``whois`` are not
available in every container. A system ``whois`` binary remains an optional ccTLD
fallback. Network failures are returned as unavailable data rather than escaping
as fatal exceptions.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import subprocess
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

UA = "Mozilla/5.0 (compatible; SEOHEAD-Tools/3.0; +https://seohead.tech/seotools)"
PRIVATE_NETWORK_ENV = "SEOHEAD_ALLOW_PRIVATE_NETWORKS"
PRIVATE_HOST_ALLOWLIST_ENV = "SEOHEAD_ALLOW_PRIVATE_HOSTS"

DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.google/resolve",
)
RDAP_BOOTSTRAP = "https://rdap.org"

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*"
    r"\.(?!-)[a-z0-9-]*[a-z][a-z0-9-]*(?<!-)$"
)


class NetworkUnavailable(RuntimeError):
    """Raised internally when the base HTTP client is unavailable."""


def private_networks_enabled() -> bool:
    """Return whether private-network access was explicitly enabled."""
    return os.getenv(PRIVATE_NETWORK_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def allowed_private_hosts() -> frozenset[str]:
    """Hostnames explicitly permitted to resolve to a non-public address.

    A scoped alternative to :data:`PRIVATE_NETWORK_ENV`: it authorizes one
    named staging host without opening every private range. Matching is exact
    on the lowercased, dot-stripped hostname — an entry does not extend to a
    subdomain, to a different host a redirect points at, or to any other
    address the same staging host might expose under a different name.
    """
    raw = os.getenv(PRIVATE_HOST_ALLOWLIST_ENV, "")
    return frozenset(host.strip().rstrip(".").lower() for host in raw.split(",") if host.strip())


def _private_target_allowed(host: str) -> bool:
    """Whether ``host`` may resolve to a private or otherwise non-public address."""
    return private_networks_enabled() or (host or "").rstrip(".").lower() in allowed_private_hosts()


# Ranges that carry a non-public address inside a globally-scoped one. Python's
# ``is_global`` answers a question about the address family, not about where the
# packet ends up: 64:ff9b::7f00:1 is 127.0.0.1 wrapped in the well-known NAT64
# prefix and reports is_global=True, so on any NAT64 host — common in CI and in
# mobile and cloud networks — the guard would pass a request to loopback.
_TRANSLATED_PREFIXES = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "64:ff9b::/96",  # RFC 6052 well-known NAT64 prefix
        "64:ff9b:1::/48",  # RFC 8215 local-use NAT64
        "2002::/16",  # 6to4, embeds an IPv4 address
        "::ffff:0:0/96",  # IPv4-mapped
        "::/96",  # IPv4-compatible, deprecated but still parsed
    )
)


def _embedded_ipv4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address a translated IPv6 address actually reaches."""
    packed = address.packed
    if address in _TRANSLATED_PREFIXES[2]:  # 6to4 carries it in bytes 2..6
        return ipaddress.IPv4Address(packed[2:6])
    return ipaddress.IPv4Address(packed[-4:])


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    # Translated forms are checked first: the wrapper's own scope says nothing
    # about the destination, and Python scores some of them non-global and
    # others global regardless of what they carry.
    if isinstance(address, ipaddress.IPv6Address) and any(
        address in prefix for prefix in _TRANSLATED_PREFIXES
    ):
        try:
            return _embedded_ipv4(address).is_global
        except (ipaddress.AddressValueError, ValueError):
            return False
    return address.is_global


def pinned_target(url: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """Rewrite a URL to connect to a vetted address, keeping the hostname.

    ``validate_url`` resolved DNS and then threw the answer away, so the HTTP
    client resolved a second time and connected to whatever came back. That is a
    time-of-check-to-time-of-use gap: a hostile resolver can answer the check
    with a public address and the connection with a loopback one. Since the guard
    also runs per redirect hop, it was one window per hop rather than one.

    Returns the URL to request, headers carrying the original ``Host``, and the
    request extensions carrying the hostname for SNI — so certificate
    verification still happens against the name, not the address.

    This is the primitive ``_PinningTransport`` below builds on for every other
    ``http_client()`` caller. It stays a free function too because
    ``collect.py``'s list-mode fetch already called it directly before the
    transport existed, on every retry attempt; the transport recognizes a
    request pinned this way (by its ``sni_hostname`` extension) and passes it
    through rather than pinning it a second time, which would re-resolve the
    literal address as if it were a hostname and lose the real one.
    """
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise ValueError(f"no host to pin in {url!r}")
    port = parts.port or (443 if parts.scheme == "https" else 80)

    address = resolve_socket_addresses(host, port)[0][3][0].split("%", 1)[0]
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{parts.port}" if parts.port else literal
    pinned = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))

    authority = f"{host}:{parts.port}" if parts.port else host
    return pinned, {"Host": authority}, {"sni_hostname": host}


def resolve_socket_addresses(host: str, port: int) -> list[tuple[int, int, int, Any]]:
    """Resolve once and return vetted socket addresses for a direct connection."""
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"hostname could not be resolved safely: {host}") from exc
    if not records:
        raise ValueError(f"hostname could not be resolved safely: {host}")
    if not _private_target_allowed(host) and any(
        not _is_public_address(record[4][0]) for record in records
    ):
        raise ValueError(
            f"private or non-public network target blocked; set {PRIVATE_NETWORK_ENV}=1 "
            f"to authorize every private target, or add {host!r} to "
            f"{PRIVATE_HOST_ALLOWLIST_ENV} to authorize only this one"
        )

    unique: list[tuple[int, int, int, Any]] = []
    seen: set[tuple[int, int, int, Any]] = set()
    for family, socktype, proto, _canonname, sockaddr in records:
        item = (family, socktype, proto, sockaddr)
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def validate_url(url: str) -> str:
    """Validate an HTTP(S) URL and block private networks by default.

    Embedded credentials are rejected because they are easily copied into logs and
    transcripts. Hostnames are resolved before a request and every resolved address
    must be globally routable. This is a guardrail, not a network sandbox; callers
    handling hostile DNS should additionally isolate the process or container.
    """
    value = str(url or "").strip()
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError(f"invalid URL: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("only http:// and https:// URLs are supported")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("embedded URL credentials are not supported")

    if private_networks_enabled():
        return value

    host = parsed.hostname.rstrip(".").lower()
    if (host == "localhost" or host.endswith(".localhost")) and host not in allowed_private_hosts():
        raise ValueError(
            f"private-network target blocked; set {PRIVATE_NETWORK_ENV}=1 to authorize "
            f"every private target, or add {host!r} to {PRIVATE_HOST_ALLOWLIST_ENV} to "
            "authorize only this one"
        )

    resolve_socket_addresses(
        host,
        parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
    )
    return value


class BlockedRedirectError(ValueError):
    """A redirect's ``Location`` pointed at a target :func:`validate_url` refuses.

    Raised instead of a bare ``ValueError`` so a caller built around a broad
    ``except Exception`` — ``fetch_one`` (issue #175) and ``check_soft404`` alike
    — can tell "we refused to follow this redirect" apart from a transport
    failure and record it as the classified outcome it is: the response the
    server sent us was real, only the next hop is refused. ``status_code`` and
    ``location`` carry that response's status and the absolute target that was
    refused, since raising from inside the event hook is the only place either
    is still available — the caller never receives the response object itself.
    """

    def __init__(self, message: str, *, status_code: int, location: str):
        super().__init__(message)
        self.status_code = status_code
        self.location = location


def _guard_request(request: Any) -> None:
    validate_url(str(request.url))


def _guard_redirect(response: Any) -> None:
    if not getattr(response, "is_redirect", False):
        return
    location = response.headers.get("location")
    if not location:
        return
    target = urljoin(str(response.request.url), location)
    try:
        validate_url(target)
    except ValueError as exc:
        raise BlockedRedirectError(
            str(exc), status_code=response.status_code, location=target
        ) from exc


def network_event_hooks() -> dict[str, list[Any]]:
    """Return httpx hooks that validate every request and redirect.

    This is a second, independent check ahead of the transport's own pinning
    below — it runs first and rejects a bad scheme or embedded credentials
    before a connection is even attempted. It does not do the pinning itself:
    its resolution is discarded, exactly like ``validate_url``'s always was.
    The property that closes issue #142 is enforced downstream, in
    ``_PinningTransport.handle_request``, which connects to the address it
    just resolved rather than to a second, later resolution of the hostname.
    """
    return {"request": [_guard_request], "response": [_guard_redirect]}


# Transport-construction kwargs a caller may forward through http_client() —
# distinct from httpx.Client's own kwargs (headers, follow_redirects, ...),
# which pass through untouched. Client() would silently ignore verify/cert/
# trust_env/http1/limits/proxy once a custom transport is supplied instead of
# building its own default one, so these have to be lifted out and given to
# the pinning transport directly; uds/local_address/retries/socket_options
# aren't Client() parameters at all and must be lifted out either way.
_TRANSPORT_KWARGS = (
    "verify",
    "cert",
    "trust_env",
    "http1",
    "limits",
    "proxy",
    "uds",
    "local_address",
    "retries",
    "socket_options",
)
_TRANSPORT_ONLY_KWARGS = ("uds", "local_address", "retries", "socket_options")

_pinning_transport_cls: type | None = None


def _get_pinning_transport_cls() -> type:
    """Build (once) the ``httpx.HTTPTransport`` subclass that pins every hop.

    Defined lazily, like the ``import httpx`` in ``http_client()`` below, so
    this module stays importable in the rare environment without httpx.
    """
    global _pinning_transport_cls
    if _pinning_transport_cls is not None:
        return _pinning_transport_cls

    import httpx

    class _PinningTransport(httpx.HTTPTransport):
        """Resolve, validate, and connect to the same address — every hop.

        ``http_client()`` used to validate a URL via request hooks and let
        httpx resolve DNS again to open the socket: two independent
        ``getaddrinfo()`` calls, free to disagree (issue #142). This is
        httpx's own connection path — ``handle_request`` runs once per hop,
        including every redirect the client follows internally — so pinning
        it here, rather than at each of fifteen call sites, is structural:
        a caller of ``http_client()`` gets the guard without asking for it.

        A request that already carries an ``sni_hostname`` extension was
        pinned by its caller before reaching the transport (``collect.py``'s
        list-mode fetch does this via ``pinned_target`` directly — see its
        docstring). Re-pinning it here would resolve the already-literal
        address as if it were a hostname, and the ``sni_hostname`` this
        transport would then set from *that* would replace the real one,
        breaking SNI and certificate verification. So an already-pinned
        request passes straight through to the real connection instead.
        """

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            if "sni_hostname" not in request.extensions:
                pinned_url, _headers, pin_extensions = pinned_target(str(request.url))
                # Headers (including Host) are left untouched: httpx already
                # set them from the real hostname when it built this request,
                # and that is exactly what a pinned connection must keep.
                request = httpx.Request(
                    method=request.method,
                    url=pinned_url,
                    headers=request.headers,
                    stream=request.stream,
                    extensions={**request.extensions, **pin_extensions},
                )
            return super().handle_request(request)

    _pinning_transport_cls = _PinningTransport
    return _pinning_transport_cls


def http_client(timeout: float, **kwargs: Any):
    """Return ``(client, http2_capable)`` with shared URL guardrails.

    The boolean must reach reports: without the optional HTTP/2 codec, reporting
    HTTP/1.1 as a server limitation would describe the client rather than the site.

    The client cannot be built without the pinning transport below — there is
    no kwarg that switches it off — because the fix for issue #142 is "this is
    the only client this function can hand back", not "remember to opt in".
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - a base dependency
        raise NetworkUnavailable("httpx is required") from exc

    reserved = kwargs.keys() & {"transport", "http2"}
    if reserved:
        raise TypeError(
            f"http_client() does not accept {', '.join(sorted(reserved))!s}: "
            "the pinning transport and its HTTP/2 negotiation are not "
            "overridable by callers"
        )

    supplied_hooks = kwargs.pop("event_hooks", None) or {}
    hooks = network_event_hooks()
    for phase, values in supplied_hooks.items():
        hooks.setdefault(phase, []).extend(values)

    transport_kwargs = {k: kwargs[k] for k in _TRANSPORT_KWARGS if k in kwargs}
    client_kwargs = {k: v for k, v in kwargs.items() if k not in _TRANSPORT_ONLY_KWARGS}
    PinningTransport = _get_pinning_transport_cls()

    options = {
        "timeout": timeout,
        "headers": {"User-Agent": UA},
        "follow_redirects": True,
        "event_hooks": hooks,
        **client_kwargs,
    }
    try:
        transport = PinningTransport(http2=True, **transport_kwargs)
        return httpx.Client(http2=True, transport=transport, **options), True
    except ImportError:
        transport = PinningTransport(http2=False, **transport_kwargs)
        return httpx.Client(transport=transport, **options), False


def _client(timeout: float):
    return http_client(timeout)[0]


# Compound public suffixes common enough that treating the last two labels as
# the registrable domain would merge unrelated sites. A full public suffix list
# is large and changes; this only has to tell a subdomain from a separate site.
_COMPOUND_SUFFIXES = frozenset({"com", "net", "org", "co", "gov", "edu", "ac", "spb", "msk"})

# Multi-tenant hosting suffixes where the label directly under the suffix is a
# customer, not a subdomain of one site -- the shape a full public suffix list
# would get right and this project deliberately doesn't ship (issue #144). Not
# a dependency: no PSL package was already in use, and this project's own rule
# on adding one for a single lookup is to prefer the standard library or a
# hand-kept table first. Kept short and reviewed whenever a client's audited
# site turns out to live on a shared suffix not yet listed here -- the ones
# below are the platforms actually seen in the checks this toolkit runs
# (GitHub/GitLab Pages, Vercel, Netlify, Heroku, Shopify, S3 static sites,
# Firebase/web.app, WordPress.com, Blogspot), not an attempt at completeness.
_MULTI_TENANT_SUFFIXES = frozenset(
    {
        "github.io",
        "githubusercontent.com",
        "gitlab.io",
        "pages.dev",
        "vercel.app",
        "netlify.app",
        "herokuapp.com",
        "myshopify.com",
        "wordpress.com",
        "blogspot.com",
        "s3.amazonaws.com",
        "web.app",
        "firebaseapp.com",
    }
)


def registrable_domain(host: str) -> str:
    """Approximate the registrable domain of a hostname."""
    host = (host or "").lower().strip(".")
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # Longest suffix first: a 3-label entry (s3.amazonaws.com) must be tried
    # before a 2-label slice of the same host could ever mask it.
    for suffix_len in (3, 2):
        if len(parts) > suffix_len and ".".join(parts[-suffix_len:]) in _MULTI_TENANT_SUFFIXES:
            return ".".join(parts[-(suffix_len + 1) :])
    if parts[-2] in _COMPOUND_SUFFIXES and len(parts[-1]) <= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize_domain(value: str) -> str:
    """Normalize a URL or hostname to a lowercase, non-www ASCII domain."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "//" in raw:
        raw = urlsplit(raw).netloc or urlsplit(raw).path
    raw = raw.split("/")[0].split("@")[-1].strip().rstrip(".").lower()
    if raw.startswith("[") or raw.count(":") > 1:
        return ""
    raw = raw.split(":")[0]
    raw = raw.removeprefix("www.")
    try:
        raw = raw.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError):
        return ""
    return raw if _DOMAIN_RE.match(raw) else ""


def normalize_url(value: str) -> str:
    """Normalize user input to an absolute HTTP(S) URL without touching DNS."""
    raw = (value or "").strip()
    if not raw:
        return ""
    scheme_prefix = raw.split(":", 1)[0].lower()
    if "//" not in raw and scheme_prefix.isalpha() and raw[len(scheme_prefix) :].startswith(":"):
        return ""
    if "//" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    return raw


def doh(name: str, rtype: str, timeout: float = 8.0) -> list[str]:
    """Query DNS over HTTPS and return record values; failures return an empty list."""
    if not name:
        return []
    try:
        client = _client(timeout)
    except NetworkUnavailable:
        return []
    with client:
        for endpoint in DOH_ENDPOINTS:
            try:
                response = client.get(
                    endpoint,
                    params={"name": name, "type": rtype},
                    headers={"Accept": "application/dns-json"},
                )
                if response.status_code != 200:
                    continue
                answers = response.json().get("Answer") or []
            except Exception:
                continue
            records = []
            for item in answers:
                data = str(item.get("data", "")).strip()
                if data:
                    records.append(data.strip('"').rstrip("."))
            if records:
                return records
    return []


def rdap(path: str, timeout: float = 12.0) -> dict[str, Any]:
    """Query RDAP and distinguish unsupported registries from parser failures."""
    try:
        client = _client(timeout)
    except NetworkUnavailable:
        return {"supported": False, "error": "httpx is required"}
    with client:
        try:
            response = client.get(
                f"{RDAP_BOOTSTRAP}/{path}",
                headers={"Accept": "application/rdap+json"},
            )
        except Exception as exc:
            return {"supported": False, "error": str(exc)}
        if response.status_code == 404:
            return {"supported": False, "error": "not found in RDAP"}
        if response.status_code >= 400:
            return {"supported": False, "error": f"RDAP HTTP {response.status_code}"}
        try:
            return {"supported": True, "data": response.json()}
        except ValueError:
            return {"supported": False, "error": "RDAP returned non-JSON"}


# Registries the default whois resolver does not reach. Without these it answers
# with the zone record instead of the domain's own, which reads as a real
# registration: every .ru domain came back with the .RU delegation date of 1994.
WHOIS_SERVERS_BY_TLD: dict[str, str] = {
    "ru": "whois.tcinet.ru",
    "su": "whois.tcinet.ru",
    "xn--p1ai": "whois.tcinet.ru",  # the Cyrillic .rf ccTLD, in punycode
}

# Fields a registry uses to refer to the authoritative server.
_WHOIS_REFERRAL_KEYS = ("whois", "refer", "registrar whois server")

_HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def _whois_field(text: str, keys: tuple[str, ...]) -> str | None:
    """First value of the first matching key, ignoring comment lines."""
    for line in text.splitlines():
        stripped = line.lstrip()
        if ":" not in stripped or stripped.startswith(("%", "#")):
            continue
        key, _, value = stripped.partition(":")
        if key.strip().lower() in keys and value.strip():
            return value.strip()
    return None


def whois_text(domain: str, timeout: float = 15.0, server: str | None = None) -> str | None:
    """Return raw system-whois output as an optional ccTLD fallback."""
    binary = shutil.which("whois")
    if not binary or not domain:
        return None
    argv = [binary]
    if server:
        # The server may come from a registry response, i.e. untrusted input.
        # Only a syntactically valid hostname is ever passed on, and it is
        # passed as an argument vector, never through a shell.
        if not _HOSTNAME_RE.match(server.lower()):
            return None
        argv += ["-h", server]
    argv.append(domain)
    try:
        process = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.SubprocessError, OSError):
        return None
    return process.stdout or None


def whois_lookup(domain: str, timeout: float = 15.0) -> tuple[str | None, str | None]:
    """Query whois, following one registry referral. Returns ``(text, server)``.

    The referral hop is what separates a domain's own record from the record of
    its zone; a caller still has to confirm the answer is about the domain it
    asked for.
    """
    if not domain:
        return None, None
    tld = domain.rsplit(".", 1)[-1].lower()
    mapped = WHOIS_SERVERS_BY_TLD.get(tld)
    if mapped:
        text = whois_text(domain, timeout, server=mapped)
        if text:
            return text, mapped

    text = whois_text(domain, timeout)
    if not text:
        return None, None

    referral = _whois_field(text, _WHOIS_REFERRAL_KEYS)
    if referral:
        referral = referral.split("//")[-1].split("/")[0].strip().lower()
        if referral and referral != (mapped or ""):
            deeper = whois_text(domain, timeout, server=referral)
            if deeper:
                return deeper, referral
    return text, None
