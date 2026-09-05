"""A billed Arsenkin `SET_TASK_OK` response must carry a usable task_id or fail structurally.

`task_id` is the only mechanism `get()`/`refetch()` have for retrieving an already-paid result.
A response missing, nulling, or malforming it must never be advertised as a recoverable task: the
charge is still journaled (as a clearly marked temporary entry, with no invented ID), and the
caller gets a structured provider error instead of a task it can never fetch.
"""

from __future__ import annotations

import pytest

from seohead.data_sources import arsenkin, spend
from seohead.servers import handlers


@pytest.fixture()
def journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SEOHEAD_SPEND_LOG", str(tmp_path / "spend.jsonl"))
    return tmp_path / "spend.jsonl"


class _StubClient(arsenkin.ArsenkinClient):
    """A client double that returns a fixed `/set` response without any network call."""

    def __init__(self, set_response):
        self._set_response = set_response
        self.checked = []

    def _post(self, endpoint, body, retries=5, *, billed=False):
        assert endpoint == "set"
        return self._set_response


_MISSING = object()  # Sentinel: the key is absent from the response, not set to None.


@pytest.mark.parametrize(
    "raw_task_id",
    [_MISSING, None, "abc", "--1", "²", "9" * 5_000, True, False, 0, -5, [], {}],
    ids=[
        "missing",
        "null",
        "non_numeric",
        "double_negative",
        "non_ascii_digit",
        "overlong_integer",
        "true",
        "false",
        "zero",
        "negative",
        "list",
        "dict",
    ],
)
def test_set_task_rejects_unusable_task_ids(journal, raw_task_id):
    response = {"code": "SET_TASK_OK", "cost": 7}
    if raw_task_id is not _MISSING:
        response["task_id"] = raw_task_id
    client = _StubClient(response)

    with pytest.raises(arsenkin.ArsenkinError) as excinfo:
        client.set_task("keywords_frequency", {"keywords": ["synthetic phrase"]})
    assert excinfo.value.code == "INVALID_TASK_ID"

    entries = spend.read_all()
    assert len(entries) == 1
    assert entries[0]["cost"] == 7.0
    assert "task_id" not in entries[0]
    assert entries[0]["extra"]["temporary"] is True


def test_set_task_missing_task_id_journals_cost_as_temporary_with_no_invented_id(journal):
    client = _StubClient({"code": "SET_TASK_OK", "cost": 7})

    with pytest.raises(arsenkin.ArsenkinError):
        client.set_task("keywords_frequency", {"keywords": ["synthetic phrase"]})

    entries = spend.read_all()
    assert entries == [
        {
            "at": entries[0]["at"],
            "source": "arsenkin",
            "operation": "keywords_frequency",
            "cost": 7.0,
            "unit": "limits",
            "items": 1,
            "extra": {
                "temporary": True,
                "reason": "set_task_ok_missing_task_id",
                "received_task_id": None,
            },
        }
    ]


def test_set_task_keeps_a_valid_task_id(journal):
    client = _StubClient({"code": "SET_TASK_OK", "task_id": 555, "cost": 12})
    task = client.set_task("keywords_frequency", {"keywords": ["synthetic phrase"]})
    assert task == {
        "task_id": 555,
        "cost": 12,
        "raw": {"code": "SET_TASK_OK", "task_id": 555, "cost": 12},
    }
    entries = spend.read_all()
    assert entries[0]["task_id"] == 555
    assert entries[0]["cost"] == 12.0
    assert "extra" not in entries[0]


def test_set_task_accepts_a_numeric_string_id_because_check_coerces_it(journal):
    client = _StubClient({"code": "SET_TASK_OK", "task_id": "555", "cost": 12})
    task = client.set_task("keywords_frequency", {"keywords": ["synthetic phrase"]})
    assert task["task_id"] == 555


@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize("task_id", [None, "--1", "²", "9" * 5_000])
def test_keywords_exact_returns_structured_error_for_both_wait_modes(
    monkeypatch, journal, wait, task_id
):
    """Both wait=False and wait=True must fail structurally, never leak a raw TypeError."""
    response = {"code": "SET_TASK_OK", "cost": 7}
    if task_id is not None:
        response["task_id"] = task_id
    client = _StubClient(response)
    monkeypatch.setattr(arsenkin, "ArsenkinClient", lambda: client)

    result = handlers.keywords_exact(keywords=["synthetic phrase"], wait=wait)

    assert result["ok"] is False
    assert result["code"] == "INVALID_TASK_ID"
    assert "task_id" not in result

    entries = spend.read_all()
    assert entries[0]["cost"] == 7.0
    assert "task_id" not in entries[0]
    assert entries[0]["extra"]["temporary"] is True
