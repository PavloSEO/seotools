"""Structured on-page SEO parser.

Fetches a URL with browser-compatible request headers (httpx follows redirects and
transparently decodes gzip/deflate/br), then extracts the on-page SEO
signals a specialist cares about: title, meta description, canonical,
robots, Open Graph / Twitter tags, the H1..H6 heading outline, JSON-LD
blocks, links (with raw and resolved href, rel / target / nofollow / external),
forms (method, action, whether a password field is present), and the collapsed
visible body text with a word count. Word count is scoped to a configurable
content area (see ``content_area.py``) so navigation and footer boilerplate
does not inflate it; link discovery always covers the whole document.

BeautifulSoup (``features="lxml"``) provides robust HTML parsing. Relative URLs
(links, canonical) are resolved against the
*final* URL after redirects. Any fetch/parse failure is reported as a
plain ``{"url", "ok": False, "error"}`` dict rather than raising.

Public API:
    parse_url(url, options=None) -> dict
"""

from __future__ import annotations

import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from seohead.models import (
    DocumentPosition,
    FormInfo,
    LinkInfo,
    ParsedPage,
    ParseFailed,
    ParseFetched,
    ParseResult,
)
from seohead.recon.net import http_client
from seohead.tools.content_area import TEXT_EXCLUDED_TAGS, extract_area_text, resolve_content_area

# Browser-like User-Agent: without it, bot protection (Cloudflare et al.)
# tends to serve a challenge/block page instead of the real document.
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Accept-Encoding is intentionally omitted here: httpx sets it itself and
# transparently decompresses gzip/deflate/br. The rest mirror a real
# navigation request (identity/no headers = an obvious bot signature).
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Which extractions run by default. Each may be switched off via options.
_OPTION_KEYS = (
    "meta",
    "canonical",
    "og",
    "headings",
    "jsonld",
    "links",
    "forms",
    "text",
    "url_sources",
    "classify_links",
)

# Options that default to False rather than True: each adds cost (a resolved
# content root, a per-link ancestor walk) that most callers of parse_html
# never need.
_DEFAULT_OFF_OPTIONS = ("url_sources", "classify_links")

# URL-bearing attributes beyond a[href]: media, forms, citations, ping,
# meta-refresh, and itemtype. This covers carriers that a crawler or auditor
# would miss by inspecting only anchor elements.
_URL_SOURCE_ATTRS = {
    "img": ("src", "srcset"),
    "source": ("src", "srcset"),
    "script": ("src",),
    "link": ("href",),
    "iframe": ("src",),
    "embed": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
    "track": ("src",),
    "input": ("src", "formaction"),
    "button": ("formaction",),
    "form": ("action",),
    "a": ("ping",),
    "blockquote": ("cite",),
    "q": ("cite",),
    "del": ("cite",),
    "ins": ("cite",),
    "object": ("data",),
}

DEFAULT_TIMEOUT = 15.0
_MAX_REDIRECTS = 8

# ── PURE HELPERS ──────────────────────────────────────────────────────────────


def collapse_whitespace(text: str | None) -> str:
    """Collapse all runs of whitespace to single spaces and trim.

    Mirrors ``stripTags``'s ``\\s+ -> ' '`` + ``trim`` step. Every call site in
    this module passes text BeautifulSoup's own parser already produced
    (``tag.get_text()`` or ``tag.get(attr)``), and lxml decodes character
    references exactly once while building the tree -- the same number of
    times a browser's tokenizer does. Decoding again here used to be a silent
    no-op on ordinary markup, but on a page whose CMS or import pipeline
    already double-escaped its entities (``&amp;amp;`` -> ``&amp;`` -> ``&``,
    a real, common artifact) it decoded a second time, past what a browser
    tab or a SERP snippet ever renders, and so past what the `TITLE_TOO_LONG`
    / `DESC_TOO_SHORT` length checks should be measuring. There is nothing
    left to decode here; a caller holding raw, unparsed markup would need to
    decode it itself before calling this.
    """
    if not text:
        return ""
    return " ".join(str(text).split())


def is_external(href_abs: str, base_url: str) -> bool:
    """True when ``href_abs`` points to a different host than ``base_url``.

    Hostname comparison is case-insensitive. A URL that cannot be parsed,
    or one lacking a host (e.g. a bare fragment resolved oddly), is treated
    as internal — matching the TS ``abs.startsWith(base)`` intent that a
    same-origin URL is internal.
    """
    try:
        target = urlparse(href_abs).hostname or ""
        base = urlparse(base_url).hostname or ""
    except ValueError:
        return False
    if not target:
        return False
    return target.lower() != base.lower()


def _resolve_options(options: dict[str, Any] | None) -> dict[str, bool]:
    """Normalize the options dict: every flag defaults to True except the opt-in ones."""
    options = options or {}
    return {key: bool(options.get(key, key not in _DEFAULT_OFF_OPTIONS)) for key in _OPTION_KEYS}


# Lighthouse's `charset` audit only looks in the first 1024 bytes of the HTML
# (or the Content-Type header, checked separately in rules.py from the
# response we already have). See https://developer.chrome.com/docs/lighthouse/best-practices/charset/
_CHARSET_WINDOW_CHARS = 1024
_CHARSET_META_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_CHARSET_VALUE_RE = re.compile(r'charset\s*=\s*["\']?([a-zA-Z0-9_\-:.()]{2,})', re.IGNORECASE)
# Matches one HTML attribute (name + quoted/unquoted value) inside a tag's raw text,
# used to inspect a candidate <meta> tag's actual attributes rather than its raw
# substring -- see document_charset()'s use below.
_ATTR_RE = re.compile(
    r"""([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))"""
)
# A doctype declaration is only meaningful at the top of the document; scanning
# the whole body would risk matching an escaped/quoted example elsewhere.
_DOCTYPE_WINDOW_CHARS = 2048
_DOCTYPE_RE = re.compile(r"<!DOCTYPE\b[^>]*>", re.IGNORECASE | re.DOTALL)


def document_charset(html: str) -> str | None:
    """The charset named by an early ``<meta charset>``/``http-equiv`` tag, if any.

    Lighthouse's ``charset`` audit (see module docstring above) also accepts a
    charset on the ``Content-Type`` response header; that half is checked
    against the header seohead already captured, in
    ``seohead.sf.core.rules.check_charset``, not here.
    """
    window = html[:_CHARSET_WINDOW_CHARS]
    for tag_match in _CHARSET_META_RE.finditer(window):
        tag_text = tag_match.group(0)
        attrs: dict[str, str] = {}
        for attr_match in _ATTR_RE.finditer(tag_text):
            name = attr_match.group(1).lower()
            value = next(g for g in attr_match.groups()[1:] if g is not None)
            attrs[name] = value
        if attrs.get("charset"):
            return attrs["charset"]
        if attrs.get("http-equiv", "").lower() == "content-type":
            value_match = _CHARSET_VALUE_RE.search(attrs.get("content", ""))
            if value_match:
                return value_match.group(1)
    return None


def document_doctype(html: str) -> str | None:
    """The raw ``<!DOCTYPE ...>`` declaration text, if the document has one."""
    match = _DOCTYPE_RE.search(html[:_DOCTYPE_WINDOW_CHARS])
    return match.group(0).strip() if match else None


def _first_meta_tag(soup: BeautifulSoup, *, name: str) -> Any:
    """Return the first ``<meta name=...>`` tag (case-insensitive), or ``None``.

    Skips ``<template>`` descendants -- see ``_INERT_LINK_CONTAINERS`` -- because a
    <meta> a script has not yet cloned into the document is not live evidence: a
    noindex or description sitting only in a template must not out-rank the real
    one, or invent one where the live document has none.
    """
    for tag in soup.find_all("meta", attrs={"name": _ci(name)}):
        if not _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            return tag
    return None


def _live_meta_tag_count(soup: BeautifulSoup, *, name: str) -> int:
    """Count every live ``<meta name=...>`` tag with a non-empty ``content`` (#385).

    ``_first_meta_tag``/``_meta_content`` intentionally keep reading only the first
    occurrence -- that is the value every existing consumer (``DESC_MISSING``,
    ``DESC_DUPLICATE``, the length checks) has always measured, and changing which
    string those measure was explicitly out of scope for adding this count. A tag with
    a blank ``content`` is not a second declaration a search engine would read either
    way, so it is excluded the same way ``_meta_content`` treats a blank as absent.
    """
    count = 0
    for tag in soup.find_all("meta", attrs={"name": _ci(name)}):
        if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            continue
        content = tag.get("content")
        if isinstance(content, str) and content.strip():
            count += 1
    return count


def _meta_content(soup: BeautifulSoup, *, name: str) -> str | None:
    """Return the ``content`` of ``<meta name=...>`` (case-insensitive)."""
    tag = _first_meta_tag(soup, name=name)
    # "content" is not one of BeautifulSoup's multi-valued attributes, so this
    # is always a plain string at runtime; the stub types it broadly because
    # .get() is generic across every attribute.
    content = cast("str | None", tag.get("content")) if tag else None
    if content is not None:
        return collapse_whitespace(content)
    return None


# <title> also exists in the SVG and MathML vocabularies, where it is an
# accessible name for a graphic, not the title of the document. An inline icon
# therefore places a <title> before the real one — often before any <title> at
# all — and BeautifulSoup's ``soup.title`` returns the first in document order.
_FOREIGN_CONTENT = ("svg", "math")

# <template> content is a DocumentFragment per the HTML spec: it never joins
# the rendered tree and nothing inside it -- an <a href>, an <img src>, a
# <meta>, a <link rel=canonical>, a JSON-LD <script>, a <form> -- is ever
# requested, indexed, or submitted by a browser or a search engine's crawler
# unless a script explicitly clones the fragment in. That makes it the one
# element every reader of the document -- not just link/URL-source
# extraction -- must agree to skip, so it is kept here as the single shared
# answer rather than one list per function that can silently drift (which is
# exactly how issue #140 happened: text extraction excluded it, link and
# URL-source extraction did not; #236 found the same drift again in the
# meta/canonical/OG/JSON-LD/form readers, which had never been taught this
# constant existed). Every one of those readers now filters through this
# same tuple, so a future inert container only needs to be added once.
#
# <noscript> is deliberately NOT in this set, unlike in ``_extract_text``
# below. Its content is real, spec-defined markup that a JS-disabled client,
# and search engines' initial non-rendering crawl of the raw HTML, do load --
# it is the standard place to put a plain <img>/<a> fallback for a
# lazy-loaded resource, which Google's own guidance recommends specifically
# so the resource stays discoverable. Excluding it from link/URL-source
# discovery would hide a genuinely fetchable URL (and any real 404 behind it)
# from the auditor -- the same false-negative failure mode #138 was about,
# just relocated. It stays excluded from *text* only, because JS-enabled
# rendering (what a human visitor and Search Console's rendered view show)
# never displays it as body copy.
_INERT_LINK_CONTAINERS = ("template",)


def _has_ancestor(tag: Any, names: tuple[str, ...]) -> bool:
    """True when ``tag`` itself, or any ancestor, is named in ``names``."""
    return tag.name in names or any(parent.name in names for parent in tag.parents)


def is_inert_template_content(tag: Any) -> bool:
    """True when ``tag`` is itself, or sits inside, a ``<template>`` (see
    ``_INERT_LINK_CONTAINERS``).

    A public wrapper so a module outside this file (e.g. asset-weight resource
    discovery) can apply the same document-fragment exclusion this module uses
    internally, without importing a private name or re-deriving its own list.
    """
    return _has_ancestor(tag, _INERT_LINK_CONTAINERS)


def _title_tag(soup: BeautifulSoup) -> Any:
    """Return the tag ``document_title`` would read, ignoring SVG/MathML ``<title>``."""
    for tag in soup.find_all("title"):
        if _has_ancestor(tag, _FOREIGN_CONTENT):
            continue
        return tag
    return None


def document_title(soup: BeautifulSoup) -> str | None:
    """Return the HTML document title, ignoring SVG/MathML ``<title>``."""
    tag = _title_tag(soup)
    return (collapse_whitespace(tag.get_text()) or None) if tag else None


def _canonical_tag(soup: BeautifulSoup) -> Any:
    """Return the first real ``<link rel=canonical>``, or ``None``.

    Skips ``<template>`` descendants -- see ``_INERT_LINK_CONTAINERS`` -- so a
    canonical only a script could clone into the page can never redirect the
    audit away from the live document's own URL.
    """
    for tag in soup.find_all("link", attrs={"rel": _rel_has("canonical")}):
        if not _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            return tag
    return None


# A robots directive is addressed to a named crawler; ``robots`` addresses all
# of them. Google reads the union of the generic tag and the ones naming it, so
# a page can be noindex without the word appearing in <meta name="robots">.
ROBOTS_META_NAMES = (
    "robots",
    "googlebot",
    "googlebot-news",
    "bingbot",
    "msnbot",
    "yandex",
    "slurp",
)

# The crawlers Google actually runs. A directive named for any other crawler in
# ROBOTS_META_NAMES is scoped to that crawler alone and must never feed a
# Google-effective indexability verdict, however its content reads.
GOOGLE_ROBOTS_AGENTS = frozenset({"googlebot", "googlebot-news"})

# Directives that carry a value after a colon, so the colon is not a
# user-agent prefix.
_VALUED_DIRECTIVES = (
    "max-snippet",
    "max-image-preview",
    "max-video-preview",
    "unavailable_after",
)


def _robots_directive_tags(soup: BeautifulSoup) -> list[Any]:
    """Return every non-empty robots-directive ``<meta>`` tag, in document order.

    Skips ``<template>`` descendants -- see ``_INERT_LINK_CONTAINERS`` -- a noindex
    only a script could clone into the page has never been read by a crawler.
    """
    out: list[Any] = []
    for tag in soup.find_all("meta"):
        if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            continue
        name = tag.get("name")
        if not isinstance(name, str) or name.lower() not in ROBOTS_META_NAMES:
            continue
        content = tag.get("content")
        if isinstance(content, str) and content.strip():
            out.append(tag)
    return out


def robots_meta_entries(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Return ``(name, content)`` for every robots-directive meta, in document order.

    The name is what scopes the directive. Once ``robots_meta_values`` below
    joins the content strings together, that scope is unrecoverable — this is
    the entry point every caller that needs to honour it (see
    ``robots_meta_scoped``) must read from instead.
    """
    return [
        (cast(str, tag.get("name")).strip().lower(), collapse_whitespace(tag.get("content")))
        for tag in _robots_directive_tags(soup)
    ]


def robots_meta_values(soup: BeautifulSoup) -> list[str]:
    """Return the ``content`` of every robots-directive meta, in document order."""
    return [content for _, content in robots_meta_entries(soup)]


def robots_meta_scoped(soup: BeautifulSoup) -> list[str]:
    """Return every robots-directive meta's content, agent scope preserved.

    The generic ``robots`` name is left bare (it already addresses everyone);
    any other name is prefixed ``"<name>: "``, the same convention an
    ``X-Robots-Tag`` header uses to address one crawler. ``robots_directives``
    reads that convention on both, so a Bingbot- or Yandex-only tag survives
    as evidence without being mistaken for a global directive downstream.
    """
    return [
        content if name == "robots" else f"{name}: {content}"
        for name, content in robots_meta_entries(soup)
    ]


def _hreflang_tags(soup: BeautifulSoup) -> list[Any]:
    """Return every ``<link rel="alternate" hreflang="...">`` tag, in document order."""
    out: list[Any] = []
    for tag in soup.find_all("link"):
        hreflang = tag.get("hreflang")
        if not hreflang:
            continue
        rel_attr: str | list[str] = tag.get("rel") or []
        rel_tokens = rel_attr.split() if isinstance(rel_attr, str) else list(rel_attr)
        if any(isinstance(t, str) and t.lower() == "alternate" for t in rel_tokens):
            out.append(tag)
    return out


def meta_refresh_content(soup: BeautifulSoup) -> str:
    """The first ``<meta http-equiv="refresh">``'s content attribute, as written.

    ``extract_url_sources`` already finds this tag, but only to resolve the URL
    inside it for link discovery -- the declaration itself was never surfaced, so
    META_REFRESH_REDIRECT could read it from a Screaming Frog export and never
    from a native crawl.

    The raw content is kept rather than the parsed target, because that is what
    SF's *Meta Refresh 1* column carries and the audit has to reach the same
    verdict whichever source produced the evidence. A refresh inside a
    ``<template>`` is inert and is not a declaration, the same rule
    ``extract_url_sources`` applies a few lines below.
    """
    for meta in soup.find_all("meta"):
        if _has_ancestor(meta, _INERT_LINK_CONTAINERS):
            continue
        equiv = meta.get("http-equiv") or ""
        if isinstance(equiv, list):
            equiv = " ".join(equiv)
        if equiv.lower().strip() == "refresh":
            return collapse_whitespace(cast("str | None", meta.get("content")) or "")
    return ""


def robots_directives(*values: str | None) -> set[str]:
    """Split robots directive strings into lowercase tokens, Google-effective only.

    Handles the two forms that defeat a substring search: ``none``, which is
    shorthand for ``noindex, nofollow``, and the ``<user-agent>: <directive>``
    prefix that an ``X-Robots-Tag`` header (or ``robots_meta_scoped``) may
    carry. A directive prefixed for a crawler other than Google is scoped to
    that crawler alone and is dropped here rather than folded into the result
    — a Bingbot- or Yandex-only ``noindex`` must never read as a global one.
    """
    tokens: set[str] = set()
    for value in values:
        for raw in str(value or "").replace(";", ",").split(","):
            token = raw.strip().lower()
            if ":" in token and not token.startswith(_VALUED_DIRECTIVES):
                agent, _, rest = token.partition(":")
                if agent.strip() not in GOOGLE_ROBOTS_AGENTS:
                    continue
                token = rest.strip()
            if not token:
                continue
            tokens.add(token)
            if token == "none":
                tokens.update(("noindex", "nofollow"))
    return tokens


def _ci(value: str) -> Callable[[Any], bool]:
    """A case-insensitive attribute matcher for BeautifulSoup ``find``."""
    target = value.lower()
    return lambda v: isinstance(v, str) and v.lower() == target


def _extract_headings(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Return ``{"h1": [...], ..., "h6": [...]}`` for headings that have text."""
    headings: dict[str, list[str]] = {}
    for level in range(1, 7):
        found: list[str] = []
        for tag in soup.find_all(f"h{level}"):
            text = collapse_whitespace(tag.get_text(" "))
            if text:
                found.append(text)
        if found:
            headings[f"h{level}"] = found
    return headings


def h1_alt_only_text(soup: BeautifulSoup) -> str | None:
    """The alt text of an H1 whose own text is empty, when an image supplies it (#385).

    ``_extract_headings`` above already treats an H1 with no text of its own as absent —
    ``tag.get_text()`` never reads an ``alt`` attribute, matching how a search engine's
    text-based reading of the page sees it. This is a separate, additive fact for the same
    tag: whether the reason it read as empty is that the H1 contains nothing but an
    alt-bearing image. A logo image *beside real heading text* — ``<h1><img alt="Logo">
    Actual Heading</h1>`` — is normal and must not fire: that H1 already has text of its
    own, and ``own_text`` short-circuits before the image is ever inspected. Only the
    first qualifying H1 in document order is returned, matching how ``h1``/``h1_2`` track
    only the first two headings elsewhere in this module.
    """
    for tag in soup.find_all("h1"):
        if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            continue
        own_text = collapse_whitespace(tag.get_text(" "))
        if own_text:
            continue  # has real text of its own -- an image inside it is incidental
        for img in tag.find_all("img"):
            if _has_ancestor(img, _INERT_LINK_CONTAINERS):
                continue
            alt = collapse_whitespace(cast("str | None", img.get("alt")) or "")
            if alt:
                return alt
    return None


def extract_images(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Every ``<img>`` element's alt-attribute evidence (#386).

    Two facts per image, kept separate rather than collapsed into one "no alt text"
    boolean: ``has_alt`` is ``True`` iff the ``alt`` attribute is written at all --
    ``alt=""`` counts, because an empty alt is the correct way to mark a decorative
    image and must never read the same as the attribute being absent outright. ``alt_length``
    is the collapsed alt string's length when the attribute is present (0 for both a missing
    attribute and a genuinely empty one). Scoped to ``<img>`` only: a CSS background image
    carries no ``alt`` concept to evaluate.
    """
    images: list[dict[str, Any]] = []
    for tag in soup.find_all("img"):
        if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            continue
        has_alt = "alt" in tag.attrs
        alt_value = collapse_whitespace(cast("str | None", tag.get("alt")) or "") if has_alt else ""
        images.append({"has_alt": has_alt, "alt_length": len(alt_value)})
    return images


# Deprecated, plugin-dependent embedding elements. <object>/<embed> also carry
# entirely ordinary, non-plugin uses today -- an inline SVG or PDF fallback via
# <object type="image/..."> renders natively in every browser and a mobile
# device handles it exactly as well as desktop -- so only the tags whose type
# does not declare an image are counted (#385). <applet> has no legitimate
# non-plugin use left; it is always counted.
_PLUGIN_TAGS = ("object", "embed", "applet")


def unsupported_plugin_count(soup: BeautifulSoup) -> int:
    """Count of legacy plugin-dependent elements (Flash, Java, Silverlight, ...) on the page.

    Mobile browsers, and modern desktop ones, do not run plugins at all, so content behind
    one of these elements is simply invisible there -- unlike an ``<iframe>`` or a native
    ``<video>``, which every browser still renders.
    """
    count = 0
    for name in _PLUGIN_TAGS:
        for tag in soup.find_all(name):
            if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
                continue
            if name == "object":
                type_attr = (cast("str | None", tag.get("type")) or "").strip().lower()
                if type_attr.startswith("image/"):
                    continue  # an SVG/raster fallback, not plugin content
            count += 1
    return count


# The canonical placeholder passage (Cicero's "de Finibus", corrupted into English filler
# since the 1500s). Matched as a phrase, not a single common word, so an incidental mention
# of "lorem" or "ipsum" alone -- a product named Lorem, a Latin-teaching page discussing the
# real source text -- cannot trip it.
_LOREM_IPSUM_RE = re.compile(r"lorem\s+ipsum\s+dolor\s+sit\s+amet", re.IGNORECASE)


def count_lorem_ipsum(content_text: str) -> int:
    """Occurrences of the Lorem Ipsum placeholder passage within scoped content text (#385).

    Counted against ``content_text`` -- already scoped to the resolved content area, nav and
    footer boilerplate excluded -- rather than the whole raw document, and matched as a full
    multi-word phrase rather than any substring. Either restriction alone was enough per the
    issue's own acceptance criteria; both together keep a page that merely *mentions* the
    passage once in a sidebar demo, or in an unrelated typography callout outside the content
    area, from reading the same as one that shipped it as real body copy.
    """
    return len(_LOREM_IPSUM_RE.findall(content_text or ""))


# How much of a broken block to quote back, so the reader can find it in the
# page without the report carrying the whole thing.
_JSONLD_EXCERPT_CHARS = 200


def _extract_jsonld(soup: BeautifulSoup) -> tuple[list[Any], list[dict[str, Any]]]:
    """Parse every ``<script type="application/ld+json">`` block.

    Returns the blocks that parsed and a record of those that did not. Dropping
    the failures silently makes a page whose markup is broken indistinguishable
    from a page with no markup — the opposite conclusion, and the more common
    one: a single stray comment voids an entire @graph.

    Skips ``<template>`` descendants -- see ``_INERT_LINK_CONTAINERS`` -- a graph
    only a script could clone into the page is not markup any crawler ever reads,
    valid or not, so it is excluded before indexing rather than flagged invalid.
    """
    import json

    out: list[Any] = []
    invalid: list[dict[str, Any]] = []
    live_blocks = [
        tag
        for tag in soup.find_all("script", attrs={"type": _ci("application/ld+json")})
        if not _has_ancestor(tag, _INERT_LINK_CONTAINERS)
    ]
    for index, tag in enumerate(live_blocks, 1):
        raw = tag.string or tag.get_text()
        text = (raw or "").strip()
        if not text:
            invalid.append({"index": index, "error": "block is empty", "excerpt": ""})
            continue
        try:
            out.append(json.loads(text))
        except (ValueError, TypeError) as exc:
            invalid.append(
                {
                    "index": index,
                    "error": str(exc),
                    "excerpt": text[:_JSONLD_EXCERPT_CHARS],
                }
            )
    return out, invalid


class _LiveBaseHrefScanner(HTMLParser):
    """Finds the ``href`` of the first live ``<base>`` in raw HTML (issue #359).

    ``<template>`` content is an inert ``DocumentFragment`` (see
    ``_INERT_LINK_CONTAINERS``): a ``<base>`` written only inside one is never
    consulted by a browser, so it must never win here either. Depth-tracking
    with the stdlib tokenizer, mirroring ``_HeadElementScanner``, is what lets
    this stay a plain string scan (the raw-HTML branch's entire point) while
    still giving templated and non-templated bases the same tree-level
    selection semantics a resolved ``BeautifulSoup`` tree gets below.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True, scripting=True)
        self.href: str | None = None
        self._template_depth = 0

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "template":
            self._template_depth += 1
            return
        if self.href is not None or self._template_depth > 0 or tag != "base":
            return
        for name, value in attrs:
            if name.lower() == "href" and value and value.strip():
                self.href = value.strip()
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "template" and self._template_depth > 0:
            self._template_depth -= 1


def document_base_url(document: BeautifulSoup | str, final_url: str) -> str:
    """Return the document base URL for resolving relative links.

    Per the HTML standard relative URLs resolve against the ``href`` of the
    **first live** ``<base>`` element that carries one — itself resolved
    against the document URL — and against the document URL when there is no
    such element. "Live" excludes a ``<base>`` sitting inside a ``<template>``
    (see ``_INERT_LINK_CONTAINERS``): that content is an inert
    ``DocumentFragment`` a browser never consults, so a template-only base
    must never be chosen over a real one, or over the ``final_url`` fallback
    when no real one exists (issue #359).

    Ignoring this reports links that do not exist: on a page whose base is
    ``https://example.com/`` a relative ``catalog/`` resolves to
    ``https://example.com/catalog/``, not to a path under the current
    directory. Sites that ship a ``<base>`` tag (MODX and older CMS themes do)
    otherwise produce a flood of phantom broken links that a browser and a
    search engine crawler both fetch with a 200.

    ``document`` accepts parsed markup or raw HTML; the raw form exists for
    callers that deliberately avoid the cost of building a tree, and both
    forms apply the same live-base selection.
    """
    href = ""
    if isinstance(document, str):
        scanner = _LiveBaseHrefScanner()
        try:
            scanner.feed(document)
            scanner.close()
        except Exception:  # best-effort scan over untrusted markup
            pass
        href = scanner.href or ""
    else:
        for tag in document.find_all("base"):
            if is_inert_template_content(tag):
                continue
            # "href" is single-valued, so this is always a plain string.
            candidate = (cast("str | None", tag.get("href")) or "").strip()
            if candidate:
                href = candidate
                break
    if not href:
        return final_url
    try:
        return urljoin(final_url, href)
    except ValueError:
        return final_url


# issue #123: a browser closes <head> at the first element that does not belong
# there, so a canonical or robots directive placed after one silently stops
# applying — from the source it looks fine, and every check that reads a tag
# wherever it finds it agrees. These helpers read where the parser itself
# resolved each element, which is the only view that matches what a browser
# (and Google, which follows the same HTML5 parsing algorithm) actually acts
# on. Verified directly against lxml's own recovery behaviour rather than
# assumed: a body-only element inside <head> (a bare <div>, <p>, <img>, ...)
# closes <head> there and moves it and everything after into <body>, while an
# element the head content model actually allows (title/base/link/meta/style/
# script/noscript/template, and comments) does not. That also means no
# *resolved* tree can ever show an invalid element still sitting inside
# <head> — by the time parsing recovers, it has already been moved out — so
# "invalid elements in head" is read from the head span as written in the
# source instead (see invalid_head_elements).
def _in_head(tag: Any) -> bool:
    """True when ``tag``'s ancestor chain includes a ``<head>`` element."""
    return any(getattr(parent, "name", None) == "head" for parent in tag.parents)


def _head_not_first(html_tag: Any, head_count: int) -> bool:
    """True when some element under ``<html>`` precedes its ``<head>``.

    Covers both "<head> Not First In <html> Element" and "<body> Element
    Preceding <html>" from the Screaming Frog catalogue: once lxml recovers
    from either shape of malformed markup, both collapse into one resolved
    tree where something other than <head> is the first element child of the
    single <html> root — there is no separate signal left to tell them apart.
    """
    if html_tag is None or head_count == 0:
        return False
    for child in html_tag.children:
        name: str | None = getattr(child, "name", None)
        if name is None:  # text/comment/doctype between the tags
            continue
        return name != "head"
    return False


# Elements the HTML head content model allows. A bare block-level element
# (div/p/img/...) verified directly against lxml forces it to close <head>
# early and move everything after into <body> — which is exactly the "outside
# <head>" symptom these checks exist to catch — so this whitelist is also,
# in effect, what a resolved tree could never still contain inside <head>.
_ALLOWED_HEAD_TAGS = frozenset(
    {"title", "base", "link", "meta", "style", "script", "noscript", "template"}
)


class _HeadElementScanner(HTMLParser):
    """Collects tokenizer-visible start tags written inside a document's <head>.

    Built on the stdlib tokenizer instead of a raw opening-tag regex so that
    text which merely *looks* like a tag never counts as one (issue #267):
    ``script``/``style`` are CDATA content, ``title`` is RCDATA content (both
    handled by :class:`HTMLParser` itself), ``noscript`` is opaque with
    ``scripting=True`` — matching a browser with JS enabled, the only case
    that matters for what a crawler sees — and comments and quoted attribute
    values are simply outside the tokenizer's tag-name grammar. ``<template>``
    content is a separate, inert document fragment per the HTML content
    model, so tags found while inside one are counted only for nesting depth,
    never as findings.

    Scans only the first ``<head>...</head>`` (or up to the first literal
    ``<body>``, matching how a browser also promotes an invalid head element
    and everything after it into <body>) — later ``<head>`` tags, if any, are
    ignored, matching the previous implementation's single-span behaviour.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True, scripting=True)
        self.found: list[str] = []
        self._seen: set[str] = set()
        self._in_head = False
        self._done = False
        self._template_depth = 0

    def _start(self, tag: str) -> None:
        tag = tag.lower()
        if self._done:
            return
        if not self._in_head:
            if tag == "head":
                self._in_head = True
            return
        if self._template_depth == 0 and tag == "body":
            self._in_head = False
            self._done = True
            return
        if tag == "template":
            self._template_depth += 1
        if self._template_depth == 0 and tag not in _ALLOWED_HEAD_TAGS and tag not in self._seen:
            self._seen.add(tag)
            self.found.append(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._done or not self._in_head:
            return
        if tag == "template" and self._template_depth > 0:
            self._template_depth -= 1
        elif self._template_depth == 0 and tag == "head":
            self._in_head = False
            self._done = True


def invalid_head_elements(html: str) -> list[str]:
    """Tag names written inside ``<head>...</head>`` that do not belong there.

    Read with the stdlib tokenizer rather than the resolved tree: an invalid
    element is exactly what makes the parser close <head> early, so by the
    time parsing finishes recovering, the resolved <head> can no longer
    contain it (see the block comment above). Malformed markup that trips up
    the tokenizer degrades to whatever was found before the failure, rather
    than raising, since this is a best-effort textual scan.
    """
    scanner = _HeadElementScanner()
    try:
        scanner.feed(html)
        scanner.close()
    except Exception:  # best-effort scan over untrusted markup
        pass
    return scanner.found


def document_position(soup: BeautifulSoup, html: str) -> DocumentPosition:
    """Where key elements sit relative to ``<head>``, as the parser resolved the tree.

    Each ``*_outside_head`` flag is ``None`` when the element is simply absent
    (a different, already-covered finding) and a bool only when it exists —
    ``True`` iff every instance the parser found sits outside ``<head>``.
    """
    head_tags = soup.find_all("head")
    body_tags = soup.find_all("body")
    title_tag = _title_tag(soup)
    desc_tag = _first_meta_tag(soup, name="description")
    canonical_tag = _canonical_tag(soup)
    robots_tags = _robots_directive_tags(soup)
    hreflang_tags = _hreflang_tags(soup)
    return {
        "head_count": len(head_tags),
        "body_count": len(body_tags),
        "head_not_first": _head_not_first(soup.find("html"), len(head_tags)),
        "invalid_head_elements": invalid_head_elements(html),
        "title_outside_head": (not _in_head(title_tag)) if title_tag else None,
        "meta_description_outside_head": (not _in_head(desc_tag)) if desc_tag else None,
        "canonical_outside_head": (not _in_head(canonical_tag)) if canonical_tag else None,
        "directives_outside_head": (
            all(not _in_head(tag) for tag in robots_tags) if robots_tags else None
        ),
        "hreflang_outside_head": (
            all(not _in_head(tag) for tag in hreflang_tags) if hreflang_tags else None
        ),
    }


def extract_hreflang(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    """Every hreflang alternate the document declares, in document order (#357).

    ``_hreflang_tags`` has always found these; ``parse_html`` used them for one
    boolean about *where the tags sat* and discarded *what they said*, so a
    crawl could not answer whether a page was localised -- the only
    authoritative statement about that being the one the site makes here.

    ``lang`` and ``raw_href`` are kept exactly as written. A code with the wrong
    case or a malformed region is itself a finding, and normalising on capture
    would hide it. ``url`` is the same href resolved against the document base,
    which is what a browser does, not a normalisation of the declaration -- both
    forms are kept so a check can compare targets without losing the original.
    """
    alternates: list[dict[str, str]] = []
    for tag in _hreflang_tags(soup):
        if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            continue  # a <template>'s alternate is never in the rendered document
        lang = cast("str | None", tag.get("hreflang")) or ""
        raw_href = (cast("str | None", tag.get("href")) or "").strip()
        alternates.append(
            {
                "lang": lang.strip(),
                "raw_href": raw_href,
                # An alternate with no href declares a language and points
                # nowhere. Recorded as such rather than dropped: it is the
                # malformed declaration a reciprocity check needs to see.
                "url": urljoin(base_url, raw_href) if raw_href else "",
            }
        )
    return alternates


def _link_cap(options: dict[str, Any] | None, key: str) -> int | None:
    """Read an opt-in per-document observation cap without changing defaults."""
    value = (options or {}).get(key)
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{key} must be a non-negative integer or null")
    return value


def _link_context(
    soup: BeautifulSoup,
    *,
    classify_links: bool,
    content_area_config: dict[str, Any] | None,
    position_rules: Any,
) -> tuple[Any, Any]:
    if not classify_links:
        return None, None
    from seohead.tools.content_area import find_content_root
    from seohead.tools.link_position import rules_from_config

    content_root, _ = find_content_root(soup, content_area_config)
    return content_root, rules_from_config(position_rules)


def _link_target(tag: Any, base_url: str, final_url: str) -> tuple[str, str, bool] | None:
    """Return the inexpensive facts needed to count a valid anchor observation."""
    if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
        return None
    href_raw = (cast("str | None", tag.get("href")) or "").strip()
    if not href_raw:
        return None
    if href_raw.startswith("#"):
        return None
    # A scheme other than http(s) -- mailto:, tel:, javascript:, and the same
    # family of non-fetchable contact/deep-link schemes (sms:, skype:,
    # whatsapp:, viber:, facetime:, intent:, market:, ...) -- is never a page
    # a crawler can fetch, so it is excluded the same way mailto/tel already
    # were rather than left to leak in as an ordinary link (#471). A relative
    # or protocol-relative href has no scheme here and is unaffected.
    try:
        scheme = urlparse(href_raw).scheme.lower()
    except ValueError:
        return None
    if scheme and scheme not in ("http", "https"):
        return None
    try:
        href = urljoin(base_url, href_raw)
    except ValueError:
        return None
    return href, href_raw, is_external(href, final_url)


def _link_info(
    tag: Any,
    href: str,
    href_raw: str,
    external: bool,
    *,
    classify_links: bool,
    content_root: Any,
    rules: Any,
) -> LinkInfo:
    rel_attr: str | list[str] = tag.get("rel") or []
    rel_tokens = rel_attr.split() if isinstance(rel_attr, str) else list(rel_attr)
    rel_tokens = [token.lower() for token in rel_tokens]
    entry: LinkInfo = {
        "href": href,
        "raw_href": href_raw,
        "text": collapse_whitespace(tag.get_text(" ")),
        "rel": " ".join(rel_tokens),
        "target": (cast("str | None", tag.get("target")) or "").strip(),
        "nofollow": "nofollow" in rel_tokens,
        "external": external,
    }
    if classify_links:
        from seohead.tools.link_position import classify_link

        entry["position"] = classify_link(tag, content_root, rules=rules)
    return entry


def _extract_link_observations(
    soup: BeautifulSoup,
    base_url: str,
    final_url: str,
    *,
    classify_links: bool = False,
    content_area_config: dict[str, Any] | None = None,
    position_rules: Any = None,
    cap: int | None = None,
) -> tuple[list[LinkInfo], dict[str, int] | None]:
    """Collect ``<a href>`` links resolved against ``base_url``.

    ``base_url`` resolves the hrefs; ``final_url`` decides what counts as
    external, because "external" means a host other than the page's own —
    a ``<base>`` pointing elsewhere must not reclassify the whole page.

    Skips empty hrefs, pure-fragment (``#...``) links, and any href whose
    scheme is not ``http``/``https`` -- ``javascript:``, ``mailto:``, ``tel:``,
    and the same family of non-fetchable contact/deep-link schemes (``sms:``,
    ``skype:``, ``whatsapp:``, etc., see ``_link_target``) -- plus any ``<a>``
    inside a ``<template>``
    (see ``_INERT_LINK_CONTAINERS`` for why ``<noscript>`` is not skipped
    too). Each entry carries the resolved absolute href, the href exactly as
    written (``raw_href`` — the only place a protocol-relative ``//host/path``
    form is still visible once resolution has run), anchor text, rel tokens,
    the ``target`` attribute, a ``nofollow`` flag, and an ``external`` flag.

    ``classify_links`` additionally resolves each link's ``position`` (nav,
    header, sidebar, footer, content, other; see ``link_position.py``). It is
    off by default: computing an ancestor-path classification for every link
    on every page has a real per-link cost, and most callers never read it.
    When off, links carry no ``position`` key at all, which is what makes the
    absence visible rather than a silently empty string.
    """
    links: list[LinkInfo] = []
    total = 0
    external_total = 0
    content_root, rules = _link_context(
        soup,
        classify_links=classify_links and cap != 0,
        content_area_config=content_area_config,
        position_rules=position_rules,
    )
    for tag in soup.find_all("a"):
        target = _link_target(tag, base_url, final_url)
        if target is None:
            continue
        href, href_raw, external = target
        total += 1
        external_total += int(external)
        if cap is None or len(links) < cap:
            links.append(
                _link_info(
                    tag,
                    href,
                    href_raw,
                    external,
                    classify_links=classify_links,
                    content_root=content_root,
                    rules=rules,
                )
            )
    if cap is None:
        return links, None
    return links, {
        "stored": len(links),
        "total": total,
        "external_total": external_total,
        "omitted": total - len(links),
    }


def _extract_links(
    soup: BeautifulSoup,
    base_url: str,
    final_url: str,
    *,
    classify_links: bool = False,
    content_area_config: dict[str, Any] | None = None,
    position_rules: Any = None,
) -> list[LinkInfo]:
    links, _ = _extract_link_observations(
        soup,
        base_url,
        final_url,
        classify_links=classify_links,
        content_area_config=content_area_config,
        position_rules=position_rules,
    )
    return links


def _form_info(tag: Any, base_url: str, final_url: str) -> FormInfo:
    method = (cast("str | None", tag.get("method")) or "get").strip().lower()
    action_raw = (cast("str | None", tag.get("action")) or "").strip()
    try:
        action = urljoin(base_url, action_raw) if action_raw else final_url
    except ValueError:
        action = final_url
    return {
        "method": method,
        "action": action,
        "has_password": tag.find("input", attrs={"type": _ci("password")}) is not None,
    }


def _extract_form_observations(
    soup: BeautifulSoup, base_url: str, final_url: str, *, cap: int | None = None
) -> tuple[list[FormInfo], int]:
    """Collect ``<form>`` elements: method, resolved action, and whether a password field
    is present (issue #125 — a form is otherwise invisible to every check downstream).

    An absent or empty ``action`` submits to the document's own URL per the HTML standard,
    so it resolves to ``final_url`` rather than being left blank or wrongly following
    ``base_url`` (a ``<base>`` tag changes where a *relative* action points, not what an
    omitted one means).

    Skips ``<template>`` descendants -- see ``_INERT_LINK_CONTAINERS`` -- a form only a
    script could clone into the page is never submitted, so it must not raise an
    insecure-action or password-over-HTTP finding on a target nothing ever posts to.
    """
    forms: list[FormInfo] = []
    total = 0
    for tag in soup.find_all("form"):
        if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            continue  # a <template>-only form is never submitted, see _INERT_LINK_CONTAINERS
        total += 1
        if cap is None or len(forms) < cap:
            forms.append(_form_info(tag, base_url, final_url))
    return forms, total - len(forms)


def _extract_forms(soup: BeautifulSoup, base_url: str, final_url: str) -> list[FormInfo]:
    forms, _ = _extract_form_observations(soup, base_url, final_url)
    return forms


def _extract_text(soup: BeautifulSoup) -> str:
    """Collapsed visible body text (see ``content_area.TEXT_EXCLUDED_TAGS`` for what is removed)."""
    body = soup.body or soup
    # Work on a copy so we don't mutate the shared tree used by other steps.
    from copy import copy

    body = copy(body)
    for tag in body.find_all(list(TEXT_EXCLUDED_TAGS)):
        tag.decompose()
    return collapse_whitespace(body.get_text(" "))


# srcset entries use ``URL descriptor``; retain the URL before the first space.
_SRCSET_SPLIT = re.compile(r"\s*,\s*(?=(?:[^']*$))")


def _split_srcset(value: str) -> list[str]:
    """Extract one URL per srcset entry, discarding density/width descriptors."""
    urls: list[str] = []
    for entry in _SRCSET_SPLIT.split(value):
        entry = entry.strip()
        if not entry:
            continue
        urls.append(entry.split(None, 1)[0])
    return urls


# CSS ``url(...)`` references, in inline style attributes and <style> blocks.
# A page whose banners and product photos are CSS backgrounds is invisible to
# every image check if only <img> is inspected — and a background image has no
# alt attribute at all, which is itself sometimes the finding.
_CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")]+)\1\s*\)""", re.IGNORECASE)


def extract_css_urls(css_text: str | None) -> list[str]:
    """URLs referenced from CSS text, in source order, duplicates kept.

    Deliberately not limited to ``background-image``: ``border-image``,
    ``list-style-image``, ``mask-image`` and ``content`` all fetch resources the
    same way, and a checker that only knew one property would under-report.
    """
    return [match.group(2).strip() for match in _CSS_URL_RE.finditer(css_text or "")]


def extract_url_sources(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    """Extract URL carriers beyond ``a[href]``.

    Covers media, forms, citations, ping, meta-refresh, and itemtype. Each URL
    records the tag and attribute where it was found. Relative references are
    resolved against ``base_url``. Skips ``<template>`` descendants -- see
    ``_INERT_LINK_CONTAINERS`` for why only ``<template>`` and not
    ``<noscript>``.
    """
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def push(raw_url: str, tag_name: str, attr: str) -> None:
        raw_url = (raw_url or "").strip()
        if not raw_url:
            return
        # Skip embedded data, active schemes, contact links, and bare fragments.
        low = raw_url.lower()
        if raw_url.startswith("#") or low.startswith(("data:", "javascript:", "mailto:", "tel:")):
            return
        try:
            absolute = urljoin(base_url, raw_url)
        except ValueError:
            return
        if absolute not in seen:
            seen.add(absolute)
            out.append({"url": absolute, "tag": tag_name, "attr": attr})

    for tag_name, attrs in _URL_SOURCE_ATTRS.items():
        for tag in soup.find_all(tag_name):
            if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
                continue  # a <template>-only resource is never fetched, see _INERT_LINK_CONTAINERS
            for attr in attrs:
                value = tag.get(attr)
                if not value:
                    continue
                if attr == "srcset":  # one attribute may contain multiple URLs
                    for sub in _split_srcset(value if isinstance(value, str) else " ".join(value)):
                        push(sub, tag_name, attr)
                elif attr == "ping":  # space-separated URL list per the HTML spec
                    for sub in (value if isinstance(value, str) else " ".join(value)).split():
                        push(sub, tag_name, attr)
                else:
                    push(value if isinstance(value, str) else value[0], tag_name, attr)

    # CSS url(...) in inline style attributes and in <style> blocks. External
    # stylesheets are not fetched here: this function parses one document and
    # performs no I/O, so a linked .css is reported as a resource by the <link>
    # rule above and its contents are a crawler concern, not a parser one.
    for tag in soup.find_all(style=True):
        if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            continue
        style_value = tag.get("style")
        if isinstance(style_value, list):
            style_value = " ".join(style_value)
        for url in extract_css_urls(style_value):
            push(url, tag.name, "style")
    for style_tag in soup.find_all("style"):
        if _has_ancestor(style_tag, _INERT_LINK_CONTAINERS):
            continue
        for url in extract_css_urls(style_tag.get_text()):
            push(url, "style", "css")

    # meta http-equiv=refresh content="0;url=..."
    for meta in soup.find_all("meta"):
        if _has_ancestor(meta, _INERT_LINK_CONTAINERS):
            continue
        equiv = meta.get("http-equiv") or ""
        if isinstance(equiv, list):
            equiv = " ".join(equiv)
        if equiv.lower().strip() == "refresh":
            # "content" is single-valued, so this is always a plain string.
            content = cast("str | None", meta.get("content")) or ""
            match = re.search(r"url\s*=\s*['\"]?([^\s'\"]+)", content, re.IGNORECASE)
            if match:
                push(match.group(1), "meta", "refresh")

    # itemtype is a microdata vocabulary URL rather than a resource URL, but it
    # is still a useful carrier for an auditor to inspect.
    for tag in soup.select("[itemtype]"):
        if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            continue
        value = tag.get("itemtype")
        if isinstance(value, list):
            for v in value:
                push(v, tag.name, "itemtype")
        elif isinstance(value, str):
            push(value, tag.name, "itemtype")

    return out


# extract_url_sources() also carries non-image carriers (script src, form
# action, cite, itemtype, ...). "img"/"source" are always an image; any other
# tag only qualifies via its "style" or "css" attr, i.e. a CSS url() -- which
# is how a background-image (no <img>, no alt attribute) is reported at all.
_IMAGE_URL_SOURCE_ATTRS = ("style", "css")


def image_url_sources(url_sources: list[dict[str, str]]) -> list[dict[str, str]]:
    """Filter ``extract_url_sources()`` output down to entries that are images."""
    return [
        s
        for s in url_sources
        if s["tag"] in ("img", "source") or s["attr"] in _IMAGE_URL_SOURCE_ATTRS
    ]


# Written onto each <iframe> just long enough to survive the copy that
# resolve_content_area makes, then removed. The alternative -- re-running the
# resolver and comparing by identity -- cannot work: the resolved root is a
# detached copy with its excluded elements decomposed, so an iframe that the
# content area drops would still look present in the live tree.
_FRAME_MARKER = "data-seohead-frame"


def extract_frames(
    soup: Any, base_url: str, final_url: str, in_content_area: set[str]
) -> list[dict[str, Any]]:
    """Every ``<iframe>`` the document declares, and where it sits (issue #360).

    A framed document is not part of the parent DOM. The parser sees
    ``<iframe src="...">`` and no text, so ``word_count`` measures the shell
    around the content and the page is reported as thin -- naming the wrong
    cause and prescribing the wrong fix. This records what is needed to say the
    true thing instead: what is framed, whether the site owns it, and whether it
    sits where the content is supposed to be.

    ``in_content_area`` holds the markers of the frames that survived into the
    resolved content root, so the answer is the one the ``word_count`` beside it
    was computed from rather than a second, possibly disagreeing, resolution.
    """
    frames: list[dict[str, Any]] = []
    for tag in soup.find_all("iframe"):
        if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
            continue  # a <template>'s iframe is never instantiated, see _INERT_LINK_CONTAINERS
        raw_src = (cast("str | None", tag.get("src")) or "").strip()
        # An empty src is not the absence of a frame: a JavaScript-populated
        # iframe hides text from a parser exactly as a src'd one does.
        src = urljoin(base_url, raw_src) if raw_src else ""
        frames.append(
            {
                "src": src,
                "raw_src": raw_src,
                # No src resolves to the page itself, which is same-origin by
                # definition -- not a third party to be excused as an embed.
                "same_origin": not src or not is_external(src, final_url),
                "in_content_area": str(tag.get(_FRAME_MARKER)) in in_content_area,
                "title": collapse_whitespace(cast("str | None", tag.get("title")) or ""),
                "loading": (cast("str | None", tag.get("loading")) or "").strip().lower(),
                "sandbox": collapse_whitespace(cast("str | None", tag.get("sandbox")) or ""),
            }
        )
    return frames


def parse_html(html: str, final_url: str, options: dict[str, Any] | None = None) -> ParsedPage:
    """Extract SEO data from an HTML string (pure — no network).

    Honors each option flag; skips the corresponding extraction when False.
    ``final_url`` is used to resolve relative links and the canonical URL.
    """
    opts = _resolve_options(options)
    link_cap = _link_cap(options, "max_link_observations")
    form_cap = _link_cap(options, "max_form_observations")
    soup = BeautifulSoup(html, features="lxml")
    # Everything that turns markup into absolute URLs resolves against the
    # document base, not the page URL: see document_base_url.
    base_url = document_base_url(soup, final_url)

    result: dict[str, Any] = {}

    if opts["meta"]:
        result["title"] = document_title(soup)
        result["meta_description"] = _meta_content(soup, name="description")
        # How many live occurrences exist, not which one is authoritative --
        # "meta_description" above stays exactly the first, unchanged (#385).
        result["meta_description_count"] = _live_meta_tag_count(soup, name="description")
        result["robots"] = _meta_content(soup, name="robots")
        # Separate from "robots": that key keeps its literal meaning, this one
        # carries every crawler-addressed tag, which is what indexability needs.
        result["robots_meta"] = robots_meta_values(soup)
        # Same tags, agent scope preserved (see robots_meta_scoped) -- this is
        # what native-crawl evidence joins into "meta_robots", not the line above.
        result["robots_meta_scoped"] = robots_meta_scoped(soup)
        # Static Lighthouse audits (see seohead.sf.core.rules): charset/doctype
        # read the raw markup directly (their rule is positional), viewport
        # reuses the existing meta-content helper.
        result["charset"] = document_charset(html)
        result["doctype"] = document_doctype(html)
        result["viewport"] = _meta_content(soup, name="viewport")
        result["meta_refresh"] = meta_refresh_content(soup)
    else:
        result["title"] = None
        result["meta_description"] = None
        result["meta_description_count"] = 0
        result["robots"] = None
        result["robots_meta"] = []
        result["robots_meta_scoped"] = []
        result["charset"] = None
        result["doctype"] = None
        result["viewport"] = None
        result["meta_refresh"] = ""

    if opts["canonical"]:
        canonical_tag = _canonical_tag(soup)
        # "href" is single-valued, so this is always a plain string.
        href = cast("str | None", canonical_tag.get("href")) if canonical_tag else None
        result["canonical"] = urljoin(base_url, href.strip()) if href else None
    else:
        result["canonical"] = None

    if opts["og"]:
        og: dict[str, str] = {}
        twitter: dict[str, str] = {}
        for tag in soup.find_all("meta"):
            if _has_ancestor(tag, _INERT_LINK_CONTAINERS):
                continue  # a template-only OG/Twitter tag is never rendered, see _INERT_LINK_CONTAINERS
            # "content" is single-valued, so this is always a plain string.
            content = cast("str | None", tag.get("content"))
            if content is None:
                continue
            prop = tag.get("property")
            if isinstance(prop, str) and prop.lower().startswith("og:"):
                og[prop.strip()] = collapse_whitespace(content)
                continue
            name = tag.get("name")
            if isinstance(name, str) and name.lower().startswith("twitter:"):
                twitter[name.strip()] = collapse_whitespace(content)
        result["og"] = og
        result["twitter"] = twitter
    else:
        result["og"] = {}
        result["twitter"] = {}

    result["headings"] = _extract_headings(soup) if opts["headings"] else {}
    result["h1_alt_only_text"] = h1_alt_only_text(soup) if opts["headings"] else None
    # jsonld stays what it has always been — the blocks that parsed — and the
    # ones that did not are reported beside it rather than dropped.
    if opts["jsonld"]:
        result["jsonld"], result["jsonld_invalid"] = _extract_jsonld(soup)
    else:
        result["jsonld"], result["jsonld_invalid"] = [], []
    if opts["links"]:
        content_config = options.get("content_area") if isinstance(options, dict) else None
        position_rules = options.get("link_position_rules") if isinstance(options, dict) else None
        if link_cap is None:
            result["links"] = _extract_links(
                soup,
                base_url,
                final_url,
                classify_links=opts["classify_links"],
                content_area_config=content_config,
                position_rules=position_rules,
            )
        else:
            links, observation = _extract_link_observations(
                soup,
                base_url,
                final_url,
                classify_links=opts["classify_links"],
                content_area_config=content_config,
                position_rules=position_rules,
                cap=link_cap,
            )
            result["links"] = links
            result["link_observation"] = observation
    else:
        result["links"] = []
    # Cheap regardless of site size: forms are rare compared to links, so — unlike
    # classify_links — there is no per-crawl memory concern that would justify an opt-out.
    if opts["forms"]:
        if form_cap is None:
            result["forms"] = _extract_forms(soup, base_url, final_url)
        else:
            forms, omitted = _extract_form_observations(soup, base_url, final_url, cap=form_cap)
            result["forms"] = forms
            result["forms_omitted"] = omitted
    else:
        result["forms"] = []
    # url_sources covers carriers beyond a[href] (srcset, ping, formaction,
    # cite, meta-refresh, itemtype). It is off by default to preserve the links contract.
    if opts["url_sources"]:
        result["url_sources"] = extract_url_sources(soup, base_url)

    if opts["text"]:
        text = _extract_text(soup)
        result["text"] = text
        # Word count is scoped to the content area (nav/footer excluded by
        # default) so a mega-menu can't make a thin page look substantial;
        # "text" above stays whole-body on purpose. page_facts.py's schema
        # evidence (sameAs social links, breadcrumbs, price/rating regexes)
        # depends on facts that legitimately live in header/footer widgets the
        # content area excludes, so scoping "text" would silently cost that
        # evidence. citability_check(url=...) does not read this field either
        # way: it scores markdown_extract's content-area Markdown instead,
        # because "text" is a single collapsed line with no paragraph or
        # heading breaks for the scorer to find. Link discovery never sees the
        # resolved root, so restricting text never restricts the crawl.
        content_config = options.get("content_area") if isinstance(options, dict) else None
        for index, frame_tag in enumerate(soup.find_all("iframe")):
            frame_tag[_FRAME_MARKER] = str(index)
        content_root, strategy = resolve_content_area(soup, content_config)
        content_text = extract_area_text(content_root)
        result["content_text"] = content_text
        result["content_area_strategy"] = strategy
        result["word_count"] = len(content_text.split())
        # Scoped to content_text, not the whole document -- see count_lorem_ipsum.
        result["lorem_ipsum_count"] = count_lorem_ipsum(content_text)
        result["frames"] = extract_frames(
            soup,
            base_url,
            final_url,
            {str(el.get(_FRAME_MARKER)) for el in content_root.find_all("iframe")},
        )
        for frame_tag in soup.find_all("iframe"):
            del frame_tag[_FRAME_MARKER]
    else:
        result["text"] = ""
        result["content_text"] = ""
        result["content_area_strategy"] = None
        result["word_count"] = 0
        result["lorem_ipsum_count"] = 0
        result["frames"] = []

    # Always computed, regardless of the option flags above: it is a handful of
    # already-parsed-tree lookups, not a separate extraction pass, and every
    # option that turns off title/canonical/etc. text still leaves the tree to
    # read positions from. See document_position (issue #123).
    result["position"] = document_position(soup, html)
    # Same reasoning as position above: a handful of link lookups on a tree
    # that is already built, and the one authoritative statement a site makes
    # about which pages are the same page in another language (#357).
    result["hreflang"] = extract_hreflang(soup, base_url)
    # Same reasoning again: <img> alt-attribute evidence and legacy plugin
    # elements are both handful-of-lookups on the already-built tree (#385, #386).
    result["images"] = extract_images(soup)
    result["plugin_elements_count"] = unsupported_plugin_count(soup)

    # Built imperatively above (one assignment per option branch) rather than as
    # one literal, so a plain dict is the natural builder; cast once at the
    # boundary instead of restructuring the loop above around a TypedDict literal.
    return cast(ParsedPage, result)


def _rel_has(token: str) -> Callable[[Any], bool]:
    """Match a ``rel`` attribute (list or string) that contains ``token``."""
    target = token.lower()

    def _matcher(value: Any) -> bool:
        if value is None:
            return False
        tokens = value.split() if isinstance(value, str) else list(value)
        return any(isinstance(t, str) and t.lower() == target for t in tokens)

    return _matcher


# ── FETCH + PARSE ─────────────────────────────────────────────────────────────


def fetch_html(url: str, timeout: float | None = None) -> dict[str, Any]:
    """Fetch ``url`` and return its raw response, unparsed.

    ``ok`` reports whether the *request* succeeded, not the HTTP status: a
    404 or 500 still returns ``ok: True`` with the body it sent, exactly
    like ``parse_url`` has always tolerated (a soft-404 page's own markup is
    evidence, not noise). Only a transport failure (DNS, TLS, timeout, ...)
    sets ``ok: False`` with an ``error``. Callers that need something other
    than ``parse_html``'s extraction (Markdown rendering, boilerplate
    hashing, a content-area-only citability score) fetch through this
    function rather than duplicating the request logic.

    Returns ``{"ok", "url", "final_url", "status_code", "html"}`` on a
    completed request, or ``{"ok": False, "url", "error"}`` on a transport
    failure.
    """
    try:
        resolved_timeout = float(timeout or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        resolved_timeout = DEFAULT_TIMEOUT
    try:
        client, _http2_capable = http_client(
            resolved_timeout,
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
        )
        with client:
            response = client.get(url)
        return {
            "ok": True,
            "url": url,
            "final_url": str(response.url),
            "status_code": response.status_code,
            "html": response.text,
        }
    except Exception as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def parse_url(url: str, options: dict[str, Any] | None = None) -> ParseResult:
    """Fetch ``url`` and return its extracted SEO data.

    ``options`` accepts the boolean flags ``meta``, ``canonical``, ``og``,
    ``headings``, ``jsonld``, ``links``, ``forms`` and ``text`` (all default True), plus
    ``url_sources`` and ``classify_links`` (both default False). A ``timeout``
    (seconds) may also be provided, a ``content_area`` dict configures the
    region ``word_count`` is scoped to — see ``content_area.resolve_content_area``
    for its keys — and, when ``classify_links`` is on, ``link_position_rules``
    (a list of ``{"position", "selector"}`` dicts; see ``link_position.py``)
    overrides the default nav/header/sidebar/footer rules.

    On success returns a dict with keys: ``url``, ``final_url``,
    ``status_code``, ``ok``, ``title``, ``meta_description``, ``canonical``,
    ``robots``, ``charset``, ``doctype``, ``viewport``, ``og``, ``twitter``,
    ``headings``, ``jsonld``, ``links``, ``forms``, ``text``, ``content_text``,
    ``content_area_strategy`` and ``word_count``.

    On any fetch or parse error returns ``{"url", "ok": False, "error"}``
    rather than raising.
    """
    opts = options or {}
    fetched = fetch_html(url, timeout=opts.get("timeout"))
    # fetch_html builds plain dicts, so the two shapes are asserted here rather
    # than checked: a transport failure carries url/ok/error, and a completed
    # request carries the page fields on top of the response metadata. The
    # runtime guard for both is tests/test_typed_handlers.py.
    if not fetched["ok"]:
        return cast("ParseFailed", fetched)
    data = parse_html(fetched["html"], fetched["final_url"], options)
    return cast(
        "ParseFetched",
        {
            "url": url,
            "final_url": fetched["final_url"],
            "status_code": fetched["status_code"],
            "ok": 200 <= fetched["status_code"] < 300,
            **data,
        },
    )


# ── SMOKE TEST (no network) ───────────────────────────────────────────────────

if __name__ == "__main__":
    sample = """
    <html lang="en">
      <head>
        <title>Example &amp; Co</title>
        <meta name="description" content="A short   description.">
        <meta name="robots" content="index, follow">
        <link rel="canonical" href="/canonical-page">
        <meta property="og:title" content="OG Title">
        <meta name="twitter:card" content="summary">
        <script type="application/ld+json">{"@type": "Article", "name": "X"}</script>
      </head>
      <body>
        <h1>Main Heading</h1>
        <h2>Sub</h2>
        <a href="/internal">Internal</a>
        <a href="https://other.example.org/x" rel="nofollow noopener">External</a>
        <a href="//cdn.example.org/y" target="_blank">Protocol-relative, new tab</a>
        <a href="mailto:a@b.com">Mail</a>
        <form method="post" action="http://insecure.example.com/submit">
          <input type="password" name="pw">
        </form>
        <p>Hello   world  from the body.</p>
        <script>ignore()</script>
      </body>
    </html>
    """
    parsed = parse_html(sample, "https://example.com/page")

    assert parsed["title"] == "Example & Co", parsed["title"]
    assert parsed["meta_description"] == "A short description."
    assert parsed["robots"] == "index, follow"
    assert parsed["canonical"] == "https://example.com/canonical-page", parsed["canonical"]
    assert parsed["og"] == {"og:title": "OG Title"}, parsed["og"]
    assert parsed["twitter"] == {"twitter:card": "summary"}, parsed["twitter"]
    assert parsed["headings"] == {"h1": ["Main Heading"], "h2": ["Sub"]}, parsed["headings"]
    assert parsed["jsonld"] == [{"@type": "Article", "name": "X"}], parsed["jsonld"]

    hrefs = {link["href"]: link for link in parsed["links"]}
    assert "https://example.com/internal" in hrefs
    assert "https://other.example.org/x" in hrefs
    assert not hrefs["https://example.com/internal"]["external"]
    assert hrefs["https://other.example.org/x"]["external"]
    assert hrefs["https://other.example.org/x"]["nofollow"]
    assert hrefs["https://example.com/internal"]["raw_href"] == "/internal"
    assert all("mailto" not in h for h in hrefs)  # mailto skipped

    blank_link = hrefs["https://cdn.example.org/y"]
    assert blank_link["raw_href"] == "//cdn.example.org/y"  # protocol-relative, pre-resolution
    assert blank_link["target"] == "_blank"
    assert hrefs["https://example.com/internal"]["target"] == ""

    assert len(parsed["forms"]) == 1
    form = parsed["forms"][0]
    assert form["method"] == "post"
    assert form["action"] == "http://insecure.example.com/submit"
    assert form["has_password"] is True

    assert "Hello world from the body." in parsed["text"]
    assert "ignore" not in parsed["text"]  # script stripped
    assert parsed["word_count"] > 0

    # Element position (issue #123): a clean head reports nothing outside it.
    pos = parsed["position"]
    assert pos["head_count"] == 1 and pos["body_count"] == 1
    assert pos["head_not_first"] is False
    assert pos["invalid_head_elements"] == []
    assert pos["canonical_outside_head"] is False
    assert pos["title_outside_head"] is False

    # A body-only element in <head> forces everything after it out — the
    # classic real-world cause (a stray <div>, here) pushes the canonical that
    # follows it into <body>, exactly as a browser would read it.
    broken_head = """
    <html><head>
      <title>T</title>
      <script>ignore()</script>
      <div>oops</div>
      <link rel="canonical" href="https://example.com/c">
    </head><body>hi</body></html>
    """
    broken = parse_html(broken_head, "https://example.com/page")
    assert broken["position"]["canonical_outside_head"] is True
    assert broken["position"]["invalid_head_elements"] == ["div"]
    assert broken["canonical"] == "https://example.com/c"  # still found — just misplaced

    two_bodies = "<html><head><title>T</title></head><body>a</body><body>b</body></html>"
    assert parse_html(two_bodies, "https://example.com/page")["position"]["body_count"] == 2

    # Option flags disable their extraction.
    off = parse_html(sample, "https://example.com/page", {"headings": False, "links": False})
    assert off["headings"] == {}
    assert off["links"] == []
    assert off["title"] == "Example & Co"  # meta still on

    # is_external edge cases.
    assert is_external("https://a.com/x", "https://b.com/y")
    assert not is_external("https://a.com/x", "https://a.com/y")
    assert not is_external("https://A.com/x", "https://a.com/y")  # case-insensitive

    print("OK: parser smoke test passed")
