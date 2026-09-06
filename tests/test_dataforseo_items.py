"""Regression coverage for DataForSEO keyword-result normalization."""

from __future__ import annotations

import pytest

from seohead.data_sources import dataforseo


@pytest.mark.parametrize(
    ("result_item", "expected_keywords"),
    [
        ({"items": None}, []),
        ({"items": []}, []),
        (
            {
                "items": [
                    {
                        "keyword": "technical seo",
                        "keyword_info": {"search_volume": 100},
                        "keyword_properties": {"keyword_difficulty": 42},
                    },
                    "not a keyword item",
                ]
            },
            [{"phrase": "technical seo", "volume": 100, "difficulty": 42}],
        ),
        (
            {"keyword": "direct result"},
            [{"phrase": "direct result", "volume": None, "difficulty": None}],
        ),
    ],
)
def test_keyword_ideas_distinguishes_empty_items_from_direct_results(
    monkeypatch, result_item, expected_keywords
):
    class FakeClient:
        env = "sandbox"

        def __init__(self, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            return {
                "cost": 0,
                "tasks": [{"status_code": 20000, "result": [result_item]}],
            }

    monkeypatch.setattr(dataforseo, "DataForSEOClient", FakeClient)

    response = dataforseo.keyword_ideas("synthetic seed")

    assert response["found"] == len(expected_keywords)
    assert response["keywords"] == expected_keywords


def test_search_volume_reports_ok_false_when_every_task_failed(monkeypatch):
    """A task-level rejection (bad geo, bad language, exhausted balance) must not read as clean."""

    class FakeClient:
        env = "sandbox"

        def __init__(self, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            return {
                "tasks": [
                    {
                        "status_code": 40501,
                        "status_message": "Invalid Field: 'location_code'",
                        "result": None,
                    }
                ]
            }

    monkeypatch.setattr(dataforseo, "DataForSEOClient", FakeClient)

    response = dataforseo.search_volume(["buy shoes"], location_code=999999999)

    assert response["ok"] is False  # handler_failed() must catch this without changes elsewhere.
    assert response["keywords"] == []
    assert response["errors"]


def test_search_volume_stays_ok_true_for_a_genuine_zero_result(monkeypatch):
    """Negative control: a task that succeeds with zero matching items stays a clean, silent ok."""

    class FakeClient:
        env = "sandbox"

        def __init__(self, **_kwargs):
            pass

        def post(self, *_args, **_kwargs):
            return {"tasks": [{"status_code": 20000, "result": [{"items": []}]}]}

    monkeypatch.setattr(dataforseo, "DataForSEOClient", FakeClient)

    response = dataforseo.search_volume(["a query with no matches"])

    assert response["ok"] is True
    assert response["keywords"] == []
