"""Typed return shapes for the most-reused dict contracts in the toolkit.

Every dict below already exists at runtime: `seohead.tools.parser.parse_url`
and `seohead.tools.robots.check_robots` have returned exactly these keys since
before this module existed. Adding a `TypedDict` for them is purely additive —
it gives a type checker (and a reader) something to verify a caller against,
without changing what either function returns or asking any caller to change.

`TypedDict` was chosen over a dataclass or a Pydantic model for this first
slice because both call sites are already deep in the codebase (crawl,
recon, the CLI/MCP handler layer) passing plain dicts around; a `TypedDict`
describes that same dict, at zero runtime cost, with no migration required.
A Pydantic model remains the right choice where runtime validation at a
boundary is wanted (see issue #31) — that trade-off should stay a deliberate
per-shape decision, not a blanket rule.

Two shapes are covered here, in order of reuse:

- ``ParseResult`` (via ``ParseFetched`` / ``ParseFailed``): the return of
  ``parser.parse_url`` / ``parser.parse_html``, reused by
  ``seohead.tools.page_facts``, ``seohead.tools.links``,
  ``seohead.crawl.collect``, ``seohead.recon.backlinks``,
  ``seohead.recon.regions``, and the ``parse`` / ``citability_check`` /
  ``social_meta_check`` handlers — the single most-shared shape in the package.
- ``RobotsCheckResult`` (via ``RobotsCheckFound`` / ``RobotsCheckError``): the
  return of ``robots.check_robots``, the ``robots_check`` handler's shape and
  a representative "ok/error envelope with a couple of optional keys"
  pattern that recurs across most of the other 42 handlers.
"""

from __future__ import annotations

import sys
from typing import Any, Literal

if sys.version_info >= (3, 12):
    from typing import TypedDict
else:  # pragma: no cover - exercised by the 3.10/3.11 CI jobs
    # Pydantic refuses typing.TypedDict below 3.12: on those versions the
    # runtime cannot see which keys are required, so a shape used in an MCP
    # tool signature would be validated against nothing. typing_extensions
    # backports the __required_keys__ machinery pydantic needs.
    from typing_extensions import TypedDict


class _LinkInfoOptional(TypedDict, total=False):
    # Only present when the caller opts in via options["classify_links"]=True
    # (off by default) — see parser.parse_html / tools/link_position.py.
    position: str


class LinkInfo(_LinkInfoOptional):
    """One `<a href>` extracted from a page."""

    href: str
    # The href attribute exactly as written, before resolution against the document base —
    # the only place a protocol-relative ("//host/path") form is still visible (issue #125).
    raw_href: str
    text: str
    rel: str
    # The anchor's own target attribute (e.g. "_blank"), "" when absent. Kept alongside `rel`
    # so a crawl can tell a cross-origin new-tab link from an ordinary one (issue #125).
    target: str
    nofollow: bool
    external: bool


class HreflangAlternate(TypedDict):
    """One `<link rel="alternate" hreflang="...">` declaration (issue #357).

    The code and the href are kept exactly as the document wrote them: a code
    with the wrong case or a malformed region is itself a finding, and
    normalising on capture would hide it. ``url`` is that same href resolved
    against the document base -- what a browser does -- so a reciprocity check
    can compare targets without the original being lost.
    """

    lang: str
    raw_href: str
    url: str


class FrameInfo(TypedDict):
    """One `<iframe>` extracted from a page (issue #360).

    A framed document is not part of the parent DOM, so every text-derived
    measurement -- word count, headings, the content area -- describes the shell
    around the content rather than the content. Recording what a page frames is
    what lets a check say "the copy is in an iframe" instead of "this page is
    thin", which is the same fact read the wrong way round.
    """

    # Resolved against the document base; "" when the src attribute is absent
    # (a JavaScript-populated frame, which is still a frame).
    src: str
    # The src exactly as written, before resolution -- the only place a
    # protocol-relative or empty form stays visible.
    raw_src: str
    # Same registrable scope as the page, by the same rule links use. A
    # same-origin frame holds content the site owns; a third-party frame is
    # usually an embed and is judged differently.
    same_origin: bool
    # Whether the frame sits inside the resolved content area. An iframe in a
    # footer is a widget; an iframe in the content area is the page's content.
    in_content_area: bool
    title: str
    loading: str
    sandbox: str


class ImageInfo(TypedDict):
    """One `<img>` as the coverage checks need it (issues #385, #386).

    ``has_alt`` is whether the attribute is present at all, which is a different
    fact from its length: ``alt=""`` is a deliberate mark on a decorative image
    and must stay silent, while a missing attribute is the finding. Collapsing
    the two into one truthy value is how a correctly marked-up site gets told to
    add alt text it already declined on purpose.
    """

    has_alt: bool
    alt_length: int


class FormInfo(TypedDict):
    """One `<form>` extracted from a page (issue #125)."""

    method: str
    # Resolved against the document base; the page's own URL when the attribute is absent or
    # empty, per the HTML standard's own default for form submission.
    action: str
    has_password: bool


class _ParsedPageOptional(TypedDict, total=False):
    # Only present when the caller opts in via options["url_sources"]=True
    # (off by default) — see parser.parse_html.
    url_sources: list[dict[str, str]]


class DocumentPosition(TypedDict):
    """Where key elements sit relative to `<head>`, as `parser.parse_html`'s
    parse tree resolved them -- not as the source text suggests (issue #123).

    A browser closes `<head>` at the first element that does not belong there,
    so a canonical or robots directive placed after one silently stops
    applying; the page still reads fine in the source. Each `*_outside_head`
    flag is `None` when the element is simply absent -- a different, already
    covered finding -- and a bool only once it exists.
    """

    head_count: int
    body_count: int
    head_not_first: bool
    invalid_head_elements: list[str]
    title_outside_head: bool | None
    meta_description_outside_head: bool | None
    canonical_outside_head: bool | None
    directives_outside_head: bool | None
    hreflang_outside_head: bool | None


class ParsedPage(_ParsedPageOptional):
    """The on-page fields `parser.parse_html` extracts (pure, no network)."""

    title: str | None
    meta_description: str | None
    robots: str | None
    robots_meta: list[str]
    # Same values as robots_meta, prefixed "<name>: " for every tag but the
    # generic one -- see parser.robots_meta_scoped. Native-crawl evidence joins
    # this, not robots_meta, so a Bingbot/Yandex-only directive keeps its scope.
    robots_meta_scoped: list[str]
    canonical: str | None
    position: DocumentPosition
    # Static Lighthouse audits (issue #59) — see seohead.sf.core.rules and
    # seohead.sf.core.lighthouse for the correspondence and doc links.
    charset: str | None
    doctype: str | None
    viewport: str | None
    # The <meta http-equiv="refresh"> content attribute exactly as written,
    # "" when the page declares none -- the same fact SF's Meta Refresh 1
    # column carries, so one check reads it whichever source produced it.
    meta_refresh: str
    og: dict[str, str]
    twitter: dict[str, str]
    headings: dict[str, list[str]]
    jsonld: list[Any]
    jsonld_invalid: list[dict[str, Any]]
    links: list[LinkInfo]
    forms: list[FormInfo]
    text: str
    # The whole body, and the content area alone. word_count follows the
    # content area, because a nav-and-footer word count describes the template
    # rather than the page.
    content_text: str
    content_area_strategy: str
    word_count: int
    # Always extracted: see parse_html. Empty when the page declares none.
    hreflang: list[HreflangAlternate]
    # Every <iframe> the document declares -- see FrameInfo. Empty when the
    # text option is off, because in_content_area needs the resolved root.
    frames: list[FrameInfo]
    # How many <applet>/<embed>/<object> elements the page carries, for the
    # unsupported-plugin check. Always computed: it is one tree lookup, and a
    # key the parser returns but this model does not declare is a contract
    # the typed-handler gate refuses.
    plugin_elements_count: int
    # Per-image alt facts -- see ImageInfo. Empty when the page has no images.
    images: list[ImageInfo]
    # Runs of boilerplate placeholder prose found in the content area.
    lorem_ipsum_count: int
    # The first H1's text when it comes only from an image's alt attribute,
    # None when the heading carries text of its own. A logo inside a text H1
    # is not an image-only heading, which is why this is a value and not a flag.
    h1_alt_only_text: str | None
    # How many live <meta name="description"> tags the document declares.
    meta_description_count: int


class ParseFetched(ParsedPage):
    """`parser.parse_url` once the request itself completed: page fields plus
    fetch metadata. ``ok`` mirrors the HTTP response (``response.is_success``)
    and can be ``False`` here too — e.g. a clean 404 still parses the body."""

    url: str
    final_url: str
    status_code: int
    ok: bool


class ParseFailed(TypedDict):
    """`parser.parse_url` when the request itself raised (network, timeout,
    invalid URL, ...) before any response existed to parse."""

    url: str
    ok: Literal[False]
    error: str


# parser.parse_url's actual return type: one or the other, never a mix.
ParseResult = ParseFetched | ParseFailed


class ParseManyResult(TypedDict):
    """The `parse` handler's return: one `ParseResult` per requested URL."""

    count: int
    results: list[ParseResult]


class RobotsGroup(TypedDict):
    """One `User-agent:` group from a parsed robots.txt."""

    user_agents: list[str]
    allow: list[str]
    disallow: list[str]
    crawl_delay: float | None


class ParsedRobots(TypedDict):
    """`robots.parse_robots`'s pure parse of robots.txt content."""

    groups: list[RobotsGroup]
    sitemaps: list[str]


class RobotsPathCheck(TypedDict):
    path: str
    allowed: bool


class _RobotsCheckFoundOptional(TypedDict, total=False):
    # Present only on the "no robots.txt" branch (status >= 400).
    note: str
    # Present only when the caller passed `paths`.
    path_checks: list[RobotsPathCheck]


class RobotsCheckFound(_RobotsCheckFoundOptional):
    """`robots.check_robots` once the robots.txt request itself succeeded
    (whether or not a robots.txt actually exists — see `exists`)."""

    ok: Literal[True]
    robots_url: str
    status_code: int
    exists: bool
    groups: list[RobotsGroup]
    sitemaps: list[str]


class _RobotsCheckErrorOptional(TypedDict, total=False):
    # An HTTP response was received but its rules could not be read.
    status_code: int


class RobotsCheckError(_RobotsCheckErrorOptional):
    """`robots.check_robots` when fetching robots.txt itself failed."""

    ok: Literal[False]
    robots_url: str
    error: str


# robots.check_robots's actual return type: one or the other, never a mix.
RobotsCheckResult = RobotsCheckFound | RobotsCheckError
