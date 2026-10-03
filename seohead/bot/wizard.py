"""Session driver for the guided scan wizard (contract in ``contract.py``).

The driver is a pure state machine over injected dependencies: it renders
``Reply`` objects (text + button tokens) for an adapter to display, resolves
the effective crawl configuration through ``seohead.crawl.settings`` — the
same loader the CLI uses — and hands the confirmed job to a ``JobSubmitter``.
It never fetches a URL, queues a crawl or renders a report itself, and it
never holds Telegram identifiers: the adapter owns identity, the driver owns
the conversation.

Two invariants the acceptance of issue #769 depends on:

* **Preview is submission.** The effective configuration is resolved once,
  when ``preview`` is entered, and that same ``config``/``manifest``/
  ``fingerprint`` triple is what ``JobSubmitter.submit`` receives. Confirm
  cannot silently drift from what the user was shown.
* **Nothing starts on a bad step.** Invalid answers, disallowed actions and
  expired sessions produce a reply and stay — or reset with a notice — and
  never reach the submitter.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from seohead.bot.contract import (
    CONTRACT_VERSION,
    FIELD_STATES,
    Action,
    Field,
    State,
    allowed_actions,
    next_state,
)
from seohead.crawl import settings as crawl_settings
from seohead.recon.net import normalize_domain, normalize_url
from seohead.reports import FORMATS as REPORT_FORMATS

#: Named scan policies the wizard offers. Values are dotted-path overrides
#: for ``seohead.crawl.settings.load`` — resolved and validated there, never
#: trusted as literals here.
POLICY_PRESETS: dict[str, dict[str, Any]] = {
    "quick": {
        "limits.max_urls": 50,
        "limits.max_depth": 2,
        "rendering.mode": "raw",
    },
    "standard": {
        "limits.max_urls": 200,
        "limits.max_depth": 5,
        "rendering.mode": "raw",
    },
    "thorough": {
        "limits.max_urls": 1000,
        "limits.max_depth": 8,
        "limits.max_crawl_seconds": 1800,
        "rendering.mode": "js",
    },
}

#: Default idle time-to-live in seconds; an event after this resets the
#: conversation with an expiry notice instead of resuming a stale draft.
DEFAULT_TTL_SECONDS = 900.0


@dataclass(frozen=True)
class Event:
    """One input to the machine. ``value`` carries the free-text answer,
    the field name for ``edit``, or the progress payload."""

    action: Action
    value: str | None = None


@dataclass(frozen=True)
class Reply:
    """One output of the machine, rendered by the adapter.

    ``buttons`` are contract tokens (``"back"``, ``"confirm"``, a preset
    name, a report format), not rendered markup — the adapter owns markup.
    """

    state: State
    text: str
    buttons: tuple[str, ...] = ()
    notice: str | None = None


@dataclass
class ScanJobSpec:
    """The confirmed job handed to the submitter.

    ``config`` is the resolved crawl configuration exactly as previewed;
    ``manifest`` and ``fingerprint`` are its ``settings.manifest`` /
    ``settings.fingerprint`` output so the worker can record what was agreed
    without re-deriving it.
    """

    url: str
    project: str
    policy: str
    report: dict[str, Any]
    config: dict[str, Any]
    manifest: dict[str, Any]
    fingerprint: str
    contract_version: str = CONTRACT_VERSION


class JobSubmitter(Protocol):
    """Boundary between the conversation and the core.

    The wizard calls this once per confirmed job and once per cancellation;
    the implementation lives in the adapter and delegates to the shared
    handlers/queue. Keeping it a protocol is what guarantees no UI handler
    implements crawl logic.
    """

    def submit(self, spec: ScanJobSpec) -> str:
        """Enqueue the job and return its job id."""
        ...

    def cancel(self, job_id: str) -> bool:
        """Request cancellation; False when the job already finished."""
        ...


_HELP: dict[State, str] = {
    State.AWAITING_SITE: "Send the address of the site to scan, e.g. https://example.com.",
    State.AWAITING_PROJECT: "Pick the project this scan belongs to, or start a new one.",
    State.AWAITING_POLICY: "Pick a scan policy: it sets the URL/depth/time budgets and rendering mode.",
    State.AWAITING_REPORT: "Pick the report format for the finished audit.",
    State.PREVIEW: "Check the effective settings. Confirm to launch, edit a step, or go back.",
    State.CONFIRMING: "Explicit final approval: press start to submit the job.",
    State.DONE: "The job finished. Rerun the same configuration or start a new scan.",
    State.CANCELLED: "The draft was cancelled. Start over whenever you are ready.",
}


class WizardSession:
    """One guided conversation. One instance per (user, chat) — the adapter
    keys it; the driver never sees platform identifiers."""

    def __init__(
        self,
        submitter: JobSubmitter,
        *,
        projects: tuple[str, ...] = (),
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._submitter = submitter
        self._projects = tuple(projects)
        self._ttl = ttl_seconds
        self._clock = clock
        self._last_activity = clock()
        self.state = State.AWAITING_SITE
        self.draft: dict[str, Any] = {}
        self.job_id: str | None = None
        self.progress: str | None = None
        self._editing = False
        self._effective: dict[str, Any] | None = None

    # -- public surface -----------------------------------------------------

    def handle(self, event: Event) -> Reply:
        """Advance the machine one step and return what to show."""
        expired = self._expired()
        self._last_activity = self._clock()
        if expired:
            return Reply(
                State.AWAITING_SITE,
                _HELP[State.AWAITING_SITE],
                self._buttons(State.AWAITING_SITE),
                notice="Session expired; the draft was discarded. Nothing was submitted.",
            )
        action, value = event.action, event.value
        if action not in allowed_actions(self.state):
            return Reply(
                self.state,
                _HELP.get(self.state, "This step is finished."),
                self._buttons(self.state),
                notice=f"Action {action.value!r} is not available here.",
            )
        if action == Action.HELP:
            return Reply(
                self.state, _HELP.get(self.state, "Guided scan setup."), self._buttons(self.state)
            )
        if action == Action.CANCEL:
            return self._cancel()
        if action == Action.BACK:
            self._editing = False
            self.state = next_state(self.state, action) or self.state
            if self.state == State.PREVIEW and self._effective is not None:
                return self._preview_reply()
            return Reply(self.state, _HELP[self.state], self._buttons(self.state))
        if action == Action.EDIT:
            return self._edit(value)
        if action == Action.ANSWER:
            return self._answer(value)
        if action == Action.CONFIRM:
            return self._confirm()
        if action == Action.RERUN:
            return self._rerun()
        if action == Action.PROGRESS:
            self.progress = value
            return Reply(self.state, f"Progress: {value}")
        if action == Action.FINISH:
            self.state = State.DONE
            return self._reply_done(value)
        raise AssertionError(f"unhandled action {action}")  # pragma: no cover

    # -- steps ----------------------------------------------------------------

    def _answer(self, value: str | None) -> Reply:
        text = (value or "").strip()
        error = self._validate_answer(text)
        if error:
            return Reply(self.state, _HELP[self.state], self._buttons(self.state), notice=error)
        self._store_answer(text)
        if self._editing:
            self._editing = False
            return self._enter_preview()
        target = next_state(self.state, Action.ANSWER)
        if target == State.PREVIEW:
            return self._enter_preview()
        self.state = target or self.state
        return Reply(self.state, _HELP[self.state], self._buttons(self.state))

    def _validate_answer(self, text: str) -> str | None:
        if self.state == State.AWAITING_SITE:
            url = normalize_url(text)
            if url and normalize_domain(url):
                return None
            return "That is not a usable site address."
        if self.state == State.AWAITING_PROJECT:
            if text.lower() == "new":
                return None
            if self._projects and text not in self._projects:
                return "Unknown project. Pick one of the buttons or 'new'."
            return None if text else "Pick a project."
        if self.state == State.AWAITING_POLICY:
            return (
                None
                if text in POLICY_PRESETS
                else f"Unknown policy; choose one of: {', '.join(POLICY_PRESETS)}."
            )
        if self.state == State.AWAITING_REPORT:
            fmt, _, problems = text.partition(" ")
            if fmt not in REPORT_FORMATS:
                return f"Unknown format; choose one of: {', '.join(REPORT_FORMATS)}."
            if problems and problems.strip() != "problems-only":
                return "After the format only 'problems-only' is understood."
            return None
        return "This step does not take an answer."

    def _store_answer(self, text: str) -> None:
        if self.state == State.AWAITING_SITE:
            self.draft["url"] = normalize_url(text)
        elif self.state == State.AWAITING_PROJECT:
            self.draft["project"] = (
                text
                if text.lower() != "new"
                else f"project-{normalize_url(self.draft['url']).split('//', 1)[1]}"
            )
        elif self.state == State.AWAITING_POLICY:
            self.draft["policy"] = text
        elif self.state == State.AWAITING_REPORT:
            fmt, _, problems = text.partition(" ")
            self.draft["report"] = {
                "format": fmt,
                "problems_only": problems.strip() == "problems-only",
            }

    # -- preview / confirm ----------------------------------------------------

    def _enter_preview(self) -> Reply:
        """Resolve the effective config once; this exact triple is submitted."""
        overrides = dict(POLICY_PRESETS[self.draft["policy"]])
        try:
            config = crawl_settings.load(overrides=overrides)
        except crawl_settings.ConfigError as exc:
            self.state = State.AWAITING_POLICY
            return Reply(
                self.state,
                _HELP[self.state],
                self._buttons(self.state),
                notice=f"The {self.draft['policy']} policy does not resolve: {exc}",
            )
        self._effective = {
            "config": config,
            "manifest": crawl_settings.manifest(config),
            "fingerprint": crawl_settings.fingerprint(config),
        }
        self.state = State.PREVIEW
        return self._preview_reply()

    def _preview_reply(self) -> Reply:
        assert self._effective is not None
        lines = [
            "Effective scan settings:",
            f"  site: {self.draft['url']}",
            f"  project: {self.draft['project']}",
            f"  policy: {self.draft['policy']}",
            f"  report: {self.draft['report']['format']}"
            + (" (problems only)" if self.draft["report"]["problems_only"] else ""),
        ]
        for path, value in self._effective["manifest"].items():
            lines.append(f"  {path} = {value}")
        lines.append(f"  config fingerprint: {self._effective['fingerprint']}")
        return Reply(State.PREVIEW, "\n".join(lines), self._buttons(State.PREVIEW))

    def _confirm(self) -> Reply:
        if self.state == State.PREVIEW:
            self.state = State.CONFIRMING
            return Reply(
                self.state,
                "Press start to submit the job exactly as previewed.",
                self._buttons(self.state),
            )
        # CONFIRMING: submit the spec resolved at preview time, unchanged.
        assert self._effective is not None
        spec = ScanJobSpec(
            url=self.draft["url"],
            project=self.draft["project"],
            policy=self.draft["policy"],
            report=dict(self.draft["report"]),
            config=self._effective["config"],
            manifest=self._effective["manifest"],
            fingerprint=self._effective["fingerprint"],
        )
        self.job_id = self._submitter.submit(spec)
        self.state = State.RUNNING
        return Reply(self.state, f"Job {self.job_id} submitted.", ("cancel",))

    def _rerun(self) -> Reply:
        if self.state == State.CANCELLED:
            self.draft = {}
            self.state = State.AWAITING_SITE
            return Reply(self.state, _HELP[self.state], self._buttons(self.state))
        # DONE: re-resolve from the same draft so the user sees and re-confirms
        # the effective settings — a rerun never submits sight unseen.
        return self._enter_preview()

    def _cancel(self) -> Reply:
        notice = None
        if self.state == State.RUNNING and self.job_id is not None:
            accepted = self._submitter.cancel(self.job_id)
            notice = "Cancellation requested." if accepted else "The job had already finished."
        self.state = State.CANCELLED
        return Reply(self.state, _HELP[State.CANCELLED], self._buttons(self.state), notice=notice)

    def _edit(self, value: str | None) -> Reply:
        try:
            field = Field((value or "").strip())
        except ValueError:
            return Reply(
                self.state,
                _HELP[self.state],
                self._buttons(self.state),
                notice=f"Editable fields: {', '.join(f.value for f in Field)}.",
            )
        self._editing = True
        self.state = FIELD_STATES[field]
        return Reply(self.state, _HELP[self.state], self._buttons(self.state))

    def _reply_done(self, value: str | None) -> Reply:
        return Reply(
            State.DONE,
            f"Job {self.job_id} finished{': ' + value if value else ''}.",
            self._buttons(State.DONE),
        )

    # -- helpers ----------------------------------------------------------------

    def _expired(self) -> bool:
        if self._clock() - self._last_activity <= self._ttl:
            return False
        if self.state == State.RUNNING and self.job_id is not None:
            # A lapsed conversation must not leave a job running unattended.
            self._submitter.cancel(self.job_id)
        self.draft = {}
        self._effective = None
        self._editing = False
        self.job_id = None
        self.state = State.AWAITING_SITE
        return True

    def _buttons(self, state: State) -> tuple[str, ...]:
        buttons: list[str] = []
        if state == State.AWAITING_PROJECT:
            buttons.extend(self._projects or ("new",))
        elif state == State.AWAITING_POLICY:
            buttons.extend(POLICY_PRESETS)
        elif state == State.AWAITING_REPORT:
            buttons.extend(REPORT_FORMATS)
        if Action.BACK in allowed_actions(state):
            buttons.append("back")
        if state == State.PREVIEW:
            buttons.extend(("edit", "confirm"))
        if state == State.CONFIRMING:
            buttons.append("start")
        if Action.CANCEL in allowed_actions(state):
            buttons.append("cancel")
        if state == State.DONE:
            buttons.extend(("rerun", "new scan"))
        if state == State.CANCELLED:
            buttons.append("rerun")
        if Action.HELP in allowed_actions(state):
            buttons.append("help")
        return tuple(buttons)
