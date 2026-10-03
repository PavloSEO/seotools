"""Contract-level invariants for the guided-bot state machine."""

from __future__ import annotations

import json

from seohead.bot.contract import (
    COLLECTING_STATES,
    CONTRACT_VERSION,
    FIELD_STATES,
    Action,
    Field,
    State,
    allowed_actions,
    describe_contract,
    next_state,
)


def test_contract_version_is_namespaced_and_stable():
    assert CONTRACT_VERSION == "seohead.bot.conversation/1"


def test_describe_contract_is_json_serializable_and_complete():
    doc = describe_contract()
    assert json.loads(json.dumps(doc))["version"] == CONTRACT_VERSION
    assert set(doc["states"]) == {s.value for s in State}
    assert set(doc["fields"]) == {f.value for f in Field}


def test_next_state_only_returns_for_allowed_actions():
    for state in State:
        for action in Action:
            target = next_state(state, action)
            if action not in allowed_actions(state):
                assert target is None, (state, action)
            elif action != Action.EDIT:
                assert target is not None or action in {
                    Action.ANSWER,
                    Action.HELP,
                    Action.PROGRESS,
                }, (state, action)


def test_forward_chain_reaches_preview():
    for current, expected in zip(
        COLLECTING_STATES, (*COLLECTING_STATES[1:], State.PREVIEW), strict=True
    ):
        assert next_state(current, Action.ANSWER) == expected


def test_back_walks_collecting_states_in_reverse():
    assert next_state(State.AWAITING_REPORT, Action.BACK) == State.AWAITING_POLICY
    assert next_state(State.AWAITING_PROJECT, Action.BACK) == State.AWAITING_SITE
    assert next_state(State.AWAITING_SITE, Action.BACK) is None
    assert next_state(State.PREVIEW, Action.BACK) == State.AWAITING_REPORT
    assert next_state(State.CONFIRMING, Action.BACK) == State.PREVIEW


def test_edit_requires_a_field_and_is_preview_only():
    assert next_state(State.PREVIEW, Action.EDIT) is None
    for field, state in FIELD_STATES.items():
        assert next_state(State.PREVIEW, Action.EDIT, field=field) == state
    for state in State:
        if state != State.PREVIEW:
            assert next_state(state, Action.EDIT, field=Field.POLICY) is None


def test_cancel_is_never_offered_once_terminal():
    for state in (State.DONE, State.CANCELLED):
        assert Action.CANCEL not in allowed_actions(state)


def test_worker_events_bound_to_running():
    for state in State:
        for action in (Action.PROGRESS, Action.FINISH):
            assert (action in allowed_actions(state)) == (state == State.RUNNING)


def test_rerun_offered_only_from_terminal_states():
    for state in State:
        assert (Action.RERUN in allowed_actions(state)) == (state in {State.DONE, State.CANCELLED})
