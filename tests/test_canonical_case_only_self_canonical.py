"""A canonical differing from the page URL only in host or scheme case is the
same page (issue #440).

``evidence.py::_indexability`` compared ``record.canonical`` to ``record.url``
case-sensitively, so a canonical that differed only in host or scheme case
(a routine CDN/copy-paste artifact) was marked Non-Indexable/Canonicalised.
``rules.py`` (via ``normalize.norm_url``) folds scheme and host case per
RFC 3986 and treats the very same pair as self-canonical. The mismatch let a
page silently drop out of every on-page check (``indexable_html_pages()``
reads ``Page.is_indexable``, which reads evidence.py's verdict) while
``NON_INDEXABLE_LINKED`` could fire with "Canonicalised" as a reason that
``check_canonical_directives`` refused to corroborate.
"""

from __future__ import annotations

from seohead.crawl.evidence import _indexability, _same_page


class _Record:
    def __init__(self, url, canonical, status_code=200):
        self.url = url
        self.canonical = canonical
        self.status_code = status_code
        self.error = None
        self.meta_robots = ""
        self.x_robots = ""


def test_a_host_case_only_canonical_is_self_canonical():
    rec = _Record("https://example.com/page", "https://EXAMPLE.com/page")
    assert _indexability(rec) == ("Indexable", "")


def test_a_scheme_case_only_canonical_is_self_canonical():
    rec = _Record("https://example.com/page", "HTTPS://example.com/page")
    assert _indexability(rec) == ("Indexable", "")


def test_a_path_case_difference_is_still_a_real_cross_canonical():
    """The negative control: path case is not folded (RFC 3986), so a real
    cross-canonical relationship must still be reported."""
    rec = _Record("https://example.com/Page", "https://example.com/page")
    assert _indexability(rec) == ("Non-Indexable", "Canonicalised")


def test_same_page_matches_norm_url_semantics_for_case_and_slash():
    # Host/scheme fold, matching norm_url.
    assert _same_page("https://example.com/page", "https://EXAMPLE.com/page")
    assert _same_page("https://example.com/page", "HTTPS://example.com/page")
    # Path case is preserved, not folded.
    assert not _same_page("https://example.com/Page", "https://example.com/page")
    # A different page entirely is still different.
    assert not _same_page("https://example.com/a", "https://example.com/b")
