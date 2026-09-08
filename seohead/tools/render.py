"""Compare raw server HTML with the rendered DOM seen after JavaScript executes.

A search crawler receives the server response, while a browser user may see a
different document after client-side scripts run. This check measures that gap.
Google can render JavaScript, but rendering is deferred and not guaranteed;
Yandex has more limited rendering; many AI crawlers do not render at all. A page
looking complete in a browser therefore does not prove that its source response
contains indexable content and links.

Performance values are laboratory measurements only: LCP, CLS, and timing data
from one run on one machine. They are not field Core Web Vitals from the Chrome
UX Report and are explicitly returned under ``metrics_lab``.

``render_document`` is the engine behind selective rendering escalation across
a whole crawl (#18, ``seohead.crawl.render_escalation``): unlike
``render_check``'s single fixed desktop/mobile comparison, it honours every
setting that changes what the rendered DOM contains -- script timeout,
viewport, resize-to-content, shadow-DOM and iframe flattening, device pixel
ratio, mobile/touch emulation, page-load strategy -- because those settings
are exactly what makes two render runs on the same site not comparable
unless both are recorded (see ``seohead.crawl.settings`` for where that
recording happens).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunsplit

from bs4 import BeautifulSoup

from seohead.recon.net import UA, http_client, normalize_url, validate_url
from seohead.tools import dualcrawl

# Two fixed profiles rather than a free-form width/height: a responsive page
# renders a different DOM at different widths, so comparing two runs requires
# a short, named list both can point at. seohead.crawl.settings' rendering
# config reuses this exact mapping.
VIEWPORT_PRESETS: dict[str, dict[str, int]] = {
    "desktop": {"width": 1366, "height": 768},
    "mobile": {"width": 390, "height": 844},
}

# A stable diagnostic representation, not a crawler identity or fingerprint-evasion
# profile. ``render_check(..., viewport="mobile")`` compares what a typical
# smartphone request receives; callers who need a site's exact variant can supply
# one explicit user_agent, which both raw and browser requests then share.
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

# Common single-page application shells. An empty mount container means the raw
# response exposes no application content to a crawler that does not render.
_SHELL_IDS = ("root", "app", "__next", "__nuxt", "q-app", "main-app")

# Below this word threshold, the raw response is effectively empty without
# JavaScript and warrants a dedicated finding.
EMPTY_BODY_WORDS = 50

# The sole all-clear message also determines ``js_dependent``. Keeping it in one
# constant prevents the summary and findings from drifting apart.
ALL_CLEAR = (
    "Raw HTML and rendered DOM are materially equivalent; JavaScript "
    "rendering does not determine SEO-visible content"
)

# A render that did not finish returns a document with none of the landmarks the
# raw response carries, at a small fraction of its size. Compared naively, that
# emptiness reads as the site deleting its own title and canonical (#623). The
# ratio is deliberately generous: a page whose rendered DOM is at least half the
# raw response's size was captured, whatever else it did.
INCOMPLETE_RENDER_BYTE_RATIO = 0.5

# Named reason and machine-readable code for that state. It is neither a clean
# result nor a finding: it says this run did not measure the page.
INCOMPLETE_RENDER_CODE = "incomplete_render"
RENDER_UNAVAILABLE = (
    "The rendered DOM was not captured, so the raw-versus-rendered comparison "
    "is unavailable: {reason}. Nothing about this page's JavaScript dependence "
    "follows from this run -- re-run it, if needed with --wait domcontentloaded "
    "or a longer --timeout"
)

# A fallback capture read the DOM at an earlier milestone than the one that was
# requested (see ``_capture_dom``). ``wait_reached`` records that on the result,
# but ``seohead.audit.site`` carries findings *text* into a report and nothing
# else -- so unless the findings list says the milestone was missed, a page
# whose scripts had not run yet arrives in the report as an affirmative "raw and
# rendered are equivalent", graded a notice (#642).
MILESTONE_MISSED = (
    "The requested {wait} milestone was never reached; the DOM was read at "
    "{reached} instead, so scripts may not have finished running before the "
    "snapshot was taken. What follows describes this run, not the page -- "
    "re-run it with a longer --timeout, or with --wait {reached} to request "
    "that milestone deliberately"
)

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_BROWSER_RESPONSE_BYTES = 5 * 1024 * 1024
_BROWSER_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_BLOCKED_WEBSOCKET_LIMITATION = "browser WebSocket requests are unsupported by pinned rendering"
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

# ``route.fulfill`` writes the body it is handed straight into the renderer: it
# never applies a declared ``Content-Encoding``. Forwarding the origin's
# compression header alongside a body this route has already decoded would hand
# Chromium gzip bytes labelled ``text/html`` and produce a DOM made of the
# compressed stream (#650). The header describes a transfer coding that ends
# here, so it is dropped exactly like the hop-by-hop set above; ``content-length``
# is already in that set, which leaves Playwright to state the decoded length.
_TRANSFER_CODING_HEADERS = frozenset({"content-encoding"})

# Fallback advertised when the client cannot state its own decodable set. httpx
# decodes these two without any optional dependency.
_BASELINE_ENCODINGS = "gzip, deflate"


def _decodable_encodings(client: Any) -> str:
    """Return the content codings this HTTP client can actually decode.

    httpx builds its own ``Accept-Encoding`` from the decoders it has -- which
    grows to ``br`` and ``zstd`` when brotli/zstandard are installed and shrinks
    when they are not. Reading it back is what keeps the coding this route asks
    for and the coding it can decode from ever disagreeing.
    """
    headers = getattr(client, "headers", None)
    getter = getattr(headers, "get", None)
    advertised = getter("accept-encoding") if getter is not None else None
    return str(advertised) if advertised else _BASELINE_ENCODINGS


def _undecoded_coding(content_encoding: str, decodable: str) -> str:
    """Name a coding the origin applied that this client did not decode.

    httpx passes an unrecognised coding through untouched rather than failing,
    so a non-compliant origin can still answer in a coding nobody asked for.
    Naming it aborts the request instead of rendering the compressed stream.
    """
    supported = {item.strip().lower().split(";")[0] for item in decodable.split(",")}
    for item in (content_encoding or "").split(","):
        coding = item.strip().lower()
        if coding and coding != "identity" and coding not in supported:
            return coding
    return ""


def _guard_browser_route(route) -> None:
    """Fail closed if a pinned HTTP fulfiller was not installed."""
    route.abort("blockedbyclient")


def _pinned_browser_route(
    client: Any,
    *,
    request_gate: Callable[[], None] | None = None,
    max_response_bytes: int = _BROWSER_RESPONSE_BYTES,
) -> tuple[Callable[[Any], None], list[str]]:
    """Build a Playwright fulfiller backed by the shared pinned HTTP transport."""
    if type(max_response_bytes) is not int or max_response_bytes < 1:
        raise ValueError("browser response limit must be a positive integer")
    limitations: list[str] = []

    def abort(route: Any, reason: str) -> None:
        if reason not in limitations:
            limitations.append(reason)
        route.abort("blockedbyclient")

    def handler(route: Any) -> None:
        request = route.request
        url = str(request.url)
        method = str(request.method).upper()
        if method not in _BROWSER_METHODS:
            abort(route, f"browser method {method} is unsupported by pinned rendering")
            return
        try:
            validate_url(url)
            if request_gate is not None:
                request_gate()
            headers = {
                name: value
                for name, value in request.all_headers().items()
                if name.lower() not in _HOP_BY_HOP_HEADERS and name.lower() != "accept-encoding"
            }
            # Chromium invites codings this transport may not own a decoder for
            # (it asks for br and zstd), and the reply has to be decoded here
            # before Playwright sees it. Asking only for what this client can
            # decode keeps the origin from answering in a coding that would
            # reach the renderer unparsed.
            decodable = _decodable_encodings(client)
            headers["accept-encoding"] = decodable
            cookies = getattr(client, "cookies", None)
            if cookies is not None:
                cookies.clear()
            with client.stream(method, url, headers=headers, content=None) as response:
                response_headers: dict[str, str] = {}
                response_header_names: dict[str, str] = {}
                cookie_headers: list[str] = []
                has_cors_header = False
                for name, value in response.headers.multi_items():
                    lowered = name.lower()
                    if lowered == "set-cookie":
                        cookie_headers.append(value)
                        continue
                    if lowered == "access-control-allow-origin":
                        has_cors_header = True
                    if (
                        lowered not in _HOP_BY_HOP_HEADERS
                        and lowered not in _TRANSFER_CODING_HEADERS
                    ):
                        response_name = response_header_names.setdefault(lowered, name)
                        if response_name in response_headers:
                            response_headers[response_name] += f", {value}"
                        else:
                            response_headers[response_name] = value
                if cookie_headers:
                    response_headers["set-cookie"] = "\n".join(cookie_headers)
                origin = headers.get("origin")
                request_parts = urlparse(url)
                request_origin = f"{request_parts.scheme}://{request_parts.netloc}"
                if origin and origin != request_origin and not has_cors_header:
                    response_headers["access-control-allow-origin"] = ""
                undecoded = _undecoded_coding(
                    response.headers.get("content-encoding", ""), decodable
                )
                if undecoded:
                    abort(
                        route,
                        f"browser response content coding {undecoded} is undecodable "
                        "by pinned rendering",
                    )
                    return
                # Decoded bytes, not transferred ones: this is the body Chromium
                # is handed, so it is also the body the cap has to measure -- a
                # compression bomb would otherwise pass the cap compressed.
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > max_response_bytes:
                        abort(route, "browser response exceeds pinned rendering byte limit")
                        return
                    body.extend(chunk)
                route.fulfill(
                    status=response.status_code, headers=response_headers, body=bytes(body)
                )
        except Exception as exc:
            abort(route, f"pinned browser request failed: {type(exc).__name__}: {exc}")

    return handler, limitations


def _guard_websocket_route(ws_route: Any, limitations: list[str] | None = None) -> None:
    """Fail closed because this renderer has no pinned WebSocket transport."""
    if limitations is not None and _BLOCKED_WEBSOCKET_LIMITATION not in limitations:
        limitations.append(_BLOCKED_WEBSOCKET_LIMITATION)
    ws_route.close()


def _refuse_if_root() -> None:
    """Refuse to launch the rendering browser as root, rather than disable its sandbox.

    Chromium's sandbox will not start as root unless it is told to run
    without one (``--no-sandbox``). This toolkit never passes that flag --
    rendering executes whatever code the audited site serves, and a browser
    with no sandbox removes the one barrier between that code and the host
    running the crawl. Refusing outright is the documented alternative.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and geteuid() == 0:
        raise RuntimeError(
            "refusing to launch the rendering browser as root: that would require "
            "disabling the sandbox (--no-sandbox), which this toolkit never does -- "
            "run the crawl as a non-root user instead"
        )


def _artifact_filename(url: str) -> str:
    """A filesystem-safe, collision-resistant name for one URL's artifacts."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


_FRAGMENT_META_RE = re.compile(
    r'<meta[^>]+name=["\']fragment["\'][^>]+content=["\']!["\']', re.IGNORECASE
)


def legacy_fragment_target(url: str, html: str) -> str | None:
    """Return the ``_escaped_fragment_`` URL for a page opting into the legacy
    AJAX-crawling scheme, or ``None`` when the page does not declare it.

    Google's now-deprecated scheme let a site announce, via a ``#!`` hash
    fragment in its own URL or a page-wide
    ``<meta name="fragment" content="!">``, that a fully rendered snapshot is
    available at a companion URL built from ``?_escaped_fragment_=``. Some
    legacy single-page applications still implement only this, not real
    server-side rendering or a modern render pipeline, so honouring it
    recovers real content without needing a browser at all.
    """
    parts = urlparse(url)
    fragment = parts.fragment
    if fragment.startswith("!"):
        escaped = fragment[1:]
    elif html and _FRAGMENT_META_RE.search(html):
        escaped = ""
    else:
        return None
    query = dict(parse_qsl(parts.query))
    query["_escaped_fragment_"] = escaped
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


# This script reads laboratory metrics after load. LCP and CLS are captured by
# PerformanceObserver; the remaining values come from Navigation Timing.
_METRICS_JS = """() => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const lcpEntries = performance.getEntriesByType('largest-contentful-paint') || [];
  const paints = {};
  for (const p of performance.getEntriesByType('paint')) paints[p.name] = Math.round(p.startTime);
  return {
    ttfb_ms: Math.round(nav.responseStart || 0),
    dom_content_loaded_ms: Math.round(nav.domContentLoadedEventEnd || 0),
    load_ms: Math.round(nav.loadEventEnd || 0),
    first_contentful_paint_ms: paints['first-contentful-paint'] ?? null,
    largest_contentful_paint_ms: window.__seohead_lcp
      ? Math.round(window.__seohead_lcp)
      : (lcpEntries.length ? Math.round(lcpEntries[lcpEntries.length - 1].startTime) : null),
    cumulative_layout_shift: window.__seohead_cls != null
      ? Math.round(window.__seohead_cls * 1000) / 1000 : null,
    transfer_size_kb: nav.transferSize ? Math.round(nav.transferSize / 1024) : null,
  };
}"""

# CLS and LCP accumulate from navigation start, so observers must be installed
# before navigation. Installing them afterward misses the earliest and often
# largest shifts. LCP also requires a buffered observer in practice because
# ``getEntriesByType('largest-contentful-paint')`` is commonly empty; relying on
# that API alone produced null LCP values in real-site runs.
_CLS_INIT_JS = """
window.__seohead_cls = 0;
window.__seohead_lcp = 0;
try {
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (!entry.hadRecentInput) window.__seohead_cls += entry.value;
    }
  }).observe({type: 'layout-shift', buffered: true});
} catch (e) {}
try {
  new PerformanceObserver((list) => {
    const entries = list.getEntries();
    if (entries.length) window.__seohead_lcp = entries[entries.length - 1].startTime;
  }).observe({type: 'largest-contentful-paint', buffered: true});
} catch (e) {}
"""

# getComputedStyle resolves background-image wherever the declaring CSS rule
# lives -- inline style, a <style> block, or an external stylesheet -- so it
# is the only way to see a background image an external stylesheet declares:
# that CSS text never appears in either the raw or the rendered HTML string.
_BACKGROUND_IMAGES_JS = """() => {
  const found = new Set();
  const urlRe = /url\\((['"]?)(.*?)\\1\\)/g;
  document.querySelectorAll('*').forEach((el) => {
    const bg = getComputedStyle(el).backgroundImage;
    if (!bg || bg === 'none') return;
    let m;
    while ((m = urlRe.exec(bg))) {
      const src = m[2];
      if (src && !src.startsWith('data:')) found.add(new URL(src, document.baseURI).href);
    }
  });
  return Array.from(found);
}"""


def _visible_text(html: str) -> str:
    """Return candidate content text after removing scripts, styles, and tags."""
    return _TAG_RE.sub(" ", _SCRIPT_STYLE_RE.sub(" ", html or ""))


def _words(html: str) -> int:
    return len([w for w in _visible_text(html).split() if len(w) > 1])


def _links(html: str, base_url: str) -> set[str]:
    """Return internal links that can participate in crawling the site."""
    if not html:
        return set()
    from seohead.tools.parser import document_base_url

    # Host comes from the page URL; links resolve against the document base.
    host = urlparse(normalize_url(base_url)).hostname or ""
    soup = BeautifulSoup(html, features="lxml")
    resolve_from = document_base_url(soup, base_url)
    out: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = str(tag["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(resolve_from, href).split("#")[0]
        if (urlparse(absolute).hostname or "") == host:
            out.add(absolute)
    return out


def _jsonld_types(html: str) -> list[str]:
    """Extract Schema.org JSON-LD types.

    Markup injected only after JavaScript does not exist for non-rendering
    crawlers, so raw and rendered type sets are measured separately.
    """
    types: list[str] = []
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html or "",
        re.IGNORECASE | re.DOTALL,
    ):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                t = node.get("@type")
                if isinstance(t, str):
                    types.append(t)
                elif isinstance(t, list):
                    types += [x for x in t if isinstance(x, str)]
                stack += [v for v in node.values() if isinstance(v, (dict, list))]
            elif isinstance(node, list):
                stack += node
    return sorted(set(types))


def detect_empty_shell(html: str) -> str | None:
    """Return the ID of an empty SPA mount container, or ``None``.

    A raw-HTML regex match, not a rendering result -- so this is also what
    the crawl-level gate in ``seohead.crawl.render_escalation`` calls on the
    start page's raw HTML, before any browser is ever launched (#18).
    """
    for shell_id in _SHELL_IDS:
        m = re.search(
            rf'<div[^>]+id=["\']{shell_id}["\'][^>]*>(.*?)</div>',
            html or "",
            re.IGNORECASE | re.DOTALL,
        )
        if m and not m.group(1).strip():
            return shell_id
        if re.search(
            rf'<div[^>]+id=["\']{shell_id}["\'][^>]*/?>\s*</div>', html or "", re.IGNORECASE
        ):
            return shell_id
    return None


def _snapshot(html: str, url: str) -> dict[str, Any]:
    """Build an identical, comparable snapshot for raw HTML and rendered DOM."""
    from seohead.tools.page_facts import extract

    facts = extract(html, url) if html else {}
    # Images: <img>/<source> plus CSS url() backgrounds declared inline or in a
    # <style> block. A background declared only in an external stylesheet is
    # not visible here -- render_check() merges that in from computed styles.
    images = sorted(dualcrawl.build_page_evidence(html, url)["images"])
    return {
        "words": _words(html),
        "links": len(_links(html, url)),
        "images": images,
        "title": facts.get("title") or "",
        "h1": facts.get("h1") or "",
        "canonical": facts.get("canonical") or "",
        "jsonld_types": _jsonld_types(html),
        "html_bytes": len(html or ""),
    }


def incomplete_render_reason(raw: dict[str, Any], rendered: dict[str, Any]) -> str | None:
    """Name why a rendered snapshot cannot be compared, or return ``None``.

    A browser that fails mid-navigation still hands back a document, and that
    document is nearly empty. Comparing it against a full raw response produces
    confident nonsense -- "the title changes after JavaScript" from a title the
    render never read, "the canonical is injected by JavaScript" from a
    canonical the render never saw (#623, four such findings in a row on one
    live site). The pair is therefore judged before it is compared.

    The evidence for "this did not capture the page" is the *conjunction*: the
    raw response carries landmarks (a title, or internal links), the rendered
    document carries none of them at all -- no title, no h1, no canonical, no
    internal link -- and it is a fraction of the raw response's size. A page
    that genuinely renders to nothing keeps at least one of those, and a page
    with no title and no links on either side is measured and merely empty, not
    unmeasured. Returning ``None`` therefore means "compare these", never "this
    page is fine".
    """
    if not isinstance(raw, dict) or not isinstance(rendered, dict):
        return None
    if rendered.get("title") or rendered.get("h1") or rendered.get("canonical"):
        return None
    if int(rendered.get("links") or 0) > 0:
        return None
    # Nothing was lost if the raw response had nothing to lose.
    if not raw.get("title") and int(raw.get("links") or 0) == 0:
        return None
    raw_bytes = int(raw.get("html_bytes") or 0)
    rendered_bytes = int(rendered.get("html_bytes") or 0)
    if raw_bytes <= 0 or rendered_bytes >= raw_bytes * INCOMPLETE_RENDER_BYTE_RATIO:
        return None
    return (
        "the rendered document has no title, no h1, no canonical and no internal links, "
        f"at {rendered_bytes} bytes against the raw response's {raw_bytes} "
        f"({rendered_bytes / raw_bytes:.0%})"
    )


def _shell_finding(shell: str) -> str:
    """State the empty-mount-point fact, which reads the raw HTML and nothing else."""
    return (
        f'Raw HTML contains an empty <div id="{shell}"> mount point; the '
        "page is assembled entirely by JavaScript, so a non-rendering "
        "crawler receives an empty page"
    )


def compare(
    raw: dict[str, Any], rendered: dict[str, Any], raw_html: str = "", shell: str | None = None
) -> list[str]:
    """Generate findings for a raw-HTML and rendered-DOM snapshot pair.

    This pure function uses neither the network nor a browser, allowing complete
    offline tests while the Playwright layer remains a thin adapter.

    A pair whose rendered half never captured the page yields the
    ``RENDER_UNAVAILABLE`` statement and no finding that draws on the rendered
    half: an unfinished measurement is not evidence about the site. Findings
    that read the raw response alone still hold, because nothing about them
    depended on the browser -- ``shell`` comes from ``detect_empty_shell()``,
    which never looks at the rendered DOM, and an empty single-page-application
    shell is exactly the page whose render times out, so dropping it with the
    render lost a genuine defect precisely where it mattered most (#642).
    """
    unavailable = incomplete_render_reason(raw, rendered)
    if unavailable:
        out: list[str] = [RENDER_UNAVAILABLE.format(reason=unavailable)]
        if shell:
            out.append(_shell_finding(shell))
        return out

    out = []

    if shell:
        out.append(_shell_finding(shell))
    elif raw.get("words", 0) < EMPTY_BODY_WORDS < rendered.get("words", 0):
        out.append(
            f"Raw HTML contains {raw['words']} words versus "
            f"{rendered['words']} after rendering; the server response "
            "contains effectively no page copy"
        )

    words_gain = rendered.get("words", 0) - raw.get("words", 0)
    if raw.get("words", 0) >= EMPTY_BODY_WORDS and words_gain > 0:
        share = words_gain / max(rendered.get("words", 1), 1)
        if share >= 0.3:
            out.append(
                f"{share:.0%} of page copy appears only after JavaScript "
                f"(+{words_gain} words); this content is unavailable to "
                "non-rendering crawlers"
            )

    links_gain = rendered.get("links", 0) - raw.get("links", 0)
    if links_gain > 0 and rendered.get("links", 0):
        share = links_gain / rendered["links"]
        if share >= 0.3 or raw.get("links", 0) == 0:
            out.append(
                f"{links_gain} of {rendered['links']} internal links appear "
                "only after JavaScript, reducing or preventing crawl discovery"
            )

    if raw.get("title") != rendered.get("title"):
        out.append(
            f"The title changes after JavaScript: raw {raw.get('title')!r}, "
            f"rendered {rendered.get('title')!r}; crawlers may index "
            "different title values"
        )
    if raw.get("h1") != rendered.get("h1") and rendered.get("h1"):
        out.append(
            f"H1 differs between raw HTML {raw.get('h1') or '—'!r} and rendered DOM "
            f"{rendered.get('h1')!r}"
        )
    if raw.get("canonical") != rendered.get("canonical"):
        out.append(
            "The canonical URL is injected or changed by JavaScript; this "
            "indexing directive should not depend on rendering"
        )

    new_types = set(rendered.get("jsonld_types", [])) - set(raw.get("jsonld_types", []))
    if new_types:
        out.append("Schema.org types appear only after JavaScript: " + ", ".join(sorted(new_types)))

    new_images = set(rendered.get("images", [])) - set(raw.get("images", []))
    if new_images:
        out.append(
            f"{len(new_images)} image(s) are visible only after rendering, most often a CSS "
            "background-image resolved from an external stylesheet; a non-rendering crawler, "
            "and every alt-text or image-weight check built on <img> alone, sees none of them"
        )

    if not out:
        out.append(ALL_CLEAR)
    return out


class _NeverRaised(Exception):
    """Stands in for Playwright's ``TimeoutError`` when it cannot be imported."""


# Deferred and lazily-hydrating scripts write the DOM after the navigation
# milestone resolves. Half a second is enough for that on the pages this check
# was misreading, and short enough not to change the cost of a render.
SETTLE_MS = 500


def _capture_dom(
    page: Any,
    target: str,
    wait: str,
    timeout: float,
    settle_ms: int,
    navigation_timeout: type[BaseException],
) -> str:
    """Navigate, settle, and report the load milestone actually reached.

    A site with long-polling third-party scripts -- analytics, chat, ads -- may
    never go network-idle and may not fire ``load`` either. That is ordinary,
    and losing the whole check to it is worse than capturing the DOM slightly
    earlier. ``DOMContentLoaded`` has normally fired long before the timeout, so
    ``wait_for_load_state`` returns at once and the page is read without being
    fetched a second time. When it genuinely never fired there is no document to
    read, and the timeout propagates unchanged.
    """
    try:
        page.goto(target, wait_until=wait, timeout=timeout * 1000)
        reached = wait
    except navigation_timeout:
        if wait == "domcontentloaded":
            raise
        page.wait_for_load_state("domcontentloaded", timeout=timeout * 1000)
        reached = "domcontentloaded"
    if settle_ms > 0:
        page.wait_for_timeout(settle_ms)
    return reached


def render_check(
    url: str,
    timeout: float = 30.0,
    wait: str = "load",
    viewport: str = "desktop",
    user_agent: str | None = None,
    *,
    settle_ms: int = SETTLE_MS,
    request_gate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Compare a server response with the DOM produced after JavaScript executes.

    Playwright is optional. When unavailable, the tool returns ``ok: False`` and
    an installation command instead of misrepresenting an unperformed check.

    ``wait="load"`` is deliberate. ``networkidle`` may never occur on commercial
    sites because analytics, chat, and advertising keep connections open, turning
    a useful render check into a timeout. Search-engine rendering does not require
    complete network silence either. Callers may still request ``networkidle``
    when a particular application genuinely needs it -- and when that milestone
    times out the DOM is still read at ``domcontentloaded`` rather than the whole
    check being lost, with ``wait_reached`` recording which milestone the
    snapshot actually came from -- and with the findings list saying so too, so
    that a snapshot taken before the scripts ran can never report an all-clear
    (#642). ``settle_ms`` is a short pause after that milestone for deferred
    scripts to write the DOM.

    When the browser hands back a document that never captured the page, the
    result is ``ok: False`` with a named reason (``reason:
    "incomplete_render"``) and both snapshots for inspection -- never findings
    about the site, which is what an unfinished render used to be reported as
    (#623). ``empty_shell`` rides along with it: that answer comes from the raw
    response and does not depend on the browser having finished.
    """
    if viewport not in VIEWPORT_PRESETS:
        return {"ok": False, "error": f"unknown viewport {viewport!r}"}
    selected_user_agent = user_agent or (MOBILE_USER_AGENT if viewport == "mobile" else UA)
    if (
        not isinstance(selected_user_agent, str)
        or "\r" in selected_user_agent
        or "\n" in selected_user_agent
    ):
        return {"ok": False, "error": "user_agent must be a single header line"}
    size = dict(VIEWPORT_PRESETS[viewport])
    if not url or not str(url).strip():
        return {"ok": False, "error": "URL is required"}
    target = normalize_url(str(url).strip())

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "ok": False,
            "error": "Playwright is required",
            "install": "pip install 'seohead[render]' && python -m playwright install chromium",
        }
    try:
        from playwright.sync_api import TimeoutError as navigation_timeout
    except ImportError:  # a harness may expose only sync_playwright
        navigation_timeout = _NeverRaised
    try:
        validate_url(target)
    except ValueError as exc:
        return {"ok": False, "url": target, "error": str(exc)}
    try:
        _refuse_if_root()
    except RuntimeError as exc:
        return {"ok": False, "url": target, "error": str(exc)}

    # Fetch raw HTML with the regular client: this is what a non-rendering crawler receives.
    if request_gate is None:
        client, _ = http_client(timeout, headers={"User-Agent": selected_user_agent})
    else:
        client, _ = http_client(
            timeout,
            headers={"User-Agent": selected_user_agent},
            event_hooks={"request": [lambda _request: request_gate()]},
        )
    try:
        resp = client.get(target)
        raw_html = resp.text
        final_url = str(resp.url)
        status = resp.status_code
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Raw HTML fetch failed: {type(exc).__name__}: {exc}",
            "url": target,
            "viewport": viewport,
            "viewport_size": size,
            "user_agent": selected_user_agent,
        }
    finally:
        client.close()

    browser_client = None
    try:
        browser_client, _http2 = http_client(
            timeout, follow_redirects=False, headers={"User-Agent": selected_user_agent}
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(chromium_sandbox=True)
            try:
                # service_workers="block": a default-configuration service
                # worker can serve requests the page.route() guard below never
                # sees, the exact bypass #18's security section names.
                # user_agent=UA: the raw fetch above used this same identity. Without it,
                # Chromium's own default UA reaches the origin instead, and a server that
                # varies its response by User-Agent (legitimate, common, and unrelated to
                # JavaScript) looks indistinguishable from a page that genuinely needs a
                # renderer -- issue #199. Matching identity removes that confound rather
                # than trying to detect it after the fact.
                context = browser.new_context(
                    viewport=size,
                    is_mobile=(viewport == "mobile"),
                    has_touch=(viewport == "mobile"),
                    service_workers="block",
                    user_agent=selected_user_agent,
                )
                context.add_init_script(_CLS_INIT_JS)
                route_handler, limitations = _pinned_browser_route(
                    browser_client, request_gate=request_gate
                )
                context.route("**/*", route_handler)
                # WebSockets are not HTTP requests and page.route() never sees
                # them either; route_web_socket is the separate interception
                # point that covers them.
                context.route_web_socket(
                    "**/*", lambda ws_route: _guard_websocket_route(ws_route, limitations)
                )
                page = context.new_page()
                wait_reached = _capture_dom(
                    page, target, wait, timeout, settle_ms, navigation_timeout
                )
                rendered_html = page.content()
                rendered_url = page.url
                metrics = page.evaluate(_METRICS_JS)
                computed_backgrounds = page.evaluate(_BACKGROUND_IMAGES_JS)
                if limitations:
                    raise RuntimeError("; ".join(limitations))
            finally:
                browser.close()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Browser rendering failed: {type(exc).__name__}: {exc}",
            "url": target,
            "viewport": viewport,
            "viewport_size": size,
            "user_agent": selected_user_agent,
            "raw": _snapshot(raw_html, final_url),
        }
    finally:
        if browser_client is not None:
            browser_client.close()

    raw = _snapshot(raw_html, final_url)
    rendered = _snapshot(rendered_html, rendered_url)
    # Merge in what only getComputedStyle can see: a background-image an
    # external stylesheet declares, absent from both HTML strings above.
    rendered["images"] = sorted(set(rendered["images"]) | set(computed_backgrounds))
    # Read from the raw response, before anything is decided about the render:
    # this answer holds whether or not the browser finished, which is why the
    # incomplete return below carries the key rather than omitting it (#642).
    shell = detect_empty_shell(raw_html)
    # An unfinished render is an unmeasured page, not a broken site: report it
    # the way every other unavailable measurement here is reported -- ok: False
    # with a named reason -- and emit no findings from it at all (#623). Both
    # snapshots ride along so the reason can be checked rather than believed.
    incomplete = incomplete_render_reason(raw, rendered)
    if incomplete:
        return {
            "ok": False,
            "url": target,
            "final_url": final_url,
            "status": status,
            "viewport": viewport,
            "viewport_size": size,
            "user_agent": selected_user_agent,
            "reason": INCOMPLETE_RENDER_CODE,
            "error": RENDER_UNAVAILABLE.format(reason=incomplete),
            "raw": raw,
            "rendered": rendered,
            "empty_shell": shell,
            "wait": wait,
            "wait_reached": wait_reached,
            "settle_ms": settle_ms,
            # Neither True nor False: this run does not know.
            "js_dependent": None,
            "metrics_lab": metrics,
        }
    findings = compare(raw, rendered, raw_html, shell)
    # Keep the summary aligned with findings: five widget words do not make a
    # page JavaScript-dependent, while findings use a 30% materiality threshold.
    js_dependent: bool | None = findings != [ALL_CLEAR]
    if wait_reached != wait:
        # _capture_dom() fell back to an earlier milestone. When scripts had not
        # run by then the rendered DOM equals the raw HTML, compare() fires on
        # nothing and ALL_CLEAR asserts that JavaScript does not determine this
        # page's content -- an affirmative verdict on a page nobody rendered.
        # The miss goes into the findings list because that list is what the
        # audit consumes, and it replaces the all-clear rather than joining it.
        findings = [MILESTONE_MISSED.format(wait=wait, reached=wait_reached)] + [
            f for f in findings if f != ALL_CLEAR
        ]
        # A difference that was found is still a difference; the absence of one
        # is not, so it stops being False and becomes "this run does not know".
        js_dependent = True if js_dependent else None
    # Its own report section, not merged into "findings": #21's compare()
    # assumes the site changed between two runs, this assumes the site is the
    # same and the method differs, so it gets its own schema/keys (dualcrawl.v1).
    dual_crawl = dualcrawl.compare_evidence(
        {final_url: {"images": set(raw["images"]), "links": _links(raw_html, final_url)}},
        {
            final_url: {
                "images": set(rendered["images"]),
                "links": _links(rendered_html, rendered_url),
            }
        },
        method_a="static",
        method_b="rendered",
    )
    return {
        "ok": True,
        "url": target,
        "final_url": final_url,
        "status": status,
        "viewport": viewport,
        "viewport_size": size,
        # Both snapshots were requested under this identity (#199) -- recorded so a report
        # can show its comparison is not confounded by a server that varies its response by
        # User-Agent, rather than leaving that an unstated assumption.
        "user_agent": selected_user_agent,
        "raw": raw,
        "rendered": rendered,
        "empty_shell": shell,
        # Which milestone the rendered snapshot actually came from: a site with
        # long-polling scripts never reaches networkidle, and a report should be
        # able to say the DOM was read at domcontentloaded instead of assuming
        # the requested milestone was the one that happened.
        "wait": wait,
        "wait_reached": wait_reached,
        "settle_ms": settle_ms,
        "js_dependent": js_dependent,
        # Laboratory, not field data: one run from one machine. Field Core Web
        # Vitals come from CrUX and must not be inferred from this measurement.
        "metrics_lab": metrics,
        "findings": findings,
        "dual_crawl": dual_crawl,
    }


def rendered_html(
    url: str,
    timeout: float = 30.0,
    wait: str = "load",
    *,
    request_gate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Return rendered HTML for tools that require the final DOM.

    A separate narrow function lets regional and similar audits request one HTML
    document without constructing the full raw-versus-rendered comparison report.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "ok": False,
            "error": "Playwright is required",
            "install": "pip install 'seohead[render]' && python -m playwright install chromium",
        }
    target = normalize_url(str(url or "").strip())
    if not target:
        return {"ok": False, "error": "URL is required"}
    try:
        validate_url(target)
    except ValueError as exc:
        return {"ok": False, "url": target, "error": str(exc)}
    try:
        _refuse_if_root()
    except RuntimeError as exc:
        return {"ok": False, "url": target, "error": str(exc)}
    browser_client = None
    try:
        browser_client, _http2 = http_client(
            timeout, follow_redirects=False, headers={"User-Agent": UA}
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch(chromium_sandbox=True)
            try:
                context = browser.new_context(service_workers="block", user_agent=UA)
                try:
                    route_handler, limitations = _pinned_browser_route(
                        browser_client, request_gate=request_gate
                    )
                    context.route("**/*", route_handler)
                    context.route_web_socket(
                        "**/*", lambda ws_route: _guard_websocket_route(ws_route, limitations)
                    )
                    page = context.new_page()
                    page.goto(target, wait_until=wait, timeout=timeout * 1000)
                    if limitations:
                        raise RuntimeError("; ".join(limitations))
                    return {"ok": True, "url": page.url, "html": page.content()}
                finally:
                    context.close()
            finally:
                browser.close()
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": target}
    finally:
        if browser_client is not None:
            browser_client.close()


# Merges every open shadow root's light-DOM-visible children into its host
# element so page.content() -- which only ever serializes light DOM -- carries
# what a search engine's own DOM flattening would see. Closed shadow roots are
# unreachable from page script at all and are left untouched, same as for any
# renderer.
_FLATTEN_SHADOW_DOM_JS = """() => {
  let flattened = 0;
  const walk = (root) => {
    root.querySelectorAll('*').forEach((el) => {
      if (el.shadowRoot) {
        walk(el.shadowRoot);
        el.append(...Array.from(el.shadowRoot.childNodes));
        flattened += 1;
      }
    });
  };
  walk(document);
  return flattened;
}"""

# Replaces each same-origin iframe with its own document's body content,
# matching how a search engine assembles one page out of same-origin frames.
# A cross-origin frame throws on contentDocument access and is left as an
# empty frame -- exactly what a non-rendering crawler could see too, so
# nothing is invented in its place.
_FLATTEN_IFRAMES_JS = """() => {
  let flattened = 0;
  document.querySelectorAll('iframe').forEach((frame) => {
    try {
      const doc = frame.contentDocument;
      if (doc && doc.body) {
        const div = document.createElement('div');
        div.setAttribute('data-flattened-iframe', frame.src || '');
        div.innerHTML = doc.body.innerHTML;
        frame.replaceWith(div);
        flattened += 1;
      }
    } catch (e) {
      // Cross-origin: not reachable from page script, left as-is.
    }
  });
  return flattened;
}"""


def _bounded_dom_script(max_html_bytes: int | None) -> str:
    limit = "null" if max_html_bytes is None else str(max_html_bytes)
    return f"""() => {{
      const html = document.documentElement.outerHTML;
      const bytes = new TextEncoder().encode(html).byteLength;
      const limit = {limit};
      if (limit !== null && bytes > limit) return {{complete: false, bytes}};
      return {{complete: true, bytes, html}};
    }}"""


def _safe_policy_facts(policy_facts: dict[str, Any] | None) -> dict[str, bool]:
    """Keep retention policy facts without copying headers, paths, or credentials."""
    facts = policy_facts or {}
    return {
        "credentials_used": bool(facts.get("credentials_used")),
        "cache_control_no_store": bool(facts.get("cache_control_no_store")),
    }


def render_document(
    url: str,
    rendering_config: dict[str, Any],
    *,
    nav_timeout: float = 30.0,
    artifacts_dir: str | None = None,
    user_agent: str = "",
    max_html_bytes: int | None = None,
    policy_facts: dict[str, Any] | None = None,
    request_gate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Render one URL under the full crawler rendering configuration.

    ``rendering_config`` is the resolved ``rendering`` block from
    ``seohead.crawl.settings`` (its ``browser`` and ``artifacts`` sub-dicts),
    not a browser handle -- which is exactly what lets a test replace this
    whole function with a stub for ``seohead.crawl.render_escalation``,
    never needing a real browser or the network.

    Unlike ``render_check`` (one fixed comparison for the single-page tool),
    this is what selective escalation calls for every page it decides to
    re-fetch, so every setting #18 asked for is honoured: script timeout
    (how long JavaScript may keep running after load), viewport,
    resize-to-content with its cap, shadow-DOM and iframe flattening, device
    pixel ratio, mobile/touch emulation, page-load strategy, and a persistent
    profile that stays off unless a directory is explicitly named.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {
            "ok": False,
            "error": "Playwright is required",
            "install": "pip install 'seohead[render]' && python -m playwright install chromium",
        }
    target = normalize_url(str(url or "").strip())
    if not target:
        return {"ok": False, "error": "URL is required"}
    try:
        validate_url(target)
    except ValueError as exc:
        return {"ok": False, "url": target, "error": str(exc)}
    try:
        _refuse_if_root()
    except RuntimeError as exc:
        return {"ok": False, "url": target, "error": str(exc)}

    if max_html_bytes is not None and (type(max_html_bytes) is not int or max_html_bytes < 0):
        return {
            "ok": False,
            "url": target,
            "error": "max_html_bytes must be a non-negative integer",
        }
    browser_cfg = rendering_config.get("browser", {})
    if browser_cfg.get("persistent_profile"):
        return {
            "ok": False,
            "url": target,
            "error": "persistent browser profiles are unavailable with pinned rendering until cookie continuity is verified",
        }
    artifacts_cfg = rendering_config.get("artifacts", {})
    preset = VIEWPORT_PRESETS.get(
        browser_cfg.get("viewport", "desktop"), VIEWPORT_PRESETS["desktop"]
    )
    viewport = dict(preset)
    console_errors: list[str] = []
    screenshot_path: str | None = None
    shadow_flattened = 0
    iframe_flattened = 0
    observed_policy = _safe_policy_facts(policy_facts)
    observed_policy["credentials_used"] |= bool(browser_cfg.get("persistent_profile"))
    engine_version = "unknown"
    browser_limitations: list[str] = []

    # There is deliberately no request hook beside _capture_response. Reading the
    # browser's own wire headers upgraded credentials_used the moment any request
    # carried Cookie:, and a browser carries back whatever the site's own
    # Set-Cookie gave it -- so a page that sets a session cookie and then asks for
    # one same-origin subresource, which is the ordinary shape of the web, had its
    # serialized DOM stored as credentialed on a run with nothing configured at all
    # (#656, the rendering lane's half of #647). What this run was configured to
    # send is already what sqlite_render._policy_facts derives and passes in as
    # policy_facts -- http.credential_headers, or a persistent browser profile --
    # and the wire has nothing to add to it: this renderer builds its own network
    # client with no credential of its own, so a sensitive header can only reach a
    # request through one of those two, or from the site itself.

    def _capture_response(response: Any) -> None:
        if max_html_bytes is None:
            return
        from seohead.crawl.cache import _parse_cache_control

        if "no-store" in _parse_cache_control(response.all_headers().get("cache-control", "")):
            observed_policy["cache_control_no_store"] = True

    def _on_console(msg: Any) -> None:
        if artifacts_cfg.get("console_errors") and msg.type == "error":
            console_errors.append(msg.text)

    browser = None
    network_client = None
    try:
        network_client, _http2 = http_client(
            nav_timeout,
            follow_redirects=False,
            headers={"User-Agent": user_agent or UA},
        )
        with sync_playwright() as pw:
            context_options = {
                "viewport": viewport,
                # The same identity the static crawl presented, for the same
                # reason #199 pinned it on the single-page probe: this fetch
                # replaces a page's body-derived evidence, so it must ask the
                # origin as the client the rest of the crawl was. Left to
                # Chromium's own default it advertises HeadlessChrome, which
                # bot protection commonly challenges, and a report then mixes
                # two populations -- escalated pages described from what the
                # site serves a headless browser, every other page from what
                # it serves the toolkit.
                "user_agent": user_agent or UA,
                "device_scale_factor": float(browser_cfg.get("device_pixel_ratio", 1.0) or 1.0),
                "is_mobile": bool(browser_cfg.get("mobile_emulation")),
                "has_touch": bool(browser_cfg.get("touch_emulation")),
                # Blocks the default-configuration bypass named in #18's
                # security section: a service worker can otherwise answer
                # requests page.route() never sees.
                "service_workers": "block",
            }
            browser = pw.chromium.launch(chromium_sandbox=True)
            context = browser.new_context(**context_options)
            actual_browser = browser if browser is not None else getattr(context, "browser", None)
            engine_version = str(getattr(actual_browser, "version", "unknown"))
            try:
                route_handler, browser_limitations = _pinned_browser_route(
                    network_client, request_gate=request_gate
                )
                context.route("**/*", route_handler)
                context.route_web_socket(
                    "**/*",
                    lambda ws_route: _guard_websocket_route(ws_route, browser_limitations),
                )
                page = context.new_page()
                if max_html_bytes is not None:
                    page.on("response", _capture_response)
                page.on("console", _on_console)
                page.goto(
                    target,
                    wait_until=browser_cfg.get("wait_until", "load"),
                    timeout=nav_timeout * 1000,
                )
                script_timeout = float(browser_cfg.get("script_timeout_seconds", 0) or 0)
                if script_timeout > 0:
                    page.wait_for_timeout(script_timeout * 1000)
                if browser_cfg.get("resize_to_content"):
                    cap = int(browser_cfg.get("resize_to_content_max_height_px", 15000))
                    content_height = int(page.evaluate("document.documentElement.scrollHeight"))
                    page.set_viewport_size(
                        {"width": viewport["width"], "height": max(min(content_height, cap), 1)}
                    )
                if browser_cfg.get("flatten_shadow_dom"):
                    shadow_flattened = int(page.evaluate(_FLATTEN_SHADOW_DOM_JS) or 0)
                if browser_cfg.get("flatten_iframes"):
                    iframe_flattened = int(page.evaluate(_FLATTEN_IFRAMES_JS) or 0)
                if max_html_bytes is None:
                    html = page.content()
                    dom = {"complete": True, "bytes": len(html.encode("utf-8")), "html": html}
                else:
                    dom = page.evaluate(_bounded_dom_script(max_html_bytes))
                final_url = page.url
                if artifacts_cfg.get("screenshots") and artifacts_dir:
                    os.makedirs(artifacts_dir, exist_ok=True)
                    screenshot_path = os.path.join(
                        artifacts_dir, _artifact_filename(target) + ".png"
                    )
                    page.screenshot(path=screenshot_path, full_page=True)
                if browser_limitations:
                    raise RuntimeError("; ".join(browser_limitations))
            finally:
                context.close()
                if browser is not None:
                    browser.close()
    except Exception as exc:
        return {"ok": False, "url": target, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if network_client is not None:
            network_client.close()

    renderer = {
        "engine": "playwright-chromium",
        "engine_version": engine_version,
        "navigation": {
            "requested_url": target,
            "final_url": final_url,
            "wait_until": browser_cfg.get("wait_until", "load"),
            "timeout_seconds": nav_timeout,
        },
        "settings": {
            "viewport": viewport,
            "device_pixel_ratio": float(browser_cfg.get("device_pixel_ratio", 1.0) or 1.0),
            "mobile_emulation": bool(browser_cfg.get("mobile_emulation")),
            "touch_emulation": bool(browser_cfg.get("touch_emulation")),
            "script_timeout_seconds": float(browser_cfg.get("script_timeout_seconds", 0) or 0),
            "resize_to_content": bool(browser_cfg.get("resize_to_content")),
            "resize_to_content_max_height_px": int(
                browser_cfg.get("resize_to_content_max_height_px", 15000)
            ),
            "persistent_profile": bool(browser_cfg.get("persistent_profile")),
        },
        "transforms": {
            "flatten_shadow_dom_requested": bool(browser_cfg.get("flatten_shadow_dom")),
            "flatten_shadow_dom_applied": shadow_flattened,
            "flatten_iframes_requested": bool(browser_cfg.get("flatten_iframes")),
            "flatten_iframes_applied": iframe_flattened,
        },
        "policy": observed_policy,
        "console_error_count": len(console_errors),
    }
    if not isinstance(dom, dict) or not dom.get("complete"):
        return {
            "ok": False,
            "url": target,
            "final_url": final_url,
            "dom_state": "truncated",
            "dom_bytes": (dom or {}).get("bytes") if isinstance(dom, dict) else None,
            "renderer": renderer,
            "error": "serialized DOM exceeds max_html_bytes",
        }

    return {
        "ok": True,
        "url": target,
        "final_url": final_url,
        "html": dom["html"],
        "dom_bytes": dom.get("bytes"),
        "dom_state": "complete",
        "renderer": renderer,
        "console_errors": console_errors,
        "screenshot_path": screenshot_path,
    }
