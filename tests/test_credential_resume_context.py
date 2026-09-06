"""Credential resume context is a closed verifier record, never a secret store."""

from __future__ import annotations

import json

import pytest

from seohead.storage import ScanError
from seohead.storage.native_context import validate_context


def _item(payload, **overrides):
    item = {
        "kind": "credential_context",
        "item_key": "run",
        "payload_version": "scan_context.v1",
        "payload_json": json.dumps(payload),
        "completeness": "complete",
        "reason": "",
    }
    item.update(overrides)
    return item


@pytest.mark.parametrize(
    "payload",
    [
        {"verifier": None, "implicit_state": False},
        {"verifier": "a" * 64, "implicit_state": True},
    ],
)
def test_credential_context_accepts_only_verifier_or_implicit_marker(payload):
    validate_context(None, _item(payload))


@pytest.mark.parametrize(
    "item",
    [
        _item({"verifier": "secret", "implicit_state": False}),
        _item({"verifier": "A" * 64, "implicit_state": False}),
        _item({"verifier": None, "implicit_state": 1}),
        _item({"verifier": None, "implicit_state": False, "token": "secret"}),
        _item({"verifier": None, "implicit_state": False}, item_key="url:1"),
        _item({"verifier": None, "implicit_state": False}, reason="secret"),
    ],
)
def test_credential_context_refuses_secret_shaped_or_ambiguous_values(item):
    with pytest.raises(ScanError, match="credential context"):
        validate_context(None, item)
