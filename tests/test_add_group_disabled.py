"""AuditContext.add_group must not leave evidence for a disabled check (#450)."""

from __future__ import annotations

import pandas as pd

from seohead.sf.core.context import AuditContext
from seohead.sf.core.rules import check_duplicates


class _FakeExports:
    def __init__(self, df: pd.DataFrame):
        self.frames = {"internal_all": df}

    def get(self, name):
        return self.frames.get(name) if name == "internal_all" else None


def _fake_exports(df: pd.DataFrame):
    return _FakeExports(df)


def _duplicate_titles_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Address": "https://ex.test/a",
                "Content Type": "text/html",
                "Status Code": 200,
                "Status": "OK",
                "Indexability": "Indexable",
                "Title 1": "Same Title",
            },
            {
                "Address": "https://ex.test/b",
                "Content Type": "text/html",
                "Status Code": 200,
                "Status": "OK",
                "Indexability": "Indexable",
                "Title 1": "Same Title",
            },
        ]
    )


def test_disabled_duplicate_check_leaves_no_group_evidence():
    df = _duplicate_titles_df()
    config = {"checks": {"TITLE_DUPLICATE": {"enabled": False}}}
    ctx = AuditContext(_fake_exports(df), config)
    check_duplicates(ctx)

    assert [i for i in ctx.issues if i.check == "TITLE_DUPLICATE"] == []
    assert [g for g in ctx.groups if g.check == "TITLE_DUPLICATE"] == []
    assert "TITLE_DUPLICATE" not in ctx._fired_ids


def test_enabled_duplicate_check_still_produces_its_group():
    # Negative control: the fix must not touch the enabled path.
    df = _duplicate_titles_df()
    ctx = AuditContext(_fake_exports(df), {})
    check_duplicates(ctx)

    groups = [g for g in ctx.groups if g.check == "TITLE_DUPLICATE"]
    assert len(groups) == 1
    assert groups[0].count == 2
    assert sorted(groups[0].urls) == ["https://ex.test/a", "https://ex.test/b"]
    assert "TITLE_DUPLICATE" in ctx._fired_ids
