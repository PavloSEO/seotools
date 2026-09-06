"""Aggregate: issue fingerprinting is stable regardless of (capped) locations."""

from __future__ import annotations

from seohead.sf.core.aggregate import _fingerprint, _implausible_checks
from seohead.sf.core.models import Issue


def test_fingerprint_independent_of_locations():
    a = Issue(
        check="BROKEN_INTERNAL_LINK",
        severity="critical",
        source="s",
        message="m",
        target_url="https://x/p",
        status_code=404,
        locations=[{"source_url": "https://x/a"}],
    )
    b = Issue(
        check="BROKEN_INTERNAL_LINK",
        severity="critical",
        source="s",
        message="m",
        target_url="https://x/p",
        status_code=404,
        locations=[{"source_url": "https://x/a"}, {"source_url": "https://x/b"}],
    )
    assert _fingerprint(a) == _fingerprint(b)


def test_implausible_checks_excludes_image_targeted_checks():
    # An ordinary image-heavy site has more images than pages: 30 oversized
    # images across only 10 crawled pages is not a defect in IMG_OVER_KB, it
    # is the wrong denominator (a check whose evidence unit is an image, not
    # a page). It must never be reported as an implausible share.
    issues = [
        Issue(
            check="IMG_OVER_KB",
            severity="warning",
            source="SF:Images:Over X KB",
            message="Image exceeds threshold",
            target_url=f"https://example.com/img{i}.jpg",
        )
        for i in range(30)
    ]
    assert _implausible_checks(issues, 10) == []


def test_implausible_checks_still_scores_page_targeted_checks():
    # Negative control: a check whose evidence unit really is a page keeps
    # being measured against n_pages exactly as before.
    issues = [
        Issue(
            check="TITLE_MISSING",
            severity="warning",
            source="SF",
            message="Title missing",
            target_url=f"https://example.com/p{i}",
        )
        for i in range(3)
    ]
    assert _implausible_checks(issues, 10) == []

    issues_majority = [
        Issue(
            check="TITLE_MISSING",
            severity="warning",
            source="SF",
            message="Title missing",
            target_url=f"https://example.com/p{i}",
        )
        for i in range(6)
    ]
    result = _implausible_checks(issues_majority, 10)
    assert result == [{"check": "TITLE_MISSING", "pages": 6, "share": 0.6}]
