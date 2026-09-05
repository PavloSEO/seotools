"""Offline tests for the boilerplate-consistency report (issue #19, part 3)."""

from seohead.tools import boilerplate_report as B

_HEADER = "<header><a href='/'>Logo</a></header>"
_NAV = "<nav><a href='/a'>A</a><a href='/b'>B</a><a href='/c'>C</a></nav>"
_FOOTER = "<footer><a href='/contact'>Contact</a><a href='/terms'>Terms</a></footer>"
_GOOD_PAGE = f"<html><body>{_HEADER}{_NAV}<main>page content</main>{_FOOTER}</body></html>"

# One link missing from the footer: the truncated-footer fixture from the issue.
_TRUNCATED_FOOTER = "<footer><a href='/contact'>Contact</a></footer>"
_BAD_PAGE = (
    f"<html><body>{_HEADER}{_NAV}<main>other content</main>{_TRUNCATED_FOOTER}</body></html>"
)


def test_identical_boilerplate_hashes_equal_and_differing_ones_dont():
    assert B.boilerplate_hash(_GOOD_PAGE) == B.boilerplate_hash(_GOOD_PAGE)
    assert B.boilerplate_hash(_GOOD_PAGE) != B.boilerplate_hash(_BAD_PAGE)


def test_document_order_changes_the_boilerplate_hash():
    ordered = "<body><header>Brand</header><nav>Menu</nav></body>"
    reordered = "<body><nav>Menu</nav><header>Brand</header></body>"
    assert B.boilerplate_hash(ordered) != B.boilerplate_hash(reordered)


def test_template_only_boilerplate_does_not_change_the_hash():
    inert_footer = "<template><footer>Unreleased contact block</footer></template>"
    assert B.boilerplate_hash(_GOOD_PAGE) == B.boilerplate_hash(
        _GOOD_PAGE.replace("</body>", inert_footer + "</body>")
    )


def test_nested_template_content_does_not_create_a_minority_group():
    def page(draft: str) -> str:
        return (
            "<html><body><header><nav>Same live menu"
            f"<template><footer>{draft}</footer></template>"
            "</nav></header><main>Article</main></body></html>"
        )

    report = B.boilerplate_consistency_report(
        [
            {"url": "https://example.test/a", "html": page("draft-a")},
            {"url": "https://example.test/b", "html": page("draft-a")},
            {"url": "https://example.test/c", "html": page("draft-b")},
        ]
    )

    assert report["minority_groups"] == []


def test_report_flags_exactly_the_page_with_the_truncated_footer():
    pages = [{"url": f"https://site.tld/p{i}", "html": _GOOD_PAGE} for i in range(1, 5)] + [
        {"url": "https://site.tld/old-legacy-page", "html": _BAD_PAGE}
    ]

    r = B.boilerplate_consistency_report(pages)

    assert r["ok"] is True
    assert r["count"] == 5
    dominant = next(g for g in r["groups"] if g["dominant"])
    assert dominant["count"] == 4
    assert len(r["minority_groups"]) == 1
    minority = r["minority_groups"][0]
    assert minority["urls"] == ["https://site.tld/old-legacy-page"]
    assert minority["sample_url"] == "https://site.tld/old-legacy-page"
    assert minority["fraction"] == 1 / 5


def test_report_empty_when_everything_matches():
    pages = [{"url": f"https://site.tld/p{i}", "html": _GOOD_PAGE} for i in range(3)]
    r = B.boilerplate_consistency_report(pages)
    assert r["minority_groups"] == []
    assert r["groups"][0]["fraction"] == 1.0


def test_report_accepts_precomputed_hashes():
    pages = [
        {"url": "https://site.tld/a", "hash": "same"},
        {"url": "https://site.tld/b", "hash": "same"},
        {"url": "https://site.tld/c", "hash": "different"},
    ]
    r = B.boilerplate_consistency_report(pages)
    assert r["dominant_hash"] == "same"
    assert r["minority_groups"][0]["urls"] == ["https://site.tld/c"]


def test_empty_input():
    r = B.boilerplate_consistency_report([])
    assert r == {"ok": True, "count": 0, "dominant_hash": None, "groups": []}
