"""Report whether boilerplate (header/nav/footer) actually matches across a site.

A single-page check cannot answer "is the boilerplate the same everywhere?" —
that question only makes sense across a corpus. This module hashes the header,
navigation and footer regions of each page and groups pages by that hash. The
largest group is assumed to be the current template; every other group is a
minority worth a look, because that is how a template catches:

  - a navigation block that lost links on one template, quietly cutting
    internal linking to a whole section;
  - a footer that differs on old pages never migrated to the current template;
  - a menu that renders differently under one language or one URL branch;
  - header markup surviving only where a legacy template does.

The hashes are computed from our own regions (BeautifulSoup + hashlib only),
so they stay deterministic and inside the byte-comparison determinism gate —
no third-party extractor whose output could shift between versions.

This module is pure and performs no network access.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from copy import copy
from typing import Any

from bs4 import BeautifulSoup, Tag

from seohead.tools.parser import is_inert_template_content

# What counts as "boilerplate" for this report — broader than content_area's
# default exclusions (nav, footer) because the issue this answers is
# specifically about header/nav/footer consistency, not word count.
BOILERPLATE_TAGS = ("header", "nav", "footer")


def _live_region_markup(region: Tag) -> str:
    """Serialize a boilerplate region without inert template descendants."""
    region = copy(region)
    for template in region.find_all("template"):
        template.decompose()
    return str(region)


def boilerplate_hash(html: str) -> str:
    """Return a SHA-1 hex digest of a page's header/nav/footer markup.

    Regions are concatenated in document order and their tag structure is
    kept (not just visible text), so a link removed from a menu changes the
    hash even when the remaining text reads the same.
    """
    soup = BeautifulSoup(html, features="lxml")
    pieces = [
        _live_region_markup(el)
        for el in soup.find_all(BOILERPLATE_TAGS)
        if not is_inert_template_content(el)
    ]
    basis = "".join(" ".join(piece.split()) for piece in pieces)
    return hashlib.sha1(basis.encode("utf-8"), usedforsecurity=False).hexdigest()


def boilerplate_consistency_report(pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Group pages by boilerplate hash and report the minority groups.

    Each page is ``{"url": str, "html": str}`` or, when the hash was already
    computed, ``{"url": str, "hash": str}``. The dominant group (most pages)
    is treated as the current template; every other group is reported with
    its fraction of the corpus and a sample URL, so "the footer on 340 pages
    is missing the contacts block" is answerable instead of "some pages differ".
    """
    if not pages:
        return {"ok": True, "count": 0, "dominant_hash": None, "groups": []}

    by_hash: dict[str, list[str]] = defaultdict(list)
    for page in pages:
        url = str(page.get("url") or "")
        h = page.get("hash") or boilerplate_hash(page.get("html") or "")
        by_hash[h].append(url)

    total = sum(len(urls) for urls in by_hash.values())
    # Ties break on hash for a stable, arbitrary-but-deterministic pick.
    dominant_hash = max(by_hash, key=lambda h: (len(by_hash[h]), h))

    groups = [
        {
            "hash": h,
            "count": len(urls),
            "fraction": round(len(urls) / total, 4),
            "dominant": h == dominant_hash,
            "sample_url": urls[0],
            "urls": sorted(urls),
        }
        for h, urls in by_hash.items()
    ]
    groups.sort(key=lambda g: (-g["count"], g["hash"]))

    return {
        "ok": True,
        "count": total,
        "dominant_hash": dominant_hash,
        "groups": groups,
        "minority_groups": [g for g in groups if not g["dominant"]],
    }
