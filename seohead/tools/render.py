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

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_BROWSER_RESPONSE_BYTES = 5 * 1024 * 1024
_BROWSER_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
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
                if name.lower() not in _HOP_BY_HOP_HEADERS
            }
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
                    if lowered not in _HOP_BY_HOP_HEADERS:
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
                raw = bytearray()
                for chunk in response.iter_raw():
                    if len(raw) + len(chunk) > max_response_bytes:
                        abort(route, "browser response exceeds pinned rendering byte limit")
                        return
                    raw.extend(chunk)
                route.fulfill(
                    status=response.status_code, headers=response_headers, body=bytes(raw)
                )
        except Exception as exc:
            abort(route, f"pinned browser request failed: {type(exc).__name__}: {exc}")

    return handler, limitations


def _guard_websocket_route(ws_route: Any) -> None:
    """Fail closed because this renderer has no pinned WebSocket transport."""
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


def compare(
    raw: dict[str, Any], rendered: dict[str, Any], raw_html: str = "", shell: str | None = None
) -> list[str]:
    """Generate findings for a raw-HTML and rendered-DOM snapshot pair.

    This pure function uses neither the network nor a browser, allowing complete
    offline tests while the Playwright layer remains a thin adapter.
    """
    out: list[str] = []

    if shell:
        out.append(
            f'Raw HTML contains an empty <div id="{shell}"> mount point; the '
            "page is assembled entirely by JavaScript, so a non-rendering "
            "crawler receives an empty page"
        )
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


def render_check(
    url: str,
    timeout: float = 30.0,
    wait: str = "load",
    viewport: str = "desktop",
    *,
    request_gate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Compare a server response with the DOM produced after JavaScript executes.

    Playwright is optional. When unavailable, the tool returns ``ok: False`` and
    an installation command instead of misrepresenting an unperformed check.

    ``wait="load"`` is deliberate. ``networkidle`` may never occur on commercial
    sites because analytics, chat, and advertising keep connections open, turning
    a useful render check into a timeout. Search-engine rendering does not require
    complete network silence either. Callers may still request ``networkidle``
    when a particular application genuinely needs it.
    """
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
        validate_url(target)
    except ValueError as exc:
        return {"ok": False, "url": target, "error": str(exc)}
    try:
        _refuse_if_root()
    except RuntimeError as exc:
        return {"ok": False, "url": target, "error": str(exc)}

    # Fetch raw HTML with the regular client: this is what a non-rendering crawler receives.
    if request_gate is None:
        client, _ = http_client(timeout)
    else:
        client, _ = http_client(timeout, event_hooks={"request": [lambda _request: request_gate()]})
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
        }
    finally:
        client.close()

    size = VIEWPORT_PRESETS.get(viewport, VIEWPORT_PRESETS["desktop"])
    browser_client = None
    try:
        browser_client, _http2 = http_client(
            timeout, follow_redirects=False, headers={"User-Agent": UA}
        )
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
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
                    service_workers="block",
                    user_agent=UA,
                )
                context.add_init_script(_CLS_INIT_JS)
                route_handler, limitations = _pinned_browser_route(
                    browser_client, request_gate=request_gate
                )
                context.route("**/*", route_handler)
                # WebSockets are not HTTP requests and page.route() never sees
                # them either; route_web_socket is the separate interception
                # point that covers them.
                context.route_web_socket("**/*", _guard_websocket_route)
                page = context.new_page()
                page.goto(target, wait_until=wait, timeout=timeout * 1000)
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
    shell = detect_empty_shell(raw_html)
    findings = compare(raw, rendered, raw_html, shell)
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
        # Both snapshots were requested under this identity (#199) -- recorded so a report
        # can show its comparison is not confounded by a server that varies its response by
        # User-Agent, rather than leaving that an unstated assumption.
        "user_agent": UA,
        "raw": raw,
        "rendered": rendered,
        "empty_shell": shell,
        # Keep the summary aligned with findings: five widget words do not make a
        # page JavaScript-dependent, while findings use a 30% materiality threshold.
        "js_dependent": findings != [ALL_CLEAR],
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
            browser = pw.chromium.launch()
            try:
                context = browser.new_context(service_workers="block", user_agent=UA)
                try:
                    route_handler, limitations = _pinned_browser_route(
                        browser_client, request_gate=request_gate
                    )
                    context.route("**/*", route_handler)
                    context.route_web_socket("**/*", _guard_websocket_route)
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

    def _capture_request(request: Any) -> None:
        if max_html_bytes is None:
            return
        headers = request.all_headers()
        if any(
            headers.get(name)
            for name in ("authorization", "cookie", "proxy-authorization", "x-api-key")
        ):
            observed_policy["credentials_used"] = True

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
            browser = pw.chromium.launch()
            context = browser.new_context(**context_options)
            actual_browser = browser if browser is not None else getattr(context, "browser", None)
            engine_version = str(getattr(actual_browser, "version", "unknown"))
            try:
                route_handler, browser_limitations = _pinned_browser_route(
                    network_client, request_gate=request_gate
                )
                context.route("**/*", route_handler)
                context.route_web_socket("**/*", _guard_websocket_route)
                page = context.new_page()
                if max_html_bytes is not None:
                    page.on("request", _capture_request)
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
