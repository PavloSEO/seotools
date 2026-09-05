"""A terminal Yandex operation-read failure must surface, never hide behind a timeout note.

Once `searchAsync` accepts a submission, the operation ID is billed and journaled. A later poll
that receives a terminal HTTP status (404/400) for that operation is a concrete, already-known
failure — not silence. It must become an explicit `operation_read_error` result carrying the
operation ID and provider status, and must never be counted among the genuinely unfinished
operations that `not_returned` and its billed-timeout note describe.
"""

from __future__ import annotations

import pytest

from seohead.data_sources import spend, yandex_cloud
from seohead.servers import handlers


@pytest.fixture()
def journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SEOHEAD_SPEND_LOG", str(tmp_path / "spend.jsonl"))
    return tmp_path / "spend.jsonl"


class _ScriptedClient(yandex_cloud.WebSearch):
    """A WebSearch double whose `_request` responses are supplied in call order, no network."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._next = 0
        self.calls = []
        self.folder = "synthetic-folder"

    def _request(self, url, body=None, method="POST", retries=5, *, billed=False, operation=None):
        self.calls.append(url)
        response = self._responses[min(self._next, len(self._responses) - 1)]
        self._next += 1
        return response


def test_terminal_404_after_accepted_submission_is_an_explicit_read_error(journal):
    client = _ScriptedClient(
        [
            (200, {"id": "op-1"}),  # searchAsync accepted
            (404, {"message": "operation not found"}),  # polling read failure
        ]
    )

    result = client.search_batch(["synthetic query"], timeout=5, poll=0)

    assert result == {
        "synthetic query": {
            "error": "operation not found",
            "status": "operation_read_error",
            "operation_id": "op-1",
            "http_status": 404,
            "docs": [],
        }
    }


def test_terminal_400_after_accepted_submission_is_also_a_read_error(journal):
    client = _ScriptedClient(
        [
            (200, {"id": "op-2"}),
            (400, {"message": "bad operation id"}),
        ]
    )
    result = client.search_batch(["another query"], timeout=5, poll=0)
    assert result["another query"]["status"] == "operation_read_error"
    assert result["another query"]["operation_id"] == "op-2"
    assert result["another query"]["http_status"] == 400


def test_completed_operation_with_error_field_is_still_operation_error(journal):
    client = _ScriptedClient(
        [
            (200, {"id": "op-3"}),
            (200, {"done": True, "error": {"message": "provider-side failure"}}),
        ]
    )
    result = client.search_batch(["ok submission, failed operation"], timeout=5, poll=0)
    entry = result["ok submission, failed operation"]
    assert entry["status"] == "operation_error"
    assert entry["operation_id"] == "op-3"
    assert entry["error"] == {"message": "provider-side failure"}


def test_genuinely_unfinished_operation_is_the_only_case_left_pending(journal):
    client = _ScriptedClient(
        [
            (200, {"id": "op-4"}),
            (200, {"done": False}),  # still running when the short deadline hits
        ]
    )
    result = client.search_batch(["still running"], timeout=0.05, poll=0.02)
    assert result == {}  # Absent, not misreported as a read error.


def test_operation_read_error_survives_the_handler_boundary_and_is_not_billed_as_timeout(
    monkeypatch, journal
):
    client = _ScriptedClient(
        [
            (200, {"id": "op-5"}),
            (404, {"message": "operation not found"}),
        ]
    )
    monkeypatch.setattr(yandex_cloud, "WebSearch", lambda: client)

    result = handlers.serp_fetch(query="synthetic query")

    assert result["ok"] is True
    assert result["not_returned"] == []
    assert result["note"] is None
    entry = result["results"]["synthetic query"]
    assert entry["status"] == "operation_read_error"
    assert entry["operation_id"] == "op-5"
    assert entry["http_status"] == 404

    journal_entry = spend.read_all()[0]
    assert journal_entry["extra"]["operation_ids"] == {"synthetic query": "op-5"}
