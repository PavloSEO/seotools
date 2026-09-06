"""#356: one ceiling, stated in bytes, that refuses instead of quietly reducing.

Two defects lived here. The number 10 000 was written twice, in ``spider.py`` and
``collect.py``, so raising it in one place would have left the other crawler at the
old limit. And both applied it with ``min(...)``: a caller who asked for 200 000
URLs got 50 000 and no indication that the crawl covered a quarter of the site.
An audit reads a truncated crawl as a small site, which is the failure mode worth
a test.
"""

from __future__ import annotations

import dataclasses

import pytest

from seohead.crawl import collect, settings, spider
from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import MAX_URLS_CEILING, ConfigError, checked_url_budget
from seohead.crawl.spider import LinkEdge

# The per-record byte costs quoted above MAX_URLS_CEILING in settings.py were
# measured against these dataclasses at these widths. Adding a field does not
# invalidate the ceiling by itself, but it does invalidate the arithmetic that
# justifies it, and nothing else in the suite would notice.
# ``body_unavailable`` joins the already-integrated iframe and hreflang fields.
# 56 -> 58 for ``meta_fragment`` and ``ajax_scheme_outlinks`` (#386), re-measured
# rather than assumed: an empty string and a zero, and a paired tracemalloc run
# over 8 000 records reports the same bytes per record at both widths.
FIELD_COUNTS_THE_CEILING_WAS_COMPUTED_AGAINST = {LinkEdge: 8, PageRecord: 58}


def test_both_crawlers_read_the_same_ceiling() -> None:
    """The constant is defined once. Neither crawler may carry its own copy."""
    assert spider.checked_url_budget is settings.checked_url_budget
    assert collect.checked_url_budget is settings.checked_url_budget
    assert not hasattr(spider, "MAX_URLS_CEILING")
    assert not hasattr(collect, "MAX_URLS_CEILING")


def test_a_budget_above_the_ceiling_is_refused_rather_than_reduced() -> None:
    with pytest.raises(ValueError) as exc:
        checked_url_budget(MAX_URLS_CEILING + 1)
    assert f"{MAX_URLS_CEILING:,}" in str(exc.value)
    assert checked_url_budget(MAX_URLS_CEILING) == MAX_URLS_CEILING


def test_configuration_refuses_the_same_number_with_the_same_reason() -> None:
    with pytest.raises(ConfigError) as exc:
        # load() validates, so an over-budget config never reaches a crawler at all.
        # A dotted path, which is the override contract: a nested mapping is
        # refused on its own grounds and would prove nothing about the ceiling.
        settings.load(overrides={"limits.max_urls": MAX_URLS_CEILING + 1})
    message = str(exc.value)
    assert f"{MAX_URLS_CEILING:,}" in message
    assert "scope" in message  # says what to do instead, not only what is wrong


def test_the_ceiling_is_the_documented_50_000() -> None:
    assert MAX_URLS_CEILING == 50_000
    described = {row["path"]: row["description"] for row in settings.describe_settings()}
    assert f"{MAX_URLS_CEILING:,}" in described["limits.max_urls"]


@pytest.mark.parametrize("record", list(FIELD_COUNTS_THE_CEILING_WAS_COMPUTED_AGAINST))
def test_the_records_the_ceiling_was_sized_for_have_not_grown(record: type) -> None:
    expected = FIELD_COUNTS_THE_CEILING_WAS_COMPUTED_AGAINST[record]
    actual = len(dataclasses.fields(record))
    assert actual == expected, (
        f"{record.__name__} now has {actual} fields, not the {expected} the memory figures "
        "above MAX_URLS_CEILING in seohead/crawl/settings.py were measured against. "
        "Re-measure the bytes per record and update that comment (and this count) rather "
        "than leaving a ceiling justified by arithmetic that no longer holds."
    )
