"""The journal must record every call, leak no secrets, and never break a run."""

import json
import time

import pytest

from seohead import runlog


@pytest.fixture(autouse=True)
def journal_path(tmp_path, monkeypatch):
    path = tmp_path / "runs.jsonl"
    monkeypatch.setenv("SEOHEAD_RUN_LOG", str(path))
    return path


def test_a_successful_call_is_recorded(journal_path):
    with runlog.journal("cli", "parse", {"url": "https://example.com/"}) as facts:
        facts["count"] = 3
    entry = runlog.read_entries()[0]
    assert entry["tool"] == "parse"
    assert entry["interface"] == "cli"
    assert entry["ok"] is True
    assert entry["count"] == 3
    assert entry["duration_s"] >= 0


def test_a_failing_call_is_recorded_and_the_error_still_propagates(journal_path):
    with pytest.raises(ValueError), runlog.journal("cli", "parse", {"url": "x"}):
        raise ValueError("boom")
    entry = runlog.read_entries()[0]
    assert entry["ok"] is False
    assert "boom" in entry["error"]


@pytest.mark.parametrize(
    "name", ["api_key", "token", "AUTH", "password", "client_secret", "credentials"]
)
def test_credentials_never_reach_the_journal(journal_path, name):
    """A journal that leaks a key is worse than no journal, and leaks silently."""
    with runlog.journal("cli", "serp", {name: "super-secret-value", "url": "https://e.com/"}):
        pass
    text = journal_path.read_text()
    assert "super-secret-value" not in text
    assert "[redacted]" in text


def test_long_values_are_shortened_not_dropped(journal_path):
    with runlog.journal("cli", "parse", {"html": "x" * 5000}):
        pass
    value = runlog.read_entries()[0]["arguments"]["html"]
    assert value.endswith("…")
    assert len(value) < 500


def test_long_lists_are_summarised(journal_path):
    with runlog.journal("cli", "parse", {"urls": [f"u{i}" for i in range(50)]}):
        pass
    value = runlog.read_entries()[0]["arguments"]["urls"]
    assert len(value) == 11
    assert "40 more" in value[-1]


def test_the_same_call_gets_the_same_fingerprint():
    first = runlog.fingerprint("parse", {"url": "https://e.com/", "depth": 2})
    again = runlog.fingerprint("parse", {"depth": 2, "url": "https://e.com/"})
    other = runlog.fingerprint("parse", {"url": "https://e.com/x", "depth": 2})
    assert first == again, "argument order must not change identity"
    assert first != other


def test_logging_can_be_switched_off(monkeypatch, tmp_path):
    monkeypatch.setenv("SEOHEAD_RUN_LOG", "off")
    assert runlog.log_path() is None
    with runlog.journal("cli", "parse", {"url": "x"}):
        pass
    assert runlog.read_entries() == []


def test_an_unwritable_journal_does_not_break_the_run(monkeypatch, tmp_path):
    """A degraded observation must never fail an audit."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    # The parent of the journal is a regular file, so creating it must fail.
    monkeypatch.setenv("SEOHEAD_RUN_LOG", str(blocker / "runs.jsonl"))
    with runlog.journal("cli", "parse", {"url": "x"}):
        pass  # must not raise
    assert runlog.read_entries() == []


def test_a_truncated_final_line_does_not_hide_earlier_entries(journal_path):
    with runlog.journal("cli", "first", {}):
        pass
    with open(journal_path, "a", encoding="utf-8") as handle:
        handle.write('{"broken": ')
    entries = runlog.read_entries()
    assert [e["tool"] for e in entries] == ["first"]


def test_every_handler_is_journalled():
    """Registry-level wrapping is what makes this impossible to forget."""
    from seohead.servers.handlers import HANDLERS

    for name, fn in HANDLERS.items():
        assert getattr(fn, "__wrapped__", None) is not None, f"{name} is not journalled"


def test_a_handler_call_writes_exactly_one_entry(journal_path):
    from seohead.servers.handlers import HANDLERS

    with pytest.raises(ValueError):
        HANDLERS["parse"]()  # missing required argument
    assert len(journal_path.read_text().strip().splitlines()) == 1


def test_the_tools_own_ok_is_kept_apart_from_the_calls_ok(journal_path):
    """ "The call did not raise" and "the tool found a usable result" differ."""
    with runlog.journal("cli", "t", {}) as facts:
        facts["result_ok"] = False
    entry = runlog.read_entries()[0]
    assert entry["ok"] is True
    assert entry["result_ok"] is False


def test_entries_are_newest_first_and_limited(journal_path):
    for n in range(5):
        with runlog.journal("cli", f"t{n}", {}):
            pass
    assert [e["tool"] for e in runlog.read_entries(limit=2)] == ["t4", "t3"]


def test_the_journal_is_valid_jsonl(journal_path):
    with runlog.journal("cli", "parse", {"url": "https://e.com/"}):
        pass
    for line in journal_path.read_text().splitlines():
        json.loads(line)


# ── journal-driven reuse (#16) ────────────────────────────────────────────────


def test_with_no_policy_configured_nothing_is_ever_reused(journal_path, monkeypatch):
    """The acceptance criterion, stated directly: no policy means no reuse, ever."""
    monkeypatch.delenv("SEOHEAD_REUSE_POLICY", raising=False)
    calls = []

    def fn(**kwargs):
        calls.append(kwargs)
        return {"value": len(calls)}

    wrapped = runlog.journaled("domain_profile", fn)
    first = wrapped(domain="example.com")
    second = wrapped(domain="example.com")
    assert len(calls) == 2, "an unconfigured tool must be called every time"
    assert "reused" not in first
    assert "reused" not in second


def test_a_configured_tool_reuses_a_fresh_answer_without_calling_the_function_again(
    journal_path, monkeypatch
):
    monkeypatch.setenv("SEOHEAD_REUSE_POLICY", json.dumps({"domain_profile": 3600}))
    calls = []

    def fn(**kwargs):
        calls.append(kwargs)
        return {"registrar": "Example Registrar Inc."}

    wrapped = runlog.journaled("domain_profile", fn)
    first = wrapped(domain="example.com")
    second = wrapped(domain="example.com")

    assert len(calls) == 1, "a fresh, matching answer must not be re-fetched"
    assert "reused" not in first
    assert second["reused"] is True
    assert second["registrar"] == "Example Registrar Inc."
    assert "reused_from_ts" in second

    entries = runlog.read_entries()
    assert entries[0]["reused"] is True
    assert entries[0]["tool"] == "domain_profile"


def test_a_reuse_policy_only_applies_to_the_tool_it_names(journal_path, monkeypatch):
    """Reuse is per-tool, never a global switch."""
    monkeypatch.setenv("SEOHEAD_REUSE_POLICY", json.dumps({"domain_profile": 3600}))
    calls = []

    def fn(**kwargs):
        calls.append(kwargs)
        return {"count": len(calls)}

    wrapped = runlog.journaled("parse", fn)
    wrapped(url="https://example.com/")
    wrapped(url="https://example.com/")
    assert len(calls) == 2, "a page a client just fixed must not be answered from memory"


def test_different_arguments_are_not_reused(journal_path, monkeypatch):
    monkeypatch.setenv("SEOHEAD_REUSE_POLICY", json.dumps({"domain_profile": 3600}))
    calls = []

    def fn(**kwargs):
        calls.append(kwargs)
        return {"domain": kwargs["domain"]}

    wrapped = runlog.journaled("domain_profile", fn)
    wrapped(domain="example.com")
    result = wrapped(domain="example.org")
    assert len(calls) == 2
    assert "reused" not in result


def test_an_expired_answer_is_measured_again_not_reused(journal_path, monkeypatch):
    monkeypatch.setenv("SEOHEAD_REUSE_POLICY", json.dumps({"domain_profile": 60}))
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3600))
    runlog.record(
        {
            "ts": old_ts,
            "interface": "cli",
            "tool": "domain_profile",
            "arguments": runlog.safe_arguments({"domain": "example.com"}),
            "fingerprint": runlog.fingerprint("domain_profile", {"domain": "example.com"}),
            "duration_s": 0.1,
            "ok": True,
            "error": None,
            "result": {"registrar": "Stale Registrar"},
        }
    )
    calls = []

    def fn(**kwargs):
        calls.append(kwargs)
        return {"registrar": "Fresh Registrar"}

    wrapped = runlog.journaled("domain_profile", fn)
    result = wrapped(domain="example.com")
    assert len(calls) == 1, "an answer older than the configured maximum age must be re-measured"
    assert result["registrar"] == "Fresh Registrar"
    assert "reused" not in result


def test_reuse_policy_is_never_reused_across_a_reuse(journal_path, monkeypatch):
    """A reused answer's own journal entry carries no result, so it cannot itself be replayed
    forever — freshness is always measured against when the value was actually fetched."""
    monkeypatch.setenv("SEOHEAD_REUSE_POLICY", json.dumps({"domain_profile": 3600}))
    calls = []

    def fn(**kwargs):
        calls.append(kwargs)
        return {"registrar": "Example Registrar Inc."}

    wrapped = runlog.journaled("domain_profile", fn)
    wrapped(domain="example.com")
    wrapped(domain="example.com")
    wrapped(domain="example.com")
    assert len(calls) == 1
    entries = runlog.read_entries()
    assert "result" not in entries[0]  # the most recent (reused) entry
    assert entries[0]["reused_from_ts"] == entries[-1]["ts"]


def test_a_malformed_reuse_policy_disables_reuse_rather_than_raising(journal_path, monkeypatch):
    monkeypatch.setenv("SEOHEAD_REUSE_POLICY", "{not json")
    assert runlog.reuse_policy() == {}


def test_reuse_never_stores_a_secret_looking_field(journal_path, monkeypatch):
    monkeypatch.setenv("SEOHEAD_REUSE_POLICY", json.dumps({"domain_profile": 3600}))

    def fn(**kwargs):
        return {"registrar": "Example Registrar Inc.", "api_key": "super-secret-value"}

    wrapped = runlog.journaled("domain_profile", fn)
    wrapped(domain="example.com")
    assert "super-secret-value" not in journal_path.read_text()


def test_an_argument_json_cannot_encode_is_named_rather_than_raising(journal_path):
    """A crawl-site progress callback (#619) is a live object, not JSON.

    ``fingerprint`` json.dumps the arguments with no ``default``, so passing the
    object through would raise there and fail every call of that tool -- the
    journal is meant to degrade, never to break a run.
    """

    class Reporter:
        pass

    with runlog.journal(
        "cli", "crawl_site", {"url": "https://example.com/", "progress": Reporter()}
    ):
        pass
    entry = runlog.read_entries()[0]
    assert entry["arguments"]["progress"] == "<Reporter>"
    assert entry["arguments"]["url"] == "https://example.com/"


def test_the_same_call_fingerprints_the_same_with_a_live_object_in_it():
    """A type name, not str(value): an object's repr carries a memory address,
    and an address would make one repeated call look like two different ones."""

    class Reporter:
        pass

    first = runlog.fingerprint(
        "crawl_site", {"url": "https://example.com/", "progress": Reporter()}
    )
    second = runlog.fingerprint(
        "crawl_site", {"url": "https://example.com/", "progress": Reporter()}
    )
    assert first == second


def test_a_path_argument_is_still_journalled_as_its_path(journal_path, tmp_path):
    """PathLike is JSON-able once, and the path is the useful fact about it."""
    with runlog.journal("library", "sf_audit_run", {"out": tmp_path / "report"}):
        pass
    assert runlog.read_entries()[0]["arguments"]["out"] == str(tmp_path / "report")
