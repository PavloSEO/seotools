"""Credential resume context never records secrets, paths, or environment names."""

from __future__ import annotations

import copy

import pytest

from seohead.crawl.settings import load, validate
from seohead.storage import ScanError
from seohead.storage.credential_context import (
    credential_verifier,
    redact_config,
    validate_recorded_credentials,
)


def _settings() -> dict:
    settings = load(overrides={"speed.min_delay_seconds": 0})
    settings["http"]["credential_headers"] = [
        {"host": "Example.test", "headers": {"Authorization": "env:TEST_SECRET"}}
    ]
    settings["http"]["credentials_acknowledged"] = True
    settings["rendering"]["browser"]["persistent_profile"] = True
    settings["rendering"]["browser"]["persistent_profile_dir"] = "/private/profile"
    return settings


def test_redaction_is_deep_and_never_leaks_environment_or_profile_path(monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "actual secret")
    live = _settings()
    recorded = redact_config(live)
    assert recorded["http"]["credential_headers"][0]["headers"] == {"Authorization": "REDACTED"}
    assert recorded["rendering"]["browser"]["persistent_profile_dir"] == "REDACTED"
    assert "TEST_SECRET" not in repr(recorded)
    assert "actual secret" not in repr(recorded)
    assert "private/profile" not in repr(recorded)
    assert live["http"]["credential_headers"][0]["headers"]["Authorization"] == "env:TEST_SECRET"


def test_redaction_defensively_hides_credentials_from_a_legacy_generic_header_mapping():
    live = _settings()
    live["http"]["headers"] = {"Cookie": "dummy-inline-value", "Accept-Language": "de"}
    recorded = redact_config(live)
    assert recorded["http"]["headers"] == {"Cookie": "REDACTED", "Accept-Language": "de"}
    assert live["http"]["headers"]["Cookie"] == "dummy-inline-value"


def test_recorded_snapshot_validates_without_environment_and_yields_safe_settings_copy(monkeypatch):
    monkeypatch.delenv("TEST_SECRET", raising=False)
    recorded = redact_config(_settings())
    safe = validate_recorded_credentials(recorded)
    validate(safe)
    assert safe["http"]["credential_headers"] == []
    assert safe["rendering"]["browser"]["persistent_profile"] is False
    assert safe["rendering"]["browser"]["persistent_profile_dir"] == ""


def test_verifier_is_stable_for_same_live_context_and_changes_with_secret_or_host(monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "one")
    settings = _settings()
    first = credential_verifier(settings, "12345678-1234-5678-1234-567812345678")
    assert first == credential_verifier(
        copy.deepcopy(settings), "12345678-1234-5678-1234-567812345678"
    )
    monkeypatch.setenv("TEST_SECRET", "two")
    assert first != credential_verifier(settings, "12345678-1234-5678-1234-567812345678")
    settings["http"]["credential_headers"][0]["host"] = "other.test"
    assert first != credential_verifier(settings, "12345678-1234-5678-1234-567812345678")


def test_verifier_is_none_without_explicit_headers_and_rejects_unredacted_snapshot():
    assert (
        credential_verifier(
            load(overrides={"speed.min_delay_seconds": 0}), "12345678-1234-5678-1234-567812345678"
        )
        is None
    )
    recorded = redact_config(_settings())
    recorded["http"]["credential_headers"][0]["headers"]["Authorization"] = "env:LEAK"
    with pytest.raises(ScanError, match="redacted"):
        validate_recorded_credentials(recorded)
