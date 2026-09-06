"""Normalize: non-finite values must not crash or poison JSON."""

from __future__ import annotations

import pandas as pd

from seohead.sf.core.normalize import (
    INTERNAL_FIELD_MAP,
    norm_url,
    records_from_df,
    to_float,
    to_int,
)


def test_to_int_handles_non_finite():
    assert to_int("inf") is None
    assert to_int(float("inf")) is None
    assert to_int("nan") is None
    assert to_int("-inf") is None
    assert to_int("42") == 42


def test_to_float_drops_non_finite():
    assert to_float("inf") is None
    assert to_float(float("nan")) is None
    assert to_float("3.5") == 3.5


# --------------------------------------------------------------------------
# #202 — norm_url must fold scheme/host but keep path/query/fragment case.
# --------------------------------------------------------------------------
def test_norm_url_keeps_path_case_distinct():
    assert norm_url("https://example.test/en") != norm_url("https://example.test/EN")
    assert norm_url("https://example.com/News") != norm_url("https://example.com/news")


def test_norm_url_still_folds_scheme_and_host_case():
    assert norm_url("HTTPS://Example.COM/x") == norm_url("https://example.com/x")


def test_norm_url_still_folds_a_trailing_slash():
    assert norm_url("https://example.com/x/") == norm_url("https://example.com/x")
    assert norm_url("https://example.com/") == norm_url("https://example.com")


def test_norm_url_query_case_is_preserved():
    assert norm_url("https://example.com/x?Q=A") != norm_url("https://example.com/x?q=a")


# --------------------------------------------------------------------------
# #449 — a comma-decimal numeric SF column must not silently collapse to None.
# --------------------------------------------------------------------------
def test_to_float_accepts_comma_decimal():
    assert to_float("2,500") == 2.5
    assert to_int("2,500") == 2


def test_to_float_dot_decimal_and_empty_still_normalize_as_before():
    """Negative control: the already-correct path must stay untouched."""
    assert to_float("2.5") == 2.5
    assert to_float("") is None
    assert to_float(None) is None


def test_records_from_df_does_not_collapse_comma_decimal_text_ratio():
    df = pd.DataFrame({"Address": ["a", "b", "c"], "Text Ratio": ["2,500", "0,000", ""]})
    recs = records_from_df(df, INTERNAL_FIELD_MAP)
    assert [r["text_ratio"] for r in recs] == [2.5, 0.0, None]
