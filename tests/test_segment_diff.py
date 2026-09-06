"""Cross-segment counterpart diff (#358). Pure, no network.

Uses the real ``Scope`` from ``seohead.crawl.spider`` to build ``segment_for``/
``rejection`` -- the module under test never imports it itself (see the module
docstring), but a test verifying the "passed in, not imported" contract needs
the real thing, not a stand-in that could drift from it.
"""

from __future__ import annotations

import pytest

from seohead.crawl.spider import Scope
from seohead.sf.core import segment_diff as sd

SEGMENTS = [
    {"name": "en", "prefix": "/en/"},
    {"name": "fr", "prefix": "/fr/"},
    {"name": "de", "prefix": "/de/"},
]


def _scope(segments_only=None):
    return Scope.from_config({"segments": SEGMENTS, "segments_only": segments_only or []})


def _page(url, *, hreflang=None, status_code=200, content_type="text/html; charset=utf-8"):
    return {
        "url": url,
        "status_code": status_code,
        "content_type": content_type,
        "metrics": {"hreflang": hreflang if hreflang is not None else []},
    }


def _alt(url, lang="fr"):
    return {"lang": lang, "raw_href": url, "url": url}


def test_declared_alternate_translated_slug_is_not_reported_as_missing():
    """The trap the feature exists to avoid: a mirrored path does not exist for a
    properly translated slug, but the site's own hreflang says it is localised."""
    scope = _scope()
    pages = [
        _page("https://x.tld/en/about", hreflang=[_alt("https://x.tld/fr/a-propos")]),
        _page("https://x.tld/fr/a-propos", hreflang=[_alt("https://x.tld/en/about", "en")]),
    ]
    result = sd.diff_segments(
        pages,
        source="en",
        target="fr",
        segments=SEGMENTS,
        segment_for=scope.segment_for,
    )
    row = result["pages"][0]
    assert row["class"] == "declared"
    assert row["counterpart"] == "https://x.tld/fr/a-propos"
    assert result["counts"]["absent"] == 0


def test_same_page_without_the_declaration_is_inferred_missing_not_a_fact():
    """Same site, same translated page, but this time nothing declares the
    correspondence and the mirrored path (/fr/about) genuinely does not exist --
    that is the one case allowed to read as "not localised", and even then only
    as an inference, never a bare fact indistinguishable from "declared"."""
    scope = _scope()
    # A high mirror rate so inference is switched on: several other pairs really
    # do mirror, established via declared+crawled hreflang.
    pages = [
        _page("https://x.tld/en/contact", hreflang=[_alt("https://x.tld/fr/contact")]),
        _page("https://x.tld/fr/contact", hreflang=[_alt("https://x.tld/en/contact", "en")]),
        _page("https://x.tld/en/shop", hreflang=[_alt("https://x.tld/fr/shop")]),
        _page("https://x.tld/fr/shop", hreflang=[_alt("https://x.tld/en/shop", "en")]),
        _page("https://x.tld/en/blog", hreflang=[_alt("https://x.tld/fr/blog")]),
        _page("https://x.tld/fr/blog", hreflang=[_alt("https://x.tld/en/blog", "en")]),
        _page("https://x.tld/en/news", hreflang=[_alt("https://x.tld/fr/news")]),
        _page("https://x.tld/fr/news", hreflang=[_alt("https://x.tld/en/news", "en")]),
        _page("https://x.tld/en/about", hreflang=[]),  # no declaration this time
    ]
    result = sd.diff_segments(
        pages,
        source="en",
        target="fr",
        segments=SEGMENTS,
        segment_for=scope.segment_for,
    )
    assert result["inference_enabled"] is True
    about = next(r for r in result["pages"] if r["url"] == "https://x.tld/en/about")
    assert about["class"] == "absent"
    assert about["method"] == "mirrored_path"
    # labelled as an inference-driven finding, never a bare, undistinguishable-from-declared fact
    assert about["class"] != "declared"


def test_five_counts_sum_to_the_eligible_page_count():
    scope = _scope()
    pages = [
        _page("https://x.tld/en/a", hreflang=[_alt("https://x.tld/fr/a")]),  # declared, crawled
        _page("https://x.tld/fr/a", hreflang=[]),
        _page(
            "https://x.tld/en/b", hreflang=[_alt("https://x.tld/fr/never-crawled")]
        ),  # declared_not_crawled
        _page("https://x.tld/en/c", hreflang=[]),  # no declaration; mirror /fr/c missing -> absent
        # Excluded from the eligible pool entirely:
        _page("https://x.tld/en/noindexed", hreflang=[], status_code=200),
        _page("https://x.tld/en/broken", status_code=404),
        _page("https://x.tld/en/pdf", content_type="application/pdf"),
    ]
    pages[4]["metrics"]["meta_robots"] = "noindex"
    result = sd.diff_segments(
        pages,
        source="en",
        target="fr",
        segments=SEGMENTS,
        segment_for=scope.segment_for,
    )
    assert sum(result["counts"].values()) == result["eligible_pages"]
    assert result["eligible_pages"] == 3  # a, b, c only
    assert result["counts"]["declared"] == 1
    assert result["counts"]["declared_not_crawled"] == 1


def test_mirror_rate_below_90_percent_switches_inference_off():
    """Only one of four declared pairs actually mirrors its path -- far below the
    90% bar -- so an undeclared page must not be called "absent" on the strength
    of an inference method this site does not, in fact, follow."""
    scope = _scope()
    pages = [
        _page("https://x.tld/en/a", hreflang=[_alt("https://x.tld/fr/a")]),  # mirrors
        _page("https://x.tld/fr/a", hreflang=[]),
        _page("https://x.tld/en/b", hreflang=[_alt("https://x.tld/fr/b-different")]),
        _page("https://x.tld/fr/b-different", hreflang=[]),
        _page("https://x.tld/en/c", hreflang=[_alt("https://x.tld/fr/c-different")]),
        _page("https://x.tld/fr/c-different", hreflang=[]),
        _page("https://x.tld/en/d", hreflang=[_alt("https://x.tld/fr/d-different")]),
        _page("https://x.tld/fr/d-different", hreflang=[]),
        _page("https://x.tld/en/undeclared", hreflang=[]),
    ]
    result = sd.diff_segments(
        pages,
        source="en",
        target="fr",
        segments=SEGMENTS,
        segment_for=scope.segment_for,
    )
    assert result["mirror_rate"] == pytest.approx(0.25)
    assert result["inference_enabled"] is False
    undeclared = next(r for r in result["pages"] if r["url"] == "https://x.tld/en/undeclared")
    assert undeclared["class"] == "undetermined"
    assert "90%" in undeclared["reason"] or "mirror rate" in undeclared["reason"]
    assert result["counts"]["absent"] == 0


def test_no_declared_pairs_at_all_leaves_inference_off_by_default():
    scope = _scope()
    pages = [_page("https://x.tld/en/a", hreflang=[])]
    result = sd.diff_segments(
        pages, source="en", target="fr", segments=SEGMENTS, segment_for=scope.segment_for
    )
    assert result["mirror_rate"] is None
    assert result["inference_enabled"] is False
    assert result["counts"]["undetermined"] == 1
    assert result["counts"]["absent"] == 0


def test_target_segment_excluded_by_segments_only_yields_no_absences():
    """A target the crawl was scoped away from cannot support a negative claim:
    every candidate absence becomes undetermined instead, naming why."""
    scope = _scope(segments_only=["en"])
    pages = [
        _page("https://x.tld/en/a", hreflang=[_alt("https://x.tld/fr/a")]),
        _page("https://x.tld/fr/a", hreflang=[]),  # would not really be crawled, but present here
        _page("https://x.tld/en/b", hreflang=[]),
    ]
    result = sd.diff_segments(
        pages,
        source="en",
        target="fr",
        segments=SEGMENTS,
        segment_for=scope.segment_for,
        segments_only=["en"],
    )
    # the undeclared page cannot be called absent -- fr was excluded by segments_only
    b_row = next(r for r in result["pages"] if r["url"] == "https://x.tld/en/b")
    assert b_row["class"] == "undetermined"
    assert "segments_only" in b_row["reason"]
    assert result["counts"]["absent"] == 0


def test_partial_crawl_of_target_yields_no_absences():
    """With inference enabled (mirror rate measured high via several declared
    pairs), an undeclared page's missing mirror would ordinarily be "absent" --
    but a partial crawl cannot support that negative claim either."""
    scope = _scope()
    pages = [
        _page("https://x.tld/en/x", hreflang=[_alt("https://x.tld/fr/x")]),
        _page("https://x.tld/fr/x", hreflang=[]),
        _page("https://x.tld/en/y", hreflang=[_alt("https://x.tld/fr/y")]),
        _page("https://x.tld/fr/y", hreflang=[]),
        _page("https://x.tld/en/a", hreflang=[]),
    ]
    result = sd.diff_segments(
        pages,
        source="en",
        target="fr",
        segments=SEGMENTS,
        segment_for=scope.segment_for,
        crawl_partial=True,
    )
    assert result["inference_enabled"] is True
    a_row = next(r for r in result["pages"] if r["url"] == "https://x.tld/en/a")
    assert result["counts"]["absent"] == 0
    assert a_row["class"] == "undetermined"
    assert "partial" in a_row["reason"]


def test_unknown_segment_name_is_refused():
    scope = _scope()
    with pytest.raises(sd.SegmentDiffError, match="unknown"):
        sd.diff_segments(
            [_page("https://x.tld/en/a")],
            source="en",
            target="es",
            segments=SEGMENTS,
            segment_for=scope.segment_for,
        )


def test_source_equal_target_is_refused():
    scope = _scope()
    with pytest.raises(sd.SegmentDiffError, match="both"):
        sd.diff_segments(
            [_page("https://x.tld/en/a")],
            source="en",
            target="en",
            segments=SEGMENTS,
            segment_for=scope.segment_for,
        )


def test_no_segments_declared_anywhere_is_refused():
    scope = Scope.from_config({"segments": [], "segments_only": []})
    with pytest.raises(sd.SegmentDiffError, match="no segments declared"):
        sd.diff_segments(
            [_page("https://x.tld/en/a")],
            source="en",
            target="fr",
            segments=[],
            segment_for=scope.segment_for,
        )


def test_page_schema_with_no_hreflang_field_at_all_is_refused():
    scope = _scope()
    pages = [{"url": "https://x.tld/en/a", "status_code": 200, "content_type": "text/html"}]
    with pytest.raises(sd.SegmentDiffError, match="hreflang"):
        sd.diff_segments(
            pages, source="en", target="fr", segments=SEGMENTS, segment_for=scope.segment_for
        )


def test_empty_hreflang_field_present_is_not_a_schema_error():
    """The negative control for the check above: a page schema that legitimately
    carries the field, just empty, must not be refused the same way."""
    scope = _scope()
    pages = [_page("https://x.tld/en/a", hreflang=[])]
    result = sd.diff_segments(
        pages, source="en", target="fr", segments=SEGMENTS, segment_for=scope.segment_for
    )
    assert result["eligible_pages"] == 1


def test_host_based_segments_mirror_by_host_not_path():
    scope = Scope.from_config(
        {
            "segments": [{"name": "en", "host": "en.x.tld"}, {"name": "fr", "host": "fr.x.tld"}],
            "segments_only": [],
        }
    )
    segments = [{"name": "en", "host": "en.x.tld"}, {"name": "fr", "host": "fr.x.tld"}]
    pages = [
        _page("https://en.x.tld/about", hreflang=[_alt("https://fr.x.tld/about")]),
        _page("https://fr.x.tld/about", hreflang=[]),
        _page("https://en.x.tld/contact", hreflang=[]),
        _page("https://fr.x.tld/contact", hreflang=[]),
    ]
    result = sd.diff_segments(
        pages, source="en", target="fr", segments=segments, segment_for=scope.segment_for
    )
    contact = next(r for r in result["pages"] if r["url"] == "https://en.x.tld/contact")
    assert contact["class"] == "inferred"
    assert contact["counterpart"] == "https://fr.x.tld/contact"


def test_handler_wires_a_native_audit_document_end_to_end():
    """The interface layer (handlers.segment_diff) is the one place the crawl's
    Scope and the pure analyzer meet -- built from an audit.json's own recorded
    run.crawl_config, not re-declared by the caller."""
    from seohead.servers import handlers

    audit = {
        "run": {
            "source": "https://x.tld/en/",
            "crawl_partial": False,
            "crawl_config": {
                "scope.segments": SEGMENTS,
                "scope.segments_only": [],
            },
        },
        "pages": [
            {
                "url": "https://x.tld/en/about",
                "status_code": 200,
                "content_type": "text/html",
                "metrics": {"hreflang": [_alt("https://x.tld/fr/a-propos")]},
            },
            {
                "url": "https://x.tld/fr/a-propos",
                "status_code": 200,
                "content_type": "text/html",
                "metrics": {"hreflang": []},
            },
        ],
    }
    result = handlers.segment_diff(audit=audit, source="en", target="fr")
    assert result["counts"]["declared"] == 1
    assert result["counts"]["absent"] == 0


def test_handler_refuses_an_audit_with_no_segments_configured():
    from seohead.servers import handlers

    audit = {"run": {"crawl_config": {}}, "pages": [{"url": "https://x.tld/a"}]}
    with pytest.raises(sd.SegmentDiffError, match="no segments declared"):
        handlers.segment_diff(audit=audit, source="en", target="fr")


def test_declared_not_crawled_names_the_scope_rejection_reason():
    scope = _scope(segments_only=["en"])
    pages = [
        _page(
            "https://x.tld/en/a", hreflang=[_alt("https://x.tld/fr/a")]
        ),  # never actually crawled since only "en" was scoped
    ]
    result = sd.diff_segments(
        pages,
        source="en",
        target="fr",
        segments=SEGMENTS,
        segment_for=scope.segment_for,
        rejection=lambda u: scope.rejection(u, "x.tld"),
        segments_only=["en"],
    )
    row = result["pages"][0]
    assert row["class"] == "declared_not_crawled"
    assert "outside_segment" in row["reason"]
