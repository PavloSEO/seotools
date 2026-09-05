"""Resolve a configurable content area for word counts, duplicate and language checks.

Word count, thin-content detection, and duplicate comparison have historically
run over the whole document, so a 40-word product page with a 600-word
mega-menu reads as substantial, and every page on a site looks similar to
every other because they share the same navigation and footer. This module
scopes text extraction to a defined content area instead.

It deliberately touches nothing about link discovery: restricting the content
area is a statement about text, not about which links exist on the page.
Callers that need both must run link extraction over the untouched document
and pass only the resolved root here.

Configuration (all keys optional, passed as one dict):
  include_selector  -- CSS selector naming the main region directly (positive
      selection). Wins over ``root_selector`` when it matches an element.
  root_selector     -- CSS selector for the region to scope exclusions within.
      Defaults to the document ``<body>``.
  exclude_tags      -- tag names removed from the resolved root before text is
      read. Defaults to ``DEFAULT_EXCLUDE_TAGS`` (nav, header, aside, footer);
      pass ``[]`` to keep everything.
  exclude_selectors -- CSS selectors (by class, id, or anything else) removed
      the same way, for boilerplate that is neither a ``<nav>`` nor a
      ``<footer>``.

With no selector configured the region is detected from the document's own semantics —
``<main>``, ``[role="main"]``, then ``<article>`` — rather than defaulting to the whole body.
HTML5 defines those elements for exactly this, so this is a documented contract rather than a
heuristic, and the strategy names which one matched so a reader can disagree with it. On a live
WordPress post the whole-body default counted 433 words where ``main`` counts 429: 126 of them
(29%) were template text, including a skip-to-content link and the word "header" (issue #96).

This module is pure and performs no network access.
"""

from __future__ import annotations

from copy import copy
from typing import Any

from bs4 import BeautifulSoup, Tag

# Menus, mastheads, sidebars and footers are boilerplate, not content, on most sites. Sites
# that name theirs differently use exclude_selectors instead. header and aside are here because
# stripping only nav and footer missed every masthead, breadcrumb bar and promo block that is
# not wrapped in one of those two elements — on the page measured in #96, neither the skip link
# nor the header block was inside a <nav>.
DEFAULT_EXCLUDE_TAGS = ("nav", "header", "aside", "footer")

# Tried in order when nothing is configured. HTML5 gives <main> (and its ARIA equivalent) for
# the document's main content and <article> for a self-contained item within it; a CMS that
# emits semantic markup gets a correct answer with no configuration at all.
AUTO_STRATEGIES = (
    ("main", "auto_main"),
    ('[role="main"]', "auto_role_main"),
    ("article", "auto_article"),
)

# Elements never part of rendered body text, wherever in the document they appear: <script>/
# <style> carry no copy, <noscript> only renders with scripting off (it still carries real,
# discoverable links -- see parser._INERT_LINK_CONTAINERS), <template> is inert per the HTML
# spec, and <svg>/<math> are visible graphics whose descendant text (an icon sprite's
# accessibility <title>, glyph <text>/<tspan>, MathML notation) is a label or notation, not
# body prose. This is the one place that answer lives: ``extract_area_text`` below and
# ``parser._extract_text`` both decompose exactly this set, rather than keeping two lists that
# silently drift (which is how issue #140 happened -- a fix to one text extractor did not
# reach the other, scoped-content-area one that actually feeds ``word_count``).
TEXT_EXCLUDED_TAGS = ("script", "style", "noscript", "template", "svg", "math")


def _strip(root: Tag, exclude_tags: Any, exclude_selectors: Any) -> None:
    """Remove excluded elements from ``root`` in place."""
    for tag_name in exclude_tags or ():
        for el in root.find_all(tag_name):
            el.decompose()
    for selector in exclude_selectors or ():
        for el in root.select(selector):
            el.decompose()


def find_content_root(soup: BeautifulSoup, config: dict[str, Any] | None = None) -> tuple[Tag, str]:
    """Return ``(root, strategy)`` for the configured content area, on the live tree.

    This is the selection half of :func:`resolve_content_area`, split out so a
    caller that needs to test descendant membership (link position
    classification: "is this link inside the content area?") can do so by
    identity against the actual document, rather than against the detached,
    stripped copy that word-count and duplicate-detection extraction need.
    Nothing here mutates ``soup``.

    ``strategy`` records how the region was picked so a wrong or missing
    selector is visible per page rather than silently falling back:
      "include_selector"      -- include_selector was given and matched.
      "root_selector"         -- root_selector was given (no include_selector
                                  or it did not match) and matched.
      "auto_main"             -- no selector configured; a <main> matched.
      "auto_role_main"        -- no selector configured; a [role="main"] matched.
      "auto_article"          -- no selector configured; an <article> matched.
      "default_body"          -- no selector configured and none of the three
                                  semantic containers is present.
      "fallback_default_body" -- a selector was configured but matched
                                  nothing, so the whole body was used instead.

    Configuration always wins over detection: an explicit selector is an override, not the
    only way to get a sane answer.
    """
    config = config or {}
    include_selector = config.get("include_selector")
    root_selector = config.get("root_selector")

    # ``parser`` imports this module, so keep the shared inert-template helper
    # local. Selection happens after both modules have initialized and must use
    # the same definition as every other document-evidence reader.
    from seohead.tools.parser import is_inert_template_content

    def first_visible(selector: str) -> Tag | None:
        return next(
            (
                candidate
                for candidate in soup.select(selector)
                if not is_inert_template_content(candidate)
            ),
            None,
        )

    requested_but_missing = False

    if include_selector:
        match = first_visible(include_selector)
        if match is not None:
            return match, "include_selector"
        requested_but_missing = True

    if root_selector:
        match = first_visible(root_selector)
        if match is not None:
            return match, "root_selector"
        requested_but_missing = True

    if not requested_but_missing:
        # Nothing was asked for, so ask the document. Only reached when neither selector was
        # configured — a configured-but-missing selector falls straight through to the body,
        # because silently substituting a different region for the one that was named would
        # hide the mistake this strategy field exists to show.
        for selector, strategy in AUTO_STRATEGIES:
            match = first_visible(selector)
            if match is not None:
                return match, strategy

    strategy = "fallback_default_body" if requested_but_missing else "default_body"
    return soup.body or soup, strategy


def resolve_content_area(
    soup: BeautifulSoup, config: dict[str, Any] | None = None
) -> tuple[Tag, str]:
    """Return ``(content_root, strategy)`` for the configured content area.

    ``content_root`` is a detached copy: it can be decomposed freely without
    disturbing the tree used for link discovery, which never passes through
    this function.
    """
    config = config or {}
    exclude_tags = config.get("exclude_tags", DEFAULT_EXCLUDE_TAGS)
    exclude_selectors = config.get("exclude_selectors")

    live_root, strategy = find_content_root(soup, config)
    root = copy(live_root)
    _strip(root, exclude_tags, exclude_selectors)
    return root, strategy


def extract_area_text(root: Tag) -> str:
    """Collapsed visible text of an already-resolved content root."""
    root = copy(root)
    for tag in root.find_all(list(TEXT_EXCLUDED_TAGS)):
        tag.decompose()
    return " ".join(root.get_text(" ").split())
