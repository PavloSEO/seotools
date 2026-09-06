"""Analyze web-server logs to show how crawlers actually use a site.

Logs expose search-engine behavior rather than assumptions about it. A crawl shows
what can be discovered; access logs show what was requested, how often, which
responses were served, and where crawl budget was spent.

The parser supports Apache/Nginx Common and Combined formats plus IIS W3C Extended.
It detects the format from sample lines. IIS column positions come from each
``#Fields:`` directive rather than a hard-coded order because administrators can
rearrange or change fields during a file.

Files are processed line by line so a multi-gigabyte log does not require matching
memory. High-cardinality accumulators are capped to bound memory use.

Bot verification
----------------
A user-agent is trivial to spoof, so a ``Googlebot`` string alone proves nothing.
Authenticity requires forward-confirmed reverse DNS: the client's PTR hostname
must end in an official suffix, and resolving that hostname forward must return
the original IP. An attacker can forge a user-agent but not another provider's
authoritative DNS records.

DNS verification performs network requests, so it is disabled by default and must
be enabled explicitly with ``verify_bots=True``.

One failure mode needs special handling. If outbound DNS is blocked by the audited
environment, every lookup could appear unverified and falsely imply that all bots
are impersonators. A canary PTR lookup first confirms that reverse DNS is available.
If the canary fails, verification is reported as unavailable rather than producing
an authenticity verdict.
"""

from __future__ import annotations

import ipaddress
import itertools
import re
import socket
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

# Official reverse-DNS suffixes. A non-empty tuple means the bot is verifiable.
GOOGLE = (".googlebot.com", ".google.com", ".googleusercontent.com")
BING = (".search.msn.com",)
YANDEX = (".yandex.com", ".yandex.net", ".yandex.ru")
APPLE = (".applebot.apple.com",)
BAIDU = (".baidu.com", ".baidu.jp")

# Order matters because the first match wins. Specific signatures such as
# Googlebot-Image precede generic Googlebot, and the catch-all remains last.
BOT_PATTERNS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (r"Googlebot[^)]*Mobile|Mobile[^)]*Googlebot", "Googlebot Smartphone", "googlebot", GOOGLE),
    (r"Googlebot-Image", "Googlebot Image", "googlebot", GOOGLE),
    (r"Googlebot-Video", "Googlebot Video", "googlebot", GOOGLE),
    (r"Googlebot-News", "Googlebot News", "googlebot", GOOGLE),
    (r"AdsBot-Google-Mobile", "AdsBot Google Mobile", "googlebot", GOOGLE),
    (r"AdsBot-Google", "AdsBot Google", "googlebot", GOOGLE),
    (r"Mediapartners-Google", "Mediapartners (AdSense)", "googlebot", GOOGLE),
    (r"Google-InspectionTool", "Google InspectionTool", "googlebot", GOOGLE),
    (r"Google-Extended", "Google-Extended (Gemini)", "ai", GOOGLE),
    (r"GoogleOther", "GoogleOther", "googlebot", GOOGLE),
    (r"APIs-Google", "APIs-Google", "googlebot", GOOGLE),
    (r"FeedFetcher-Google", "FeedFetcher-Google", "googlebot", GOOGLE),
    (r"Googlebot", "Googlebot", "googlebot", GOOGLE),
    (r"BingPreview", "BingPreview", "bingbot", BING),
    (r"adidxbot", "AdIdxBot (Bing Ads)", "bingbot", BING),
    (r"bingbot", "Bingbot", "bingbot", BING),
    (r"msnbot", "MSNBot", "bingbot", BING),
    (r"YandexMobileBot", "YandexMobileBot", "yandexbot", YANDEX),
    (r"YandexImages", "YandexImages", "yandexbot", YANDEX),
    (r"YandexBot", "YandexBot", "yandexbot", YANDEX),
    (
        r"Yandex(?:Accessibility|Metrika|Webmaster|[A-Za-z]+)?",
        "Yandex (other)",
        "yandexbot",
        YANDEX,
    ),
    (r"DuckDuckBot|DuckDuckGo-Favicons-Bot", "DuckDuckBot", "search", ()),
    (r"Baiduspider", "Baiduspider", "search", BAIDU),
    (r"Sogou web spider", "Sogou", "search", ()),
    (r"Exabot", "Exabot", "search", ()),
    (r"Applebot", "Applebot", "search", APPLE),
    (r"SeznamBot", "SeznamBot", "search", ()),
    (r"PetalBot", "PetalBot", "search", ()),
    (r"GPTBot", "GPTBot (OpenAI)", "ai", ()),
    (r"OAI-SearchBot", "OAI-SearchBot (OpenAI)", "ai", ()),
    (r"ChatGPT-User", "ChatGPT-User", "ai", ()),
    (r"ClaudeBot|Claude-Web|anthropic-ai", "ClaudeBot (Anthropic)", "ai", ()),
    (r"PerplexityBot", "PerplexityBot", "ai", ()),
    (r"CCBot", "CCBot (Common Crawl)", "ai", ()),
    (r"Bytespider", "Bytespider (ByteDance)", "ai", ()),
    (r"Amazonbot", "Amazonbot", "ai", ()),
    (r"facebookexternalhit|facebookcatalog|meta-externalagent", "Facebook", "social", ()),
    (r"Twitterbot", "Twitterbot", "social", ()),
    (r"LinkedInBot", "LinkedInBot", "social", ()),
    (r"Slackbot", "Slackbot", "social", ()),
    (r"WhatsApp", "WhatsApp", "social", ()),
    (r"TelegramBot", "TelegramBot", "social", ()),
    (r"Discordbot", "Discordbot", "social", ()),
    (r"Pinterest(?:bot)?", "Pinterestbot", "social", ()),
    (r"AhrefsBot", "AhrefsBot", "seo-tool", ()),
    (r"SemrushBot", "SemrushBot", "seo-tool", ()),
    (r"MJ12bot", "MJ12bot (Majestic)", "seo-tool", ()),
    (r"DotBot", "DotBot (Moz)", "seo-tool", ()),
    (r"rogerbot", "rogerbot (Moz)", "seo-tool", ()),
    (r"BLEXBot", "BLEXBot", "seo-tool", ()),
    (r"DataForSeoBot", "DataForSeoBot", "seo-tool", ()),
    (r"Screaming Frog SEO Spider", "Screaming Frog", "seo-tool", ()),
    (r"\b(bot|crawler|spider|crawl)\b", "Other bot", "other", ()),
)

_COMPILED = tuple(
    (re.compile(p, re.IGNORECASE), name, family, sfx) for p, name, family, sfx in BOT_PATTERNS
)

# Combined is a superset of Common and must be checked first.
_COMBINED_RE = re.compile(
    r'^(\S+)\s+\S+\s+\S+\s+\[([^\]]+)\]\s+"([A-Z!]+)\s+(\S+)(?:\s+[^"]*)?"'
    r'\s+(\d{3})\s+(\S+)\s+"([^"]*)"\s+"([^"]*)"'
)
_COMMON_RE = re.compile(
    r'^(\S+)\s+\S+\s+\S+\s+\[([^\]]+)\]\s+"([A-Z!]+)\s+(\S+)(?:\s+[^"]*)?"'
    r"\s+(\d{3})\s+(\S+)"
)
_TS_RE = re.compile(r"^(\d{2})/([A-Za-z]{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})\s*([+-]\d{4})?$")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}

MAX_TRACKED_PATHS = 5000
MAX_TRACKED_IPS = 2000
RDNS_TIMEOUT = 3.0
# DNS canary used to confirm PTR lookups work at all; Google's public resolver has
# a stable reverse record.
DNS_CANARY = "8.8.8.8"


def detect_bot(user_agent: str | None) -> dict[str, Any] | None:
    """Identify a known bot; ``None`` means human or unrecognized user-agent."""
    if not user_agent or user_agent == "-":
        return None
    for rx, name, family, suffixes in _COMPILED:
        if rx.search(user_agent):
            return {
                "name": name,
                "family": family,
                "rdns_suffixes": list(suffixes),
                "verifiable": bool(suffixes),
            }
    return None


def parse_apache_timestamp(raw: str) -> datetime | None:
    """Convert an Apache timestamp such as ``10/Oct/2000:13:55:36 -0700`` to UTC.

    When the log omits a time zone, the value is assumed to be UTC. This is an
    explicit assumption, not a measured fact; a server in another zone introduces
    a systematic offset.
    """
    m = _TS_RE.match(raw.strip())
    if not m:
        return None
    day, mon, year, hh, mm, ss, tz = m.groups()
    month = _MONTHS.get(mon)
    if month is None:
        return None
    try:
        naive = datetime(int(year), month, int(day), int(hh), int(mm), int(ss), tzinfo=timezone.utc)
    except ValueError:
        return None
    if tz:
        sign = -1 if tz[0] == "-" else 1
        offset = timedelta(minutes=int(tz[1:3]) * 60 + int(tz[3:5]))
        naive -= sign * offset
    return naive


def _parse_apache(line: str) -> dict[str, Any] | None:
    m = _COMBINED_RE.match(line)
    referer = agent = None
    if m:
        ip, ts, method, path, status, size, referer, agent = m.groups()
    else:
        m = _COMMON_RE.match(line)
        if not m:
            return None
        ip, ts, method, path, status, size = m.groups()
    try:
        bytes_sent = int(size)
    except ValueError:
        bytes_sent = 0  # ``-`` means no response body size was recorded.
    return {
        "ip": ip,
        "time": parse_apache_timestamp(ts),
        "method": method,
        "path": path,
        "status": int(status),
        "bytes": bytes_sent,
        "referer": None if referer in (None, "", "-") else referer,
        "user_agent": None if agent in (None, "", "-") else agent,
    }


class _IISParser:
    """Read field positions from ``#Fields:``, which may change within a log."""

    WANTED = (
        "date",
        "time",
        "cs-uri-stem",
        "cs-uri-query",
        "cs-method",
        "c-ip",
        "cs(User-Agent)",
        "cs(Referer)",
        "sc-status",
        "sc-bytes",
    )

    def __init__(self) -> None:
        self.fields: list[str] = []

    def parse(self, line: str) -> dict[str, Any] | None:
        if line.startswith("#"):
            if line.lower().startswith("#fields:"):
                self.fields = line.split(":", 1)[1].split()
            return None
        if not self.fields:
            return None
        parts = line.split()
        got: dict[str, str] = {}
        for name in self.WANTED:
            if name in self.fields:
                idx = self.fields.index(name)
                if idx < len(parts):
                    got[name] = parts[idx]
        path = got.get("cs-uri-stem")
        if not path:
            return None
        query = got.get("cs-uri-query")
        if query and query != "-":
            path = f"{path}?{query}"
        when = None
        if got.get("date") and got.get("time"):
            try:
                when = datetime.strptime(
                    f"{got['date']} {got['time']}", "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                when = None
        try:
            status = int(got.get("sc-status", "0"))
        except ValueError:
            status = 0
        try:
            bytes_sent = int(got.get("sc-bytes", "0"))
        except ValueError:
            bytes_sent = 0
        # IIS encodes spaces in user-agent values as plus signs.
        agent = (got.get("cs(User-Agent)") or "").replace("+", " ").strip()
        referer = got.get("cs(Referer)")
        return {
            "ip": got.get("c-ip", ""),
            "time": when,
            "method": got.get("cs-method", ""),
            "path": path,
            "status": status,
            "bytes": bytes_sent,
            "referer": None if referer in (None, "", "-") else referer,
            "user_agent": agent or None,
        }


def detect_format(sample: Iterable[str]) -> str | None:
    """Detect ``combined``, ``common``, or ``iis`` format from sample lines."""
    combined = common = 0
    for line in sample:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#") and "fields:" in line.lower():
            return "iis"
        if _COMBINED_RE.match(line):
            combined += 1
        elif _COMMON_RE.match(line):
            common += 1
    if combined:
        return "combined"
    if common:
        return "common"
    return None


def verify_bot_rdns(
    ip: str, suffixes: Iterable[str], timeout: float = RDNS_TIMEOUT
) -> dict[str, Any]:
    """Verify a bot with forward-confirmed reverse DNS.

    The PTR hostname must end in an official suffix and resolve forward to the
    original IP. Returns ``{verified, reason, hostname}``, distinguishing a failed
    authenticity check from an unavailable DNS check; collapsing both to ``False``
    would misrepresent the evidence.
    """
    suffixes = tuple(suffixes)
    if not suffixes:
        return {
            "verified": None,
            "reason": "No official PTR suffix is available for this bot",
            "hostname": None,
        }
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        try:
            hostname, _, _ = socket.gethostbyaddr(ip)
        except (socket.herror, socket.gaierror):
            return {"verified": False, "reason": "No PTR record was found", "hostname": None}
        except OSError as exc:
            return {"verified": None, "reason": f"DNS is unavailable: {exc}", "hostname": None}
        low = hostname.lower()
        if not any(low.endswith(s) for s in suffixes):
            return {
                "verified": False,
                "reason": f"PTR hostname {hostname} is outside the official domains",
                "hostname": hostname,
            }
        try:
            _, _, addrs = socket.gethostbyname_ex(hostname)
        except OSError as exc:
            return {
                "verified": None,
                "reason": f"Forward resolution of {hostname} is unavailable: {exc}",
                "hostname": hostname,
            }
        if ip in addrs:
            return {
                "verified": True,
                "reason": "PTR and forward resolution confirm the same IP",
                "hostname": hostname,
            }
        return {
            "verified": False,
            "reason": f"PTR hostname {hostname} does not resolve back to {ip}",
            "hostname": hostname,
        }
    finally:
        socket.setdefaulttimeout(old)


def _dns_available(timeout: float = RDNS_TIMEOUT) -> bool:
    """Return whether reverse DNS works from this environment.

    Without the canary, blocked DNS could make every bot appear fake, which is the
    most damaging false conclusion this analyzer could report.
    """
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)
    try:
        socket.gethostbyaddr(DNS_CANARY)
        return True
    except OSError:
        return False
    finally:
        socket.setdefaulttimeout(old)


def _section(path: str) -> str:
    """Return the first path segment as a site section; ``/`` is the root."""
    cut = path.split("?", 1)[0].strip("/")
    if not cut:
        return "/"
    return "/" + cut.split("/", 1)[0]


def analyze_log(
    path: str, *, verify_bots: bool = False, max_lines: int = 5_000_000, sample_size: int = 50
) -> dict[str, Any]:
    """Parse a log and calculate aggregates; only bot verification uses the network."""
    try:
        handle = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115 - report open errors
    except OSError as exc:
        return {"ok": False, "path": path, "error": str(exc)}

    with handle:
        sample = []
        for _ in range(sample_size):
            line = handle.readline()
            if not line:
                break
            sample.append(line)
        fmt = detect_format(sample)
        if fmt is None:
            return {
                "ok": False,
                "path": path,
                "error": "Unrecognized log format: expected Apache Common/Combined or IIS W3C",
            }

        iis = _IISParser() if fmt == "iis" else None
        parse = iis.parse if iis else _parse_apache

        total = parsed = skipped = 0
        truncated = False
        by_family: dict[str, Counter] = defaultdict(Counter)
        bot_hits: Counter = Counter()
        bot_bytes: Counter = Counter()
        bot_ips: dict[str, set] = defaultdict(set)
        status_by_family: dict[str, Counter] = defaultdict(Counter)
        section_by_family: dict[str, Counter] = defaultdict(Counter)
        paths_by_family: dict[str, Counter] = defaultdict(Counter)
        paths_truncated: set[str] = set()
        ips_truncated: set[str] = set()
        daily: Counter = Counter()
        first_time = last_time = None

        # chain(), not [*sample, *handle]: the list literal forces the whole remaining
        # file into memory before the first row is even parsed, which is exactly the
        # eager read max_lines exists to prevent (#252). Stop pulling from the iterator
        # entirely once the cap is hit, instead of continuing to "skip" every line the
        # file still has -- that no longer bounds reads, only the skip counter.
        for line in itertools.chain(sample, handle):
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if total >= max_lines:
                truncated = True
                break
            total += 1
            row = parse(line)
            if row is None:
                skipped += 1
                continue
            parsed += 1

            bot = detect_bot(row.get("user_agent"))
            family = bot["family"] if bot else "human"
            name = bot["name"] if bot else "human"

            by_family[family][name] += 1
            status_by_family[family][row["status"]] += 1
            section_by_family[family][_section(row["path"])] += 1
            # A full counter must keep incrementing paths it already tracks -- only a
            # brand-new path should be refused once the cap is reached. Capping every
            # increment here (#251) let a path counted before the cap filled silently
            # undercount every repeat after: the family total stayed complete while its
            # own top-paths breakdown quietly fell out of sync with it.
            family_paths = paths_by_family[family]
            path_value = row["path"]
            if path_value in family_paths or len(family_paths) < MAX_TRACKED_PATHS:
                family_paths[path_value] += 1
            else:
                paths_truncated.add(family)
            if bot:
                bot_hits[name] += 1
                bot_bytes[name] += row.get("bytes") or 0
                # Once the tracked-IP set fills, a brand-new address must be recorded as
                # refused rather than silently dropped -- otherwise unique_ips reports the
                # same value for 2,000 and 200,000 distinct sources with no way to tell
                # an exact count from a saturated sample (#330).
                ip_value = row["ip"]
                if ip_value in bot_ips[name] or len(bot_ips[name]) < MAX_TRACKED_IPS:
                    bot_ips[name].add(ip_value)
                else:
                    ips_truncated.add(name)
            when = row.get("time")
            if when:
                daily[when.date().isoformat()] += 1
                first_time = when if first_time is None or when < first_time else first_time
                last_time = when if last_time is None or when > last_time else last_time

    verification = _verify_batch(bot_ips, verify_bots)

    result: dict[str, Any] = {
        "ok": True,
        "path": path,
        "format": fmt,
        "lines": {"total": total, "parsed": parsed, "skipped": skipped, "truncated": truncated},
        "period": {
            "from": first_time.isoformat() if first_time else None,
            "to": last_time.isoformat() if last_time else None,
        },
        "by_family": {f: dict(c.most_common(20)) for f, c in by_family.items()},
        "bots": [
            {
                "name": n,
                "hits": h,
                "bytes": bot_bytes[n],
                "unique_ips": len(bot_ips[n]),
                "unique_ips_truncated": n in ips_truncated,
            }
            for n, h in bot_hits.most_common(40)
        ],
        "status_by_family": {f: dict(sorted(c.items())) for f, c in status_by_family.items()},
        "sections_by_family": {f: dict(c.most_common(20)) for f, c in section_by_family.items()},
        "top_paths_by_family": {f: dict(c.most_common(15)) for f, c in paths_by_family.items()},
        "paths_truncated": sorted(paths_truncated),
        "ips_truncated": sorted(ips_truncated),
        "daily": dict(sorted(daily.items())),
        "verification": verification,
    }
    result["findings"] = _findings(result)
    return result


def _verify_batch(bot_ips: dict[str, set], enabled: bool) -> dict[str, Any]:
    """Verify bot authenticity using a sample IP from each detected bot family."""
    if not enabled:
        return {
            "checked": False,
            "reason": "Verification is disabled because it requires network access; enable it explicitly",
        }
    if not _dns_available():
        return {
            "checked": False,
            "dns_available": False,
            "reason": "Reverse DNS is unavailable from this machine, so bot authenticity "
            "cannot be checked. This does not mean the bots are fake.",
        }

    checks: list[dict[str, Any]] = []
    for name, ips in bot_ips.items():
        sample_ip = next((i for i in sorted(ips) if _is_ip(i)), None)
        if sample_ip is None:
            continue
        suffixes = next((s for _, n, _, s in BOT_PATTERNS if n == name), ())
        if not suffixes:
            continue
        verdict = verify_bot_rdns(sample_ip, suffixes)
        checks.append({"bot": name, "ip": sample_ip, **verdict})
    return {"checked": True, "dns_available": True, "checks": checks}


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _findings(r: dict[str, Any]) -> list[str]:
    out: list[str] = []
    lines = r["lines"]
    if lines["parsed"] == 0:
        return ["No lines were parsed; the detected format is wrong or the file is empty"]
    if lines["skipped"] > lines["parsed"] * 0.1:
        out.append(
            f"Failed to parse {lines['skipped']} of {lines['total']} lines; "
            "the log may mix formats or contain custom fields"
        )

    fams = r["by_family"]
    if "googlebot" not in fams:
        out.append(
            "Googlebot does not appear in the log; the site may not be crawled, "
            "or the log period may be too short"
        )
    if "ai" in fams:
        total_ai = sum(fams["ai"].values())
        out.append(f"AI crawlers made {total_ai} requests ({', '.join(list(fams['ai'])[:4])})")
    if "seo-tool" in fams:
        total_seo = sum(fams["seo-tool"].values())
        out.append(
            f"Third-party SEO crawlers made {total_seo} requests, consuming "
            "your bandwidth and server resources"
        )

    for family, statuses in r["status_by_family"].items():
        if family == "human":
            continue
        errors = sum(c for s, c in statuses.items() if s >= 400)
        total = sum(statuses.values()) or 1
        if errors / total > 0.1:
            out.append(
                f"{family}: {errors} of {total} requests returned errors "
                f"({errors / total:.0%}), wasting crawl budget"
            )

    ver = r["verification"]
    if ver.get("checked"):
        fake = [c["bot"] for c in ver.get("checks", []) if c.get("verified") is False]
        if fake:
            out.append(
                f"Reverse-DNS verification failed for {', '.join(fake)}; "
                "these requests may be impersonating known bots"
            )
    elif ver.get("dns_available") is False:
        out.append("Bot authenticity was not checked because reverse DNS is unavailable")
    return out
