"""Guided-bot conversation contract and scan configuration wizard.

This package is the bot-facing half of the guided-scan roadmap (epic #751).
It defines a versioned conversation state machine and a session driver that
walks a user from a site address to a confirmed scan job. It deliberately
contains no Telegram SDK code and no crawl logic: adapters bind it to a
delivery surface, and job submission goes through the ``JobSubmitter``
protocol into the shared core.
"""

from seohead.bot.contract import (
    CONTRACT_VERSION,
    Action,
    Field,
    State,
    allowed_actions,
    describe_contract,
)
from seohead.bot.wizard import (
    POLICY_PRESETS,
    Event,
    JobSubmitter,
    Reply,
    ScanJobSpec,
    WizardSession,
)

__all__ = [
    "CONTRACT_VERSION",
    "POLICY_PRESETS",
    "Action",
    "Event",
    "Field",
    "JobSubmitter",
    "Reply",
    "ScanJobSpec",
    "State",
    "WizardSession",
    "allowed_actions",
    "describe_contract",
]
