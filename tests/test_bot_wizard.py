"""Synthetic end-to-end runs of the guided scan wizard.

Every fixture is synthetic: a recording submitter stands in for the job
queue, a mutable clock drives expiry, and example.com URLs are normalized
offline — no network, no crawl, no Telegram.
"""

from __future__ import annotations

import pytest

from seohead.bot import (
    POLICY_PRESETS,
    Action,
    Event,
    ScanJobSpec,
    State,
    WizardSession,
)
from seohead.crawl import settings as crawl_settings


class RecordingSubmitter:
    """Stands in for the adapter's JobSubmitter. Records, never crawls."""

    def __init__(self) -> None:
        self.specs: list[ScanJobSpec] = []
        self.cancelled: list[str] = []

    def submit(self, spec: ScanJobSpec) -> str:
        self.specs.append(spec)
        return f"job-{len(self.specs)}"

    def cancel(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


@pytest.fixture
def clock():
    now = [0.0]
    return now


@pytest.fixture
def session(clock):
    return WizardSession(
        RecordingSubmitter(),
        projects=("alpha-site", "beta-shop"),
        ttl_seconds=600.0,
        clock=lambda: clock[0],
    )


def answer(session, text):
    return session.handle(Event(Action.ANSWER, text))


def walk_to_preview(session) -> str:
    """Drive site -> project -> policy -> report; return the preview text."""
    answer(session, "https://example.com")
    answer(session, "alpha-site")
    answer(session, "standard")
    reply = answer(session, "xlsx problems-only")
    assert reply.state == State.PREVIEW
    return reply.text


def test_happy_path_submits_exactly_the_previewed_config(session):
    reply = answer(session, "https://example.com")
    assert reply.state == State.AWAITING_PROJECT
    answer(session, "alpha-site")
    answer(session, "standard")
    reply = answer(session, "xlsx problems-only")
    assert reply.state == State.PREVIEW
    preview_text = reply.text
    assert "limits.max_urls = 200" in preview_text
    assert "config fingerprint:" in preview_text

    reply = session.handle(Event(Action.CONFIRM))
    assert reply.state == State.CONFIRMING
    reply = session.handle(Event(Action.CONFIRM))
    assert reply.state == State.RUNNING

    (spec,) = session._submitter.specs
    assert spec.url == "https://example.com"
    assert spec.project == "alpha-site"
    assert spec.report == {"format": "xlsx", "problems_only": True}
    # The submitted config is the resolved config the preview showed.
    assert spec.config == crawl_settings.load(overrides=POLICY_PRESETS["standard"])
    assert spec.fingerprint in preview_text
    assert spec.manifest == crawl_settings.manifest(spec.config)


def test_invalid_answer_stays_and_never_submits(session):
    reply = answer(session, "not a url at all")
    assert reply.state == State.AWAITING_SITE
    assert reply.notice is not None
    answer(session, "https://example.com")
    reply = answer(session, "no-such-project")
    assert reply.state == State.AWAITING_PROJECT
    assert "Unknown project" in reply.notice
    answer(session, "alpha-site")
    reply = answer(session, "ludicrous")
    assert reply.state == State.AWAITING_POLICY
    answer(session, "quick")
    reply = answer(session, "pdf")
    assert reply.state == State.AWAITING_REPORT
    assert session._submitter.specs == []


def test_disallowed_action_stays(session):
    reply = session.handle(Event(Action.CONFIRM))
    assert reply.state == State.AWAITING_SITE
    assert "not available" in reply.notice
    assert session._submitter.specs == []


def test_back_and_edit_round_trip(session):
    answer(session, "https://example.com")
    answer(session, "alpha-site")
    answer(session, "standard")
    reply = session.handle(Event(Action.BACK))
    assert reply.state == State.AWAITING_POLICY
    answer(session, "standard")
    reply = answer(session, "md")
    assert reply.state == State.PREVIEW
    old = session._effective["fingerprint"]
    reply = session.handle(Event(Action.EDIT, "policy"))
    assert reply.state == State.AWAITING_POLICY
    reply = answer(session, "thorough")
    assert reply.state == State.PREVIEW
    assert session._effective["fingerprint"] != old
    assert "limits.max_urls = 1000" in reply.text


def test_cancel_before_submit_discards_draft(session):
    walk_to_preview(session)
    reply = session.handle(Event(Action.CANCEL))
    assert reply.state == State.CANCELLED
    assert session._submitter.specs == []


def test_cancel_running_job_goes_through_submitter(session):
    walk_to_preview(session)
    session.handle(Event(Action.CONFIRM))
    session.handle(Event(Action.CONFIRM))
    reply = session.handle(Event(Action.CANCEL))
    assert reply.state == State.CANCELLED
    assert session._submitter.cancelled == ["job-1"]


def test_expired_session_resets_without_submitting(session, clock):
    walk_to_preview(session)
    clock[0] += 900.0
    reply = session.handle(Event(Action.CONFIRM))
    assert reply.state == State.AWAITING_SITE
    assert "expired" in reply.notice
    assert session._submitter.specs == []


def test_expired_running_job_is_cancelled(session, clock):
    walk_to_preview(session)
    session.handle(Event(Action.CONFIRM))
    session.handle(Event(Action.CONFIRM))
    clock[0] += 900.0
    reply = session.handle(Event(Action.PROGRESS, "50%"))
    assert reply.state == State.AWAITING_SITE
    assert session._submitter.cancelled == ["job-1"]


def test_progress_and_finish(session):
    walk_to_preview(session)
    session.handle(Event(Action.CONFIRM))
    session.handle(Event(Action.CONFIRM))
    reply = session.handle(Event(Action.PROGRESS, "120/200 URLs"))
    assert "120/200" in reply.text
    reply = session.handle(Event(Action.FINISH, "report ready"))
    assert reply.state == State.DONE
    assert "report ready" in reply.text


def test_rerun_repreviews_and_resubmits_same_config(session):
    walk_to_preview(session)
    session.handle(Event(Action.CONFIRM))
    session.handle(Event(Action.CONFIRM))
    session.handle(Event(Action.FINISH))
    reply = session.handle(Event(Action.RERUN))
    assert reply.state == State.PREVIEW
    session.handle(Event(Action.CONFIRM))
    session.handle(Event(Action.CONFIRM))
    first, second = session._submitter.specs
    assert second.fingerprint == first.fingerprint
    assert second.config == first.config
    assert session._submitter.cancelled == []


def test_help_is_a_stay(session):
    reply = session.handle(Event(Action.HELP))
    assert reply.state == State.AWAITING_SITE
    assert "site" in reply.text.lower()
