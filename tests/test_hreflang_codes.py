"""hreflang values are language tags, and validity is a question about registers.

The previous shape regex answered a different question — "does this look like a
tag" — and got both halves wrong: it rejected values Google documents and
accepted ones that name nothing.
"""

from __future__ import annotations

import pytest

from seohead.tools.hreflang import code_error, validate

VALID = [
    "en",
    "en-US",
    "EN-US",  # language tags are case-insensitive
    "en-us",
    "pt-BR",
    "zh-Hans",
    "zh-Hant-TW",  # language-script-region
    "sr-Cyrl-RS",
    "en-419",  # UN M.49 region (Latin America)
    "x-default",
    "X-Default",
]

INVALID = [
    "xx",  # right shape, names no language
    "eng",  # ISO 639-2, not 639-1
    "en-usa",  # region is alpha-2 or three digits
    "en-UK",  # the country code for the United Kingdom is GB
    "en-XQ",  # unassigned
    "zh-Xxxx-TW",  # not a script
    "en_US",  # underscore is not a subtag separator
    "de-DE-1996",  # variant subtags are not part of an hreflang value
    "",
]


@pytest.mark.parametrize("value", VALID)
def test_valid_codes_are_accepted(value):
    assert code_error(value) == ""


@pytest.mark.parametrize("value", INVALID)
def test_invalid_codes_are_rejected(value):
    assert code_error(value)


def test_the_reason_is_reported_not_just_the_verdict():
    assert "ISO 3166-1" in code_error("en-UK")
    assert "ISO 639-1" in code_error("xx")


def test_duplicates_are_matched_case_insensitively():
    alternates = [
        {"hreflang": "en-US", "href": "https://e.com/a"},
        {"hreflang": "en-us", "href": "https://e.com/b"},
        {"hreflang": "x-default", "href": "https://e.com/"},
    ]
    assert validate(alternates, "") == ["duplicate hreflang: en-us"]


def test_x_default_is_recognised_in_any_case():
    alternates = [{"hreflang": "X-DEFAULT", "href": "https://e.com/"}]
    assert "no x-default alternate" not in validate(alternates, "https://e.com/")


@pytest.mark.parametrize(
    ("page_url", "href"),
    (
        ("https://example.com/page", "HTTPS://EXAMPLE.COM/page"),
        ("https://example.com/page", "https://example.com/page/"),
    ),
)
def test_self_reference_uses_shared_url_identity(page_url, href):
    alternates = [{"hreflang": "en", "href": href}]

    assert "page does not self-reference in its hreflang set" not in validate(alternates, page_url)


@pytest.mark.parametrize(
    ("page_url", "href"),
    (
        ("https://example.com/News", "https://example.com/news"),
        ("https://example.com/page?ref=a", "https://example.com/page?ref=b"),
    ),
)
def test_self_reference_preserves_path_and_query_identity(page_url, href):
    alternates = [{"hreflang": "en", "href": href}]

    assert "page does not self-reference in its hreflang set" in validate(alternates, page_url)
