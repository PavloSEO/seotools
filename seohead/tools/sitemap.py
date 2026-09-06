"""Bounded sitemap crawler and parser.

Recursively fetches a sitemap URL, following ``<sitemapindex>`` entries down to child
``<urlset>`` documents, and collects every ``<url>`` entry.

The crawler understands:

* gzip-compressed sitemaps (``*.gz`` files and ``Content-Encoding: gzip``),
* nested sitemap indexes (index -> child sitemaps -> urlsets),
* XML namespaces (they are stripped before matching element names),
* duplicate ``loc`` values across sitemaps (reported, not re-emitted),
* a global cap on the number of URLs collected (truncation is flagged).

Public API
----------
``crawl(url, concurrency=3) -> dict``
    Fetch and recursively expand a sitemap tree. Never raises: on failure it
    records an entry in the returned ``errors`` list.

``parse_sitemap(xml_bytes, base_url) -> dict``
    Pure helper that classifies a single sitemap document as an ``index`` or
    ``urlset`` and extracts its entries. No network access.

Depends only on the Python stdlib plus ``httpx`` and ``lxml``.
"""

from __future__ import annotations

import gzip
import io
import re
import sqlite3
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from lxml import etree

from seohead.recon.net import http_client

__all__ = [
    "classify_url_type",
    "crawl",
    "detect_host_policy",
    "normalize_url",
    "parse_sitemap",
    "strip_www",
]

# ── Limits ──────────────────────────────────────────────────────────────────
MAX_SITEMAPS = 5000
MAX_URLS = 300_000
MAX_XML_BYTES = 12 * 1024 * 1024
TIMEOUT_S = 25.0
MAX_REDIRECTS = 8

_USER_AGENT = "Mozilla/5.0 (compatible; SEOHEAD-Tools/3.0; +https://seohead.tech/seotools)"
_GZIP_MAGIC = b"\x1f\x8b"
_GUNZIP_CHUNK = 64 * 1024  # read granularity for the bounded gunzip loop below
# One message for "too large", raised whichever stage (compressed or decompressed) catches
# it, so a caller sees one consistent failure rather than branching on which check fired.
_TOO_LARGE_MSG = f"Response too large (> {MAX_XML_BYTES} bytes)"


class _StreamingDeduper:
    """Per-selected-root normalized dedup without an in-memory URL map."""

    def __init__(self, sink: Callable[[dict], None]) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="seohead-sitemap-")
        self._con = sqlite3.connect(f"{self._temporary.name}/keys.sqlite")
        self._con.execute("PRAGMA temp_store=FILE")
        self._con.execute("PRAGMA cache_size=-8192")
        self._con.execute("CREATE TABLE keys (normalized TEXT PRIMARY KEY)")
        self._sink = sink
        self.count = 0
        self.duplicates = 0

    def add(self, entry: dict, normalized: str) -> bool:
        try:
            self._con.execute("INSERT INTO keys(normalized) VALUES (?)", (normalized,))
        except sqlite3.IntegrityError:
            self.duplicates += 1
            return False
        try:
            self._sink(
                {
                    "loc": entry["loc"],
                    "loc_normalized": normalized,
                    "lastmod": entry.get("lastmod"),
                    "changefreq": entry.get("changefreq"),
                    "priority": entry.get("priority"),
                }
            )
        except Exception:
            self.close()
            raise
        self.count += 1
        return True

    def close(self) -> None:
        self._con.close()
        self._temporary.cleanup()


# ── Pure helpers ────────────────────────────────────────────────────────────
def strip_www(host: str) -> str:
    """Return *host* without a leading ``www.`` label."""
    return host.removeprefix("www.")


def normalize_url(url_str: str) -> str:
    """Canonicalise a URL for de-duplication.

    Lower-cases the host, drops default ports (80/443), removes the fragment,
    and strips a trailing slash from non-root paths. Raises ``ValueError`` if
    the string has no scheme or host (mirrors the TS ``new URL`` behaviour of
    rejecting non-absolute inputs).
    """
    parts = urlsplit(url_str.strip())
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"Invalid URL: {url_str!r}")

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None

    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = re.sub(r"/+$", "", path)

    # Fragment intentionally dropped; query preserved.
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def classify_url_type(url_str: str) -> str:
    """Classify a URL as a ``"folder"`` or a ``"finalUrl"``.

    The root path is a folder; otherwise the last path segment decides: a dot
    in the leaf (e.g. ``page.html``) marks a final URL, everything else a
    folder. On a malformed URL the original code defaults to ``"finalUrl"``.
    """
    try:
        parts = urlsplit(url_str)
        if not parts.scheme or not parts.netloc:
            return "finalUrl"
        path = parts.path or "/"
        if path == "/":
            return "folder"
        segments = [s for s in path.split("/") if s]
        leaf = segments[-1] if segments else ""
        return "finalUrl" if "." in leaf else "folder"
    except Exception:
        return "finalUrl"


def detect_host_policy(urls: list[dict]) -> dict:
    """Summarise the www vs non-www split across collected URLs.

    Returns ``{dominant, mixed, counts: {www, non_www}, by_host}`` where
    ``dominant`` is ``"www"`` when www hosts are at least as common as non-www
    (ties favour www, matching the TS ``>=``), and ``mixed`` is true when both
    styles appear.
    """
    counts = {"www": 0, "non_www": 0}
    by_host: dict[str, int] = {}
    for u in urls:
        try:
            host = (urlsplit(u["loc"]).hostname or "").lower()
        except Exception:
            continue
        if not host:
            continue
        if host.startswith("www."):
            counts["www"] += 1
        else:
            counts["non_www"] += 1
        by_host[host] = by_host.get(host, 0) + 1
    dominant = "www" if counts["www"] >= counts["non_www"] else "non_www"
    mixed = counts["www"] > 0 and counts["non_www"] > 0
    return {
        "dominant": dominant,
        "mixed": mixed,
        "counts": counts,
        "by_host": list(by_host.items()),
    }


def _local_name(tag: object) -> str:
    """Return an element's tag name with any XML namespace stripped."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _child_text(element: etree._Element, name: str) -> str | None:
    """Return the trimmed text of the first namespace-agnostic child *name*."""
    for child in element:
        if _local_name(child.tag) == name:
            text = child.text
            return text.strip() if text else None
    return None


def _parse_priority(value: str | None) -> float | None:
    """Parse a ``<priority>`` value into a float, or ``None`` if unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_text_sitemap(text_bytes: bytes, base_url: str) -> dict:
    """Parse a plain-text sitemap with one URL per line and ``#`` comments.

    The sitemaps.org specification permits ``.txt`` sitemaps in which every
    non-empty line is an absolute URL. Relative references are resolved against
    ``base_url``; blank lines and comments are ignored. Pure; no network access.
    """
    try:
        text = text_bytes.decode("utf-8", errors="replace")
    except (UnicodeDecodeError, AttributeError):
        text = str(text_bytes)
    urls: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            absolute = urljoin(base_url, line)
        except ValueError:
            continue
        # Keep only HTTP(S); plain-text sitemap files sometimes contain noise.
        if not absolute.startswith(("http://", "https://")):
            continue
        urls.append({"loc": absolute, "lastmod": None, "changefreq": None, "priority": None})
    return {"ok": True, "type": "text", "sitemaps": [], "urls": urls}


def parse_sitemap(xml_bytes: bytes, base_url: str) -> dict:
    """Classify and extract a single sitemap document. Pure; no network.

    Parameters
    ----------
    xml_bytes:
        Raw (already decompressed) XML bytes.
    base_url:
        URL the document was fetched from; used to resolve any relative
        ``<loc>`` values against an absolute base.

    Returns a dict of the form::

        {
            "ok": True,
            "type": "index" | "urlset" | "text" | "unknown",
            "sitemaps": [{"loc", "lastmod"}, ...],
            "urls": [{"loc", "lastmod", "changefreq", "priority"}, ...],
        }

    Plain-text sitemaps (one URL per line, optional ``#`` comments) are detected
    when the body does not look like XML and recognised as ``type: "text"``.

    On a parse error it returns ``{"ok": False, "error": <message>, ...}`` with
    empty ``sitemaps``/``urls`` rather than raising.
    """
    # Plain-text sitemap: one URL per line. A body without '<' is not XML.
    head = xml_bytes.lstrip()[:512]
    if head and b"<" not in head:
        return parse_text_sitemap(xml_bytes, base_url)

    try:
        # recover=True tolerates stray bytes / malformed markup, like the
        # lenient JS parser. huge_tree lifts libxml2's default node limits.
        parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False)
        root = etree.fromstring(xml_bytes, parser=parser)
    except (etree.XMLSyntaxError, ValueError) as exc:
        # A boundary case may still be non-XML text; fall back when '<' is absent.
        if b"<" not in (xml_bytes[:512]):
            return parse_text_sitemap(xml_bytes, base_url)
        return {
            "ok": False,
            "error": f"XML parse error: {exc}",
            "type": "unknown",
            "sitemaps": [],
            "urls": [],
        }

    if root is None:
        return {
            "ok": False,
            "error": "XML parse error: empty document",
            "type": "unknown",
            "sitemaps": [],
            "urls": [],
        }

    root_name = _local_name(root.tag)

    if root_name == "sitemapindex":
        sitemaps: list[dict] = []
        for child in root:
            if _local_name(child.tag) != "sitemap":
                continue
            loc = _child_text(child, "loc")
            if not loc:
                continue
            sitemaps.append(
                {
                    "loc": urljoin(base_url, loc),
                    "lastmod": _child_text(child, "lastmod"),
                }
            )
        return {"ok": True, "type": "index", "sitemaps": sitemaps, "urls": []}

    if root_name == "urlset":
        urls: list[dict] = []
        for child in root:
            if _local_name(child.tag) != "url":
                continue
            loc = _child_text(child, "loc")
            if not loc:
                continue
            urls.append(
                {
                    "loc": urljoin(base_url, loc),
                    "lastmod": _child_text(child, "lastmod"),
                    "changefreq": _child_text(child, "changefreq"),
                    "priority": _parse_priority(_child_text(child, "priority")),
                }
            )
        return {"ok": True, "type": "urlset", "sitemaps": [], "urls": urls}

    return {"ok": True, "type": "unknown", "sitemaps": [], "urls": []}


# ── Fetching ────────────────────────────────────────────────────────────────
def _maybe_gunzip(url: str, body: bytes) -> bytes:
    """Decompress *body* if it is a ``.gz`` sitemap or has a gzip magic header.

    Reads through the decompressor in ``_GUNZIP_CHUNK``-sized steps and aborts as soon as
    the output exceeds :data:`MAX_XML_BYTES`, rather than materialising the whole stream
    first: ``gzip.decompress()`` has no size bound of its own, so a 2 MB payload can expand
    to 2 GiB in under two seconds regardless of the pre-decompression size check in
    :func:`_fetch` (issue #148). zlib's deflate format tops out around a 1032:1 compression
    ratio (https://www.zlib.net/zlib_tech.html), so bounding the *output* at
    ``MAX_XML_BYTES`` bounds the peak extra memory this can cost to about one document's
    worth of XML, independent of how the compressed bytes were crafted -- a compressed input
    already capped well under that ratio's worth of ``MAX_XML_BYTES`` can never reach the
    ceiling before ``_GzipFile.read()`` itself runs out of bytes to expand.

    Mirrors the bounded-read loop ``seohead/sf/core/sitemap_coverage.py``'s ``_safe_gunzip``
    already uses for its own, independent sitemap fetch -- the two modules don't share code
    across the tools/sf layering boundary, but the bound is the same one either way.

    ``httpx`` already handles the ``Content-Encoding`` header transparently, so this only
    needs to catch gzipped *payloads* (``.xml.gz`` files, or servers that gzip the body
    without advertising it).
    """
    path = urlsplit(url).path.lower()
    looks_gzip = body[:2] == _GZIP_MAGIC
    if not (path.endswith(".gz") or looks_gzip):
        return body
    out = bytearray()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
            while True:
                chunk = gz.read(_GUNZIP_CHUNK)
                if not chunk:
                    break
                out += chunk
                if len(out) > MAX_XML_BYTES:
                    raise ValueError(_TOO_LARGE_MSG)
    except (OSError, EOFError, gzip.BadGzipFile):
        # Not actually gzip (or truncated): fall back to the raw bytes, same as before.
        return body
    return bytes(out)


def _fetch(client: httpx.Client, url: str) -> bytes:
    """Fetch *url* and return its (decompressed) body bytes.

    Raises ``httpx.HTTPError`` on transport failure or a non-2xx status, and
    ``ValueError`` if the body exceeds :data:`MAX_XML_BYTES` either compressed or, once
    decompressed, in :func:`_maybe_gunzip`.
    """
    resp = client.get(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/xml,text/xml,application/gzip,*/*",
        },
    )
    resp.raise_for_status()
    body = resp.content
    if len(body) > MAX_XML_BYTES:
        raise ValueError(_TOO_LARGE_MSG)
    return _maybe_gunzip(str(resp.url), body)


# ── Crawl ───────────────────────────────────────────────────────────────────
def crawl(
    url: str,
    concurrency: int = 3,
    *,
    sink: Callable[[dict], None] | None = None,
    request_gate: Callable[[], None] | None = None,
) -> dict:
    """Recursively crawl a sitemap tree starting at *url*.

    Follows sitemap-index documents down to their child sitemaps and collects
    every ``<url>`` entry, de-duplicating by normalized ``loc``. Child sitemaps
    within a level are fetched in parallel using up to *concurrency* threads.

    Transport, HTTP and parse failures are recorded in the returned ``errors``
    list. A caller-supplied sink streams accepted URLs instead of returning
    their list; sink/storage failures propagate so incomplete writes cannot
    look like a successful expansion.

    Returns
    -------
    dict
        ``{"ok", "root", "count", "urls", "sitemaps", "duplicates",
        "errors", "truncated"}`` where:

        * ``root`` — the normalized starting URL,
        * ``count`` — number of unique URLs collected,
        * ``urls`` — ``[{"loc", "lastmod", "changefreq", "priority"}, ...]``,
        * ``sitemaps`` — per-document ``{"url", "type", "count"}`` records,
        * ``duplicates`` — ``loc`` values seen more than once,
        * ``errors`` — ``{"url", "error"}`` records,
        * ``truncated`` — ``True`` if a hard limit stopped collection.
    """
    try:
        root = normalize_url(url)
    except ValueError as exc:
        return {
            "ok": False,
            "root": url,
            "count": 0,
            "urls": [],
            "sitemaps": [],
            "duplicates": [],
            "errors": [{"url": url, "error": str(exc)}],
            "truncated": False,
        }

    concurrency = max(1, int(concurrency))
    visited: set[str] = set()
    queued: set[str] = {root}
    frontier: list[str] = [root]

    sitemaps: list[dict] = []
    errors: list[dict] = []
    seen_locs: dict[str, str] = {}
    duplicates: list[str] = []
    # A URL declared in two child sitemaps is usually a generator that ran twice, and it
    # distorts every count derived from the declared set. Counting them says it happened;
    # naming both documents says where to look.
    duplicate_sources: dict[str, list[str]] = {}
    all_urls: list[dict] = []
    truncated = False

    def process(target: str) -> dict:
        """Fetch and parse one sitemap; return a result record (no shared state)."""
        try:
            body = _fetch(client, target)
        except Exception as exc:
            return {"kind": "error", "url": target, "error": _err_message(exc)}
        parsed = parse_sitemap(body, target)
        if not parsed.get("ok"):
            return {"kind": "error", "url": target, "error": parsed.get("error", "parse error")}
        # Measured after decompression, because the protocol's 50 MB limit is about the document
        # a search engine has to parse, not about what travelled over the wire.
        return {"kind": "parsed", "url": target, "parsed": parsed, "bytes": len(body)}

    limits = httpx.Limits(max_connections=concurrency)
    options = {
        "follow_redirects": True,
        "max_redirects": MAX_REDIRECTS,
        "limits": limits,
    }
    if request_gate is not None:
        # httpx invokes request hooks for every hop it follows itself.  Calling
        # the gate here rather than in _fetch means redirects reserve the same
        # turn as the selected root and nested sitemap documents.
        options["event_hooks"] = {"request": [lambda _request: request_gate()]}
    client, _http2_capable = http_client(TIMEOUT_S, **options)
    with (
        client,
        closing(_StreamingDeduper(sink)) if sink is not None else nullcontext() as streaming,
        ThreadPoolExecutor(max_workers=concurrency) as pool,
    ):
        while frontier and not truncated:
            if len(visited) >= MAX_SITEMAPS:
                truncated = True
                break

            batch = [u for u in frontier if u not in visited]
            frontier = []
            for u in batch:
                visited.add(u)
                queued.discard(u)

            for result in pool.map(process, batch):
                if truncated:
                    # The cap already tripped on an earlier document in this same
                    # frontier batch. pool.map() has no way to cancel the results
                    # still queued behind it, so we must keep draining the
                    # iterator but stop appending anything it yields — otherwise
                    # every remaining document in the batch adds at least one
                    # more URL before its own inner-loop check ever fires, and
                    # `count` overshoots MAX_URLS (#473).
                    break

                target = result["url"]
                if result["kind"] == "error":
                    errors.append({"url": target, "error": result["error"]})
                    continue

                parsed = result["parsed"]
                if parsed["type"] == "index":
                    children = parsed["sitemaps"]
                    sitemaps.append(
                        {
                            "url": target,
                            "type": "index",
                            "count": len(children),
                            "bytes": result.get("bytes", 0),
                            "declared": len(children),
                        }
                    )
                    for child in children:
                        try:
                            norm = normalize_url(child["loc"])
                        except ValueError:
                            continue
                        if norm not in visited and norm not in queued:
                            frontier.append(norm)
                            queued.add(norm)
                elif parsed["type"] in ("urlset", "text"):
                    added = 0
                    for entry in parsed["urls"]:
                        try:
                            norm_loc = normalize_url(entry["loc"])
                        except ValueError:
                            continue
                        if streaming is not None:
                            if not streaming.add(entry, norm_loc):
                                continue
                            added += 1
                            if streaming.count >= MAX_URLS:
                                truncated = True
                                break
                            continue
                        if norm_loc in seen_locs:
                            duplicates.append(norm_loc)
                            sources = duplicate_sources.setdefault(norm_loc, [seen_locs[norm_loc]])
                            if target not in sources:
                                sources.append(target)
                            continue
                        seen_locs[norm_loc] = target
                        all_urls.append(
                            {
                                # The address the sitemap published, not the normalised key.
                                # Normalisation is right for deciding whether two entries are
                                # the same URL and wrong for deciding what to request: a
                                # sitemap declaring /a/ must not cause a fetch of /a, which on
                                # most CMSes is a 301 the crawler invented for itself (#115).
                                "loc": entry["loc"],
                                # Kept beside it so every consumer that was comparing on the
                                # normalised form still can, without re-deriving it.
                                "loc_normalized": norm_loc,
                                "lastmod": entry.get("lastmod"),
                                "changefreq": entry.get("changefreq"),
                                "priority": entry.get("priority"),
                            }
                        )
                        added += 1
                        if len(all_urls) >= MAX_URLS:
                            truncated = True
                            break
                    sitemaps.append(
                        {
                            "url": target,
                            "type": parsed["type"],
                            # Added to the run after de-duplication and the global budget.
                            "count": added,
                            # What this one document declares, before either — the number the
                            # protocol's 50,000-URL limit is actually about.
                            "declared": len(parsed["urls"]),
                            "bytes": result.get("bytes", 0),
                        }
                    )
                else:
                    errors.append({"url": target, "error": "Unknown sitemap format"})

    count = streaming.count if streaming is not None else len(all_urls)
    duplicate_count = streaming.duplicates if streaming is not None else len(duplicates)
    result = {
        "ok": True,
        "root": root,
        "count": count,
        "urls": [] if sink is not None else all_urls,
        "sitemaps": sitemaps,
        "duplicates": [] if sink is not None else duplicates,
        "duplicate_sources": {} if sink is not None else duplicate_sources,
        "errors": errors,
        "truncated": truncated,
    }
    if sink is not None:
        result["duplicate_count"] = duplicate_count
    return result


def _err_message(exc: Exception) -> str:
    """Render an exception as a concise, HTTP-aware message string."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "Timeout"
    msg = str(exc).strip()
    return msg or exc.__class__.__name__


# ── Smoke test (no network) ─────────────────────────────────────────────────
if __name__ == "__main__":
    index_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/sitemap-1.xml</loc><lastmod>2024-01-01</lastmod></sitemap>
      <sitemap><loc>/sitemap-2.xml</loc></sitemap>
    </sitemapindex>"""
    idx = parse_sitemap(index_xml, "https://example.com/sitemap.xml")
    assert idx["type"] == "index", idx
    assert idx["sitemaps"][0]["loc"] == "https://example.com/sitemap-1.xml"
    assert idx["sitemaps"][1]["loc"] == "https://example.com/sitemap-2.xml", idx

    urlset_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/a.html</loc><priority>0.8</priority><changefreq>daily</changefreq></url>
      <url><loc>https://example.com/blog/</loc><lastmod>2024-02-02</lastmod></url>
      <url><loc>bad</loc></url>
    </urlset>"""
    us = parse_sitemap(urlset_xml, "https://example.com/sitemap.xml")
    assert us["type"] == "urlset", us
    assert us["urls"][0]["priority"] == 0.8, us
    assert us["urls"][0]["changefreq"] == "daily"
    assert us["urls"][2]["loc"] == "https://example.com/bad"  # resolved against base

    assert normalize_url("https://Example.com:443/path/") == "https://example.com/path"
    assert classify_url_type("https://example.com/page.html") == "finalUrl"
    assert classify_url_type("https://example.com/blog") == "folder"
    assert classify_url_type("https://example.com/") == "folder"
    assert strip_www("www.example.com") == "example.com"

    policy = detect_host_policy(
        [{"loc": "https://www.example.com/a"}, {"loc": "https://example.com/b"}]
    )
    assert policy["mixed"] is True, policy
    assert policy["counts"] == {"www": 1, "non_www": 1}

    bad = parse_sitemap(b"<<not xml", "https://example.com/")
    assert bad["type"] in ("unknown", "urlset", "index"), bad  # recover parser is lenient

    print("sitemap.py self-check OK")
