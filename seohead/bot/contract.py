"""Versioned conversation contract for the guided scan bot.

This module is data-only: it declares the states, the user/worker actions
that move between them, and the field each collecting state fills. The
driver in ``seohead.bot.wizard`` executes this contract; a Telegram adapter
renders it. Changing a state, an allowed action, or a transition changes the
contract, so ``CONTRACT_VERSION`` must be bumped in the same change — that
is what lets a persisted session or a second adapter refuse a conversation
it does not understand instead of misrouting it.

Flow (see docs/TELEGRAM_BOT.md for the full narrative):

    awaiting_site -> awaiting_project -> awaiting_policy -> awaiting_report
        -> preview -> confirming -> running -> done
                                          -> cancelled

Every collecting state also accepts ``back``, ``edit`` (from ``preview``),
``help`` and ``cancel``. A session whose time-to-live lapses resets to
``awaiting_site`` with an explicit notice — nothing is submitted on the
stale path.
"""

from __future__ import annotations

from enum import Enum

CONTRACT_VERSION = "seohead.bot.conversation/1"


class State(str, Enum):
    """Conversation states in declaration order."""

    AWAITING_SITE = "awaiting_site"
    AWAITING_PROJECT = "awaiting_project"
    AWAITING_POLICY = "awaiting_policy"
    AWAITING_REPORT = "awaiting_report"
    PREVIEW = "preview"
    CONFIRMING = "confirming"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


class Action(str, Enum):
    """Events the contract understands.

    ``ANSWER``, ``BACK``, ``EDIT``, ``HELP``, ``CANCEL`` and ``CONFIRM``
    come from the user. ``PROGRESS`` and ``FINISH`` come from the worker
    side of the adapter; they are actions here only so the whole machine is
    one table.
    """

    ANSWER = "answer"
    BACK = "back"
    EDIT = "edit"
    HELP = "help"
    CANCEL = "cancel"
    CONFIRM = "confirm"
    RERUN = "rerun"
    PROGRESS = "progress"
    FINISH = "finish"


class Field(str, Enum):
    """Draft fields a collecting state fills, editable from ``preview``."""

    SITE = "site"
    PROJECT = "project"
    POLICY = "policy"
    REPORT = "report"


#: Collecting states in forward order; ``back`` walks this list in reverse
#: and ``edit`` jumps from ``preview`` into one of them.
COLLECTING_STATES: tuple[State, ...] = (
    State.AWAITING_SITE,
    State.AWAITING_PROJECT,
    State.AWAITING_POLICY,
    State.AWAITING_REPORT,
)

#: Which collecting state fills which draft field.
FIELD_STATES: dict[Field, State] = {
    Field.SITE: State.AWAITING_SITE,
    Field.PROJECT: State.AWAITING_PROJECT,
    Field.POLICY: State.AWAITING_POLICY,
    Field.REPORT: State.AWAITING_REPORT,
}

#: States where ``cancel`` abandons the draft entirely. RUNNING cancels the
#: submitted job instead and lands on CANCELLED either way.
_CANCELLABLE: frozenset[State] = frozenset(
    {
        State.AWAITING_SITE,
        State.AWAITING_PROJECT,
        State.AWAITING_POLICY,
        State.AWAITING_REPORT,
        State.PREVIEW,
        State.CONFIRMING,
        State.RUNNING,
    }
)

#: Forward transitions for ANSWER/CONFIRM/RERUN/FINISH. EDIT and HELP are
#: context-dependent and BACK walks COLLECTING_STATES, so they are derived
#: in allowed_actions rather than listed here.
_FORWARD: dict[tuple[State, Action], State] = {
    (s, Action.ANSWER): n
    for s, n in zip(COLLECTING_STATES, (*COLLECTING_STATES[1:], State.PREVIEW), strict=True)
} | {
    (State.PREVIEW, Action.CONFIRM): State.CONFIRMING,
    (State.CONFIRMING, Action.CONFIRM): State.RUNNING,
    (State.RUNNING, Action.FINISH): State.DONE,
    (State.DONE, Action.RERUN): State.PREVIEW,
    (State.CANCELLED, Action.RERUN): State.AWAITING_SITE,
}


def allowed_actions(state: State) -> frozenset[Action]:
    """Actions the contract accepts in ``state``.

    Anything outside this set is an invalid step: the driver answers with an
    error and stays, it never advances and never submits.
    """
    actions: set[Action] = set()
    if state in COLLECTING_STATES:
        actions.add(Action.ANSWER)
    if state in COLLECTING_STATES[1:] or state in {State.PREVIEW, State.CONFIRMING}:
        actions.add(Action.BACK)
    if state == State.PREVIEW:
        actions.add(Action.EDIT)
    if state in _CANCELLABLE:
        actions.add(Action.CANCEL)
    if state not in {State.RUNNING}:
        actions.add(Action.HELP)
    for (s, action), _target in _FORWARD.items():
        if s == state:
            actions.add(action)
    if state == State.RUNNING:
        actions.add(Action.PROGRESS)
    return frozenset(actions)


def next_state(state: State, action: Action, *, field: Field | None = None) -> State | None:
    """The state ``action`` lands in, or ``None`` for stay/invalid.

    BACK maps to the previous collecting state (or ``awaiting_report`` from
    ``preview``/``confirming``); EDIT requires ``field`` and maps to that
    field's collecting state; CANCEL maps to ``cancelled``. ANSWER while
    editing returns to ``preview`` — the driver knows that context, so the
    table reports the forward state and the driver overrides it.
    """
    if action not in allowed_actions(state):
        return None
    if action == Action.CANCEL:
        return State.CANCELLED
    if action == Action.BACK:
        if state in {State.PREVIEW, State.CONFIRMING}:
            return State.PREVIEW if state == State.CONFIRMING else State.AWAITING_REPORT
        idx = COLLECTING_STATES.index(state)
        return COLLECTING_STATES[idx - 1]
    if action == Action.EDIT:
        return FIELD_STATES.get(field)
    return _FORWARD.get((state, action))


def describe_contract() -> dict[str, object]:
    """The whole contract as JSON-able data, for docs and adapters."""
    return {
        "version": CONTRACT_VERSION,
        "states": {s.value: sorted(a.value for a in allowed_actions(s)) for s in State},
        "fields": {f.value: s.value for f, s in FIELD_STATES.items()},
        "collecting_order": [s.value for s in COLLECTING_STATES],
    }
