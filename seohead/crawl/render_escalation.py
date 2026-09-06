"""Selective JavaScript-rendering escalation over an already-completed static
crawl, and the pre-flight gate that stops an empty shell or a link-less start
page from reaching a health score.

Rendering costs roughly an order of magnitude more per URL than a static
fetch (#18), so this module never renders the whole crawl. It samples a
handful of URLs per detected *template pattern* rather than per URL, decides
per pattern whether the static and rendered document diverge enough to
matter, and only then re-fetches the fuller representation for pages that
share an escalated pattern -- inside its own, separate budget.

Both the sampling probe and the full re-fetch are injected callables. That
is what keeps this module testable without a browser or the network: the
production caller (``seohead.servers.handlers.crawl_site``) binds them to
``seohead.tools.render``'s Playwright-backed functions; a test binds them to
plain functions returning canned data.

Which fuller representation is fetched -- executing JavaScript, or honouring
the legacy ``_escaped_fragment_`` scheme -- is the caller's business, not
this module's: ``escalate()`` only asks "does this pattern need a fuller
fetch" and "go get it", and records whatever label the caller passes as the
representation that produced each page's evidence going forward. That
recording is the point of #18's central rule: raw and rendered numbers are
not comparable, so every finding must say which one produced it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit


class _HasUrl(Protocol):
    url: str


# A path segment shaped like this is structurally an identifier -- a number, a
# UUID, or a date -- no matter where in the path it sits, because nothing
# about those shapes is a word a person would choose for a single static
# page. Collapsing it is what lets two pages of one template share a single
# pattern key, so a hundred product pages are sampled as one pattern instead
# of a hundred.
_ID_SEGMENT_RE = re.compile(
    r"^(?:\d+"
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\d{4}-\d{2}-\d{2})$"
)
# A long hyphenated slug (`how-to-fix-pumps`) is *not* structurally an
# identifier the way a number or UUID is -- "documentation" and
# "case-studies" are exactly this shape too, and are distinct static pages,
# not interchangeable members of one template. What actually distinguishes a
# template instance from a hand-written page is a shared parent: `/blog/<
# slug>` and `/product/<slug>` mean many pages sit under one template
# segment, while a bare `/<slug>` at the root is indistinguishable from any
# other page name. So this alternative only fires on a segment that has a
# preceding sibling segment in the path -- see `_is_identifier_segment`.
_SLUG_SEGMENT_RE = re.compile(r"^[\w-]{9,}$")


def _is_identifier_segment(segment: str, *, has_parent: bool) -> bool:
    if _ID_SEGMENT_RE.match(segment):
        return True
    return has_parent and bool(_SLUG_SEGMENT_RE.match(segment))


def url_pattern(url: str) -> str:
    """Collapse a URL's identifier-shaped path segments into a template key.

    A heuristic, not a template engine. Query string and fragment are
    dropped entirely: they vary per item at least as often as path segments
    do, and keeping them would turn "one pattern per template" back into
    "one pattern per URL". This groups `/product/wireless-mouse` with
    `/product/bluetooth-speaker` (a shared parent, so the slug is a per-item
    identifier) but leaves `/documentation` and `/contact-us` apart (no
    parent segment, so each root-level slug is its own page).
    """
    parts = urlsplit(url)
    raw_segments = parts.path.split("/")
    segments = []
    has_named_parent = False
    for seg in raw_segments:
        if seg and _is_identifier_segment(seg, has_parent=has_named_parent):
            segments.append("*")
        else:
            segments.append(seg)
        if seg:
            has_named_parent = True
    return urlunsplit((parts.scheme, parts.netloc, "/".join(segments), "", ""))


def select_samples(urls: Iterable[str], sample_per_pattern: int) -> dict[str, list[str]]:
    """Group URLs by pattern and keep only the first N of each for probing."""
    n = max(1, int(sample_per_pattern))
    groups: dict[str, list[str]] = {}
    for u in urls:
        groups.setdefault(url_pattern(u), []).append(u)
    return {pattern: members[:n] for pattern, members in groups.items()}


@dataclass
class GateResult:
    """Whether a run is a false-green that must not reach a health score."""

    requires_rendering: bool = False
    reason: str = ""


def start_page_gate(start_url: str, internal_outlinks: int, start_html: str) -> GateResult:
    """Catch the two shapes of "fetched fine, proved nothing" before scoring.

    An empty shell fetches cleanly, yields one URL, and produces a
    clean-looking audit -- exactly the false-green #18 asks this gate to
    stop. Both checks here are static-only: an empty SPA shell is a raw-HTML
    regex match, and the outlink count comes from the ordinary parse of the
    start page. Neither needs a browser, so this gate applies even to a
    ``rendering.mode: "raw"`` run -- the one that most needs it, since raw
    mode has no render to fall back on.
    """
    if internal_outlinks <= 0:
        return GateResult(True, f"the start URL yielded zero internal links: {start_url}")
    if start_html:
        from seohead.tools.render import detect_empty_shell

        shell = detect_empty_shell(start_html)
        if shell:
            return GateResult(
                True,
                f'the start URL is a detected empty SPA shell (<div id="{shell}">): {start_url}',
            )
    return GateResult(False, "")


@dataclass
class EscalationResult:
    schema_version: str = "render_escalation.v1"
    mode: str = "raw"
    patterns_sampled: int = 0
    # Patterns whose probe sample said "needs a fuller fetch" -- a judgement,
    # not a promise that every page in it was re-fetched. See render_counts
    # and patterns_partially_rendered for what the render budget actually did
    # with that judgement.
    patterns_escalated: list[str] = field(default_factory=list)
    # Request counts are how "selective" is proven rather than asserted: a
    # crawl of 500 URLs across 12 patterns should show on the order of 24
    # probe requests, not 500 -- see the acceptance criterion in #18.
    probe_requests: int = 0
    render_requests: int = 0
    render_budget_exhausted: bool = False
    # Set when rendering.escalation.max_render_seconds ran out during this call, whether
    # that happened while sampling or while rendering (#198). Kept distinct from
    # render_budget_exhausted, which means max_render_urls hit zero: a report needs to say
    # which budget cut the run short, since one is a URL count the operator set and the
    # other is a clock the operator set, and they run out for unrelated reasons.
    time_budget_exhausted: bool = False
    # Patterns the deadline reached before their sample was even probed -- distinct from
    # patterns_escalated (never got a verdict at all, so they are absent from that list too).
    patterns_unprobed: list[str] = field(default_factory=list)
    # pattern -> how many of its pages actually reached render_fetch(). A
    # pattern in patterns_escalated with no entry here got zero -- the exact
    # corruption #147 found: an escalated pattern indistinguishable in the
    # summary from one that was fully rendered.
    render_counts: dict[str, int] = field(default_factory=dict)
    # Escalated patterns the render budget ran out before finishing, sorted.
    # A pattern absent from this list either was not escalated, or had every
    # one of its pages rendered -- the two states patterns_escalated alone
    # cannot tell apart.
    patterns_partially_rendered: list[str] = field(default_factory=list)
    empty_shell_urls: list[str] = field(default_factory=list)
    # url -> the representation that produced its evidence going forward.
    representations: dict[str, str] = field(default_factory=dict)
    # url -> whatever render_fetch() returned for it (html, final_url, ...).
    rendered: dict[str, dict[str, Any]] = field(default_factory=dict)
    # URLs where render_fetch() returned ok:True but the parsed body cleared
    # none of apply_rendered_evidence()'s floor -- a client-side crash after
    # load, a cookie wall, a script blocked by an extension, an app that
    # never hydrates. The raw record is kept for these (#143); this list is
    # what makes the downgrade-that-didn't-happen auditable instead of silent.
    degenerate_render_urls: list[str] = field(default_factory=list)


def escalate(
    pages: Iterable[_HasUrl],
    rendering_config: dict[str, Any],
    *,
    probe: Callable[[str], dict[str, Any]],
    render_fetch: Callable[[str], dict[str, Any]],
    representation_label: str,
    render_consumer: Callable[[str, dict[str, Any], str], dict[str, Any] | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> EscalationResult:
    """Sample, decide, and selectively re-fetch -- see the module docstring.

    ``probe(url)`` answers "does this URL's pattern need a fuller fetch": it
    must return a dict with ``ok`` and ``needs_escalation``, and may include
    ``empty_shell`` (an SPA mount-point id, or a falsy value). ``render_fetch
    (url)`` performs the fuller fetch for one page and must return a dict
    with ``ok`` and, when ``ok``, ``html``. Both are entirely the caller's
    business -- this function only counts requests and applies the two
    budgets (patterns sampled, then URLs rendered).

    ``rendering.escalation.max_render_seconds`` (0 = unlimited) is a single wall-clock
    deadline for the whole call, checked before every ``probe`` and every ``render_fetch`` --
    documented as covering "the escalation step", not the render phase alone, so probing eats
    into the same budget a slow site's renders would otherwise exhaust on their own. ``clock``
    is injectable so a test can advance a fake one from inside a fake ``render_fetch`` instead
    of sleeping in real time.
    """
    urls = [p.url for p in pages]
    result = EscalationResult(mode=rendering_config.get("mode", "raw"))
    for u in urls:
        result.representations[u] = "static"

    escalation_cfg = rendering_config.get("escalation", {})
    samples = select_samples(urls, escalation_cfg.get("sample_per_pattern", 1))
    result.patterns_sampled = len(samples)

    max_render_seconds = float(escalation_cfg.get("max_render_seconds", 0) or 0)
    deadline = clock() + max_render_seconds if max_render_seconds > 0 else None

    def time_left() -> bool:
        return deadline is None or clock() < deadline

    escalated: set[str] = set()
    probed_patterns: set[str] = set()
    for pattern, sample_urls in samples.items():
        if not time_left():
            break
        needs_it = False
        for sample_url in sample_urls:
            if not time_left():
                break
            probed = probe(sample_url)
            result.probe_requests += 1
            if not probed.get("ok"):
                continue
            if probed.get("empty_shell"):
                result.empty_shell_urls.append(sample_url)
            if probed.get("needs_escalation"):
                needs_it = True
        else:
            probed_patterns.add(pattern)
            if needs_it:
                escalated.add(pattern)
            continue
        break  # the inner loop above ran out of time before finishing this pattern's sample
    result.patterns_escalated = sorted(escalated)
    result.patterns_unprobed = sorted(set(samples) - probed_patterns)
    if not time_left():
        result.time_budget_exhausted = True
    if not escalated:
        return result

    by_pattern: dict[str, list[str]] = {}
    for u in urls:
        by_pattern.setdefault(url_pattern(u), []).append(u)

    # Spent breadth-first, one URL per escalated pattern per round, rather
    # than draining patterns_escalated in order: sequential spending let
    # whichever pattern sorted first consume the whole budget, leaving every
    # later pattern at zero renders while still calling it "escalated" (#147).
    # Round-robin instead means a budget that covers at least one URL per
    # pattern reaches every pattern; render_counts and
    # patterns_partially_rendered record honestly what a smaller budget could
    # not finish, instead of the summary claiming a fuller fetch that never
    # ran.
    queues = {pattern: list(by_pattern.get(pattern, [])) for pattern in result.patterns_escalated}
    budget = int(escalation_cfg.get("max_render_urls", 0))
    active = [pattern for pattern in result.patterns_escalated if queues[pattern]]
    while active and budget > 0 and time_left():
        next_active = []
        for pattern in active:
            if budget <= 0 or not time_left():
                break
            target_url = queues[pattern].pop(0)
            fetched = render_fetch(target_url)
            result.render_requests += 1
            budget -= 1
            result.render_counts[pattern] = result.render_counts.get(pattern, 0) + 1
            if render_consumer is None:
                if fetched.get("ok"):
                    result.representations[target_url] = representation_label
                    result.rendered[target_url] = fetched
            else:
                consumed = render_consumer(target_url, fetched, representation_label) or {}
                accepted = bool(fetched.get("ok")) and bool(consumed.get("accepted"))
                if accepted:
                    result.representations[target_url] = representation_label
                result.rendered[target_url] = {
                    "ok": bool(fetched.get("ok")),
                    "final_url": fetched.get("final_url") or target_url,
                    "renderer": fetched.get("renderer") or {},
                    "capture": {
                        "accepted": accepted,
                        "state": str(consumed.get("state") or "unavailable"),
                        "reason": str(consumed.get("reason") or ""),
                    },
                }
            if queues[pattern]:
                next_active.append(pattern)
        active = next_active
    result.patterns_partially_rendered = sorted(
        pattern for pattern, remaining in queues.items() if remaining
    )
    result.render_budget_exhausted = bool(result.patterns_partially_rendered)
    if not time_left():
        result.time_budget_exhausted = True
    return result


def _clears_content_floor(record: Any) -> bool:
    """A non-trivial word count, or at least one of title/h1/canonical present.

    The same ``EMPTY_BODY_WORDS``-style reasoning
    ``seohead.tools.render.detect_empty_shell`` already applies to an empty
    SPA shell, reused here as the minimum signal that a ``PageRecord`` (raw
    or freshly re-derived from a render) describes a real page rather than a
    blank one -- see ``apply_rendered_evidence``.
    """
    from seohead.tools.render import EMPTY_BODY_WORDS

    return bool(
        record.word_count >= EMPTY_BODY_WORDS or record.title or record.h1 or record.canonical
    )


# PageRecord fields apply_rendered_evidence must never touch: identity/transport
# facts of the *static* fetch (url, status_code, ...), the two outlink counts
# (recomputed below as the raw/rendered union, never a plain overwrite), and
# the bookkeeping fields this function itself decides (error, cache_status,
# representation). Every other field is body-derived and belongs to whichever
# body last produced it -- see _apply_body.
_RENDER_UNTOUCHED_FIELDS = frozenset(
    {
        "url",
        "status_code",
        "content_type",
        "response_time",
        "redirect_url",
        "x_robots",
        "content_encoding",
        "crawl_depth",
        "outlinks",
        "external_outlinks",
        "error",
        "cache_status",
        "representation",
    }
)


def apply_rendered_evidence(
    pages: list[Any],
    raw_links: list[Any],
    escalation: EscalationResult,
    *,
    parse_options: dict[str, Any] | None = None,
    max_response_bytes: int | None = None,
) -> None:
    """Fold each re-fetched page's fuller HTML back into its ``PageRecord``.

    Every body-derived field is filled in by ``_apply_body`` -- the same
    function a live fetch and a cache hit already share (#99) -- instead of
    a second, hand-rolled copy of what it does. That is what #139 found
    missing: calling ``_record_from_parsed`` directly here left `size_bytes`,
    `text_ratio`, `jsonld_blocks_found` and `jsonld_blocks_parsed` at the
    static fetch's values while `title` and `word_count` moved on.

    ``size_bytes`` for a rendered page is the length of the DOM Playwright
    serialized back to us, not "bytes on the wire" (#99's original sense) --
    a rendered document was never transferred as this string, so there is no
    wire size to report for it. The serialized length is the only honest
    stand-in, and it is exactly what ``_apply_body`` already falls back to
    whenever no real byte count is supplied, so no ``size_bytes`` argument is
    passed through here on purpose.

    A render is only rejected as failed when it takes a record that already
    showed a real page -- a non-trivial word count, or at least one of
    title/h1/canonical present, the same ``EMPTY_BODY_WORDS``-style
    reasoning ``seohead.tools.render.detect_empty_shell`` already applies to
    an empty SPA shell -- and produces a body that clears none of those
    signals. That comparison is deliberately one-sided: a raw record that
    was already an empty shell has nothing left to lose, so its rendered
    replacement is applied even when it too is thin (that is exactly #139's
    case -- rendering an empty static shell into a real, if imperfect,
    document). What must never happen is the other direction: a raw record
    that already carried real content being overwritten by an emptier
    rendered one (#143). A page that is merely thinner after rendering while
    still clearing the floor is not failed -- it is a real finding (JS
    hydration removing content a non-rendering crawler cannot see), and it
    must still reach the report as the rendered numbers, not be silently
    kept as the raw ones. A render that fails this test, or whose body fails
    to parse at all (``_apply_body`` returns ``None``), is treated as
    failed: `representation` and every raw field are left exactly as the
    static fetch produced them, and the URL is recorded in
    ``degenerate_render_urls`` so the downgrade-that-didn't-happen is
    auditable instead of silent.

    Outlinks are the union of what the raw HTML and the fuller fetch each
    found, never the fuller fetch alone: a link hydration removes is a real
    finding (#18), not a link that never existed, and dropping it here would
    make that finding invisible to every check built on outlink counts.

    That union changes ``PageRecord.outlinks`` but, on its own, leaves
    ``raw_links`` -- the sole source ``build_evidence`` reads for the
    ``all_inlinks`` frame -- exactly as the static crawl left it. Every graph
    check (anchor text, inlink composition, link score, ...) reads that frame,
    not the outlink count, so a rendered-only href reaching a page the crawl
    already collected must also become a ``LinkEdge`` here, mutating
    ``raw_links`` in place, or the graph a rendered page claims to have
    measured stays invisible to everything built on it (#245). This is
    evidence about a link the page already carries, not new crawl work: it
    never enqueues a destination the static crawl had not already reached.
    """
    by_url = {p.url: p for p in pages}
    for target_url, fetched in escalation.rendered.items():
        record = by_url.get(target_url)
        if record is None:
            continue
        _parsed, degenerate = fold_rendered_evidence(
            record,
            raw_links,
            target_url,
            fetched,
            escalation.representations.get(target_url, "static"),
            parse_options=parse_options,
            max_response_bytes=max_response_bytes,
        )
        if degenerate:
            escalation.degenerate_render_urls.append(target_url)


def fold_rendered_evidence(
    record: Any,
    raw_links: list[Any],
    target_url: str,
    fetched: dict[str, Any],
    representation: str,
    *,
    parse_options: dict[str, Any] | None = None,
    max_response_bytes: int | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Apply one DOM under the established content-floor and union policy.

    The native SQLite path calls this for one source at a time.  Keeping the
    fold here prevents a separately implemented DOM parser from drifting from
    the directory collector's raw-plus-rendered evidence rules.
    """
    from dataclasses import replace
    from urllib.parse import urlsplit

    from seohead.crawl.collect import _apply_body
    from seohead.crawl.spider import LinkEdge

    html = fetched.get("html")
    if not html:
        return None, False
    final_url = fetched.get("final_url") or target_url
    scratch = replace(record)
    scratch.content_type = scratch.content_type or "text/html"
    parsed = _apply_body(
        scratch,
        final_url,
        html,
        parse_options=parse_options,
        **({"max_response_bytes": max_response_bytes} if max_response_bytes is not None else {}),
    )
    rendered_has_content = parsed is not None and _clears_content_floor(scratch)
    if _clears_content_floor(record) and not rendered_has_content:
        return parsed, True
    if parsed is None:
        return None, True

    record.representation = representation
    for f in fields(record):
        if f.name not in _RENDER_UNTOUCHED_FIELDS:
            setattr(record, f.name, getattr(scratch, f.name))

    raw_hrefs = {edge.destination for edge in raw_links if edge.source == target_url}
    rendered_hrefs = {link["href"] for link in parsed.get("links") or []}
    merged = raw_hrefs | rendered_hrefs
    host = (urlsplit(final_url).hostname or "").lower()
    record.outlinks = len(merged)
    record.external_outlinks = sum(
        1 for href in merged if (urlsplit(href).hostname or "").lower() != host
    )
    new_hrefs = rendered_hrefs - raw_hrefs
    if new_hrefs:
        by_href = {link["href"]: link for link in parsed.get("links") or []}
        for href in new_hrefs:
            link = by_href.get(href, {})
            raw_links.append(
                LinkEdge(
                    source=target_url,
                    destination=href,
                    anchor=(link.get("text") or "")[:200],
                    nofollow=bool(link.get("nofollow")),
                    position=link.get("position") or "",
                )
            )
    return parsed, False
