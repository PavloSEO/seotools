"""Render a page as Markdown, in two scopes: content-area only and full document.

A word count or a hash tells you *that* a page changed; neither is worth
diffing between crawls, feeding to content scoring, or handing to a model
directly. Markdown with structure preserved (headings, lists, links) is.

Two renderings answer different questions:
  - ``content_markdown`` -- boilerplate stripped, via ``content_area.py``.
    This is the representation worth diffing, scoring, or feeding to a model.
  - ``full_markdown`` -- the whole document, header and footer included, for
    reading. It is *not* an input to ``boilerplate_report.py``: that module
    hashes the tag structure of a template (see ``boilerplate_hash``), and this
    rendering has already discarded it. Pass it the original HTML, or a hash
    precomputed from that HTML, instead.

The converter below is intentionally small and handles only the tags visible
body text realistically uses (headings, paragraphs, lists, links, emphasis).
It is our own code rather than a third-party extractor on purpose: a
third-party converter's output is version-dependent, which would either pull
Markdown text outside the determinism gate or require pinning and recording a
version. A deterministic, dependency-free renderer avoids the trade-off.

This module is pure and performs no network access.
"""

from __future__ import annotations

from bs4 import BeautifulSoup, NavigableString, Tag

from seohead.tools.content_area import resolve_content_area

_BLOCK_TAGS = {"p", "div", "section", "article", "li", "blockquote"}
_HEADING_LEVELS = {f"h{i}": i for i in range(1, 7)}
# A semantic wrapper (<article>, <section>, a bare <div>) is itself one of
# _BLOCK_TAGS, so it used to be handed straight to _inline(), which has no
# notion of headings or lists and just concatenates every descendant's text
# -- silently discarding the "#"/"-" markers of anything nested inside it
# (issue #230). A tag in this set means "structure worth its own line or
# block", so its presence anywhere under a _BLOCK_TAGS wrapper is the signal
# that the wrapper is a layout container to recurse into via walk(), not a
# text leaf to flatten via _inline().
_STRUCTURAL_TAGS = frozenset(_HEADING_LEVELS) | {"ul", "ol"} | _BLOCK_TAGS


def _inline(node: Tag | NavigableString) -> str:
    """Render inline content: text, links, and bold/italic emphasis."""
    if isinstance(node, NavigableString):
        return str(node)
    if node.name in ("script", "style", "noscript", "template"):
        return ""
    if node.name == "br":
        return "\n"
    if node.name == "a":
        text = "".join(_inline(c) for c in node.children).strip()
        href = node.get("href")
        return f"[{text}]({href})" if href and text else text
    if node.name in ("strong", "b"):
        text = "".join(_inline(c) for c in node.children).strip()
        return f"**{text}**" if text else ""
    if node.name in ("em", "i"):
        text = "".join(_inline(c) for c in node.children).strip()
        return f"*{text}*" if text else ""
    return "".join(_inline(c) for c in node.children)


def _has_structural_descendant(node: Tag) -> bool:
    """True when a heading, list, or nested block sits anywhere under ``node``.

    Distinguishes a layout wrapper (an <article>/<section>/<div> whose job is
    to group other block content) from a text leaf (a <p> or <li> whose
    children are just inline markup) -- see ``_STRUCTURAL_TAGS``.
    """
    return any(
        isinstance(descendant, Tag) and descendant.name in _STRUCTURAL_TAGS
        for descendant in node.descendants
    )


def to_markdown(root: Tag) -> str:
    """Render an element tree as Markdown with headings, lists, and links preserved.

    Block elements become their own line(s); list items are bulleted.
    Consecutive blank lines are collapsed so boilerplate removal upstream
    (which decomposes whole elements) doesn't leave gaps.
    """
    lines: list[str] = []

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if text:
                    lines.append(text)
                continue
            if child.name in ("script", "style", "noscript", "template"):
                continue
            if child.name in _HEADING_LEVELS:
                text = _inline(child).strip()
                if text:
                    lines.append(f"{'#' * _HEADING_LEVELS[child.name]} {text}")
                continue
            if child.name in ("ul", "ol"):
                for i, li in enumerate(child.find_all("li", recursive=False), start=1):
                    text = _inline(li).strip()
                    if text:
                        marker = f"{i}." if child.name == "ol" else "-"
                        lines.append(f"{marker} {text}")
                continue
            if child.name in _BLOCK_TAGS:
                if _has_structural_descendant(child):
                    # A layout wrapper around headings/lists/nested blocks: recurse
                    # so each keeps its own line, instead of _inline() flattening
                    # the whole subtree into one line with no "#"/"-" markers left.
                    walk(child)
                    continue
                text = _inline(child).strip()
                if text:
                    lines.append(text)
                else:
                    walk(child)  # a container with only nested blocks, not text
                continue
            walk(child)  # unrecognized wrapper: descend without emitting a line

    walk(root)
    return "\n\n".join(lines)


def extract_markdown(html: str, content_area_config: dict | None = None) -> dict[str, str]:
    """Return ``{"content_markdown", "full_markdown", "content_area_strategy"}`` for ``html``."""
    soup = BeautifulSoup(html, features="lxml")
    full_root = soup.body or soup
    content_root, strategy = resolve_content_area(soup, content_area_config)
    return {
        "content_markdown": to_markdown(content_root),
        "full_markdown": to_markdown(full_root),
        "content_area_strategy": strategy,
    }
