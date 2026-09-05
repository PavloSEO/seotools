"""``mirror-check`` sits in ``NEEDS_LIVE_INFRASTRUCTURE`` in
``test_docs_commands_execute.py``: its ``www`` variant needs real DNS-over-HTTPS, so the
parametrized runner there only checks that the documented invocation still *parses* as CLI
flags, not that its ``--input`` JSON actually matches the handler it is dispatched to.

``--input`` is free-form JSON mapped straight onto handler kwargs (``seohead/cli.py``,
``_build_kwargs``), so a JSON object with the wrong keys parses fine and only fails once the
handler itself is called with it -- a ``TypeError`` a live-only command would otherwise only
surface when someone actually ran it against the network (issue #323).

This test builds the kwargs for every documented ``mirror-check`` command the same way the CLI
does and binds them against the real handler signature, entirely offline: no network, no
subprocess.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from scripts.doc_commands import extract_commands, to_argv
from seohead.cli import _build_kwargs, build_parser
from seohead.servers import handlers

ROOT = Path(__file__).resolve().parent.parent


def _mirror_check_commands() -> list[str]:
    return [
        command.raw
        for command in extract_commands(ROOT)
        if to_argv(command.raw)[:1] == ["mirror-check"]
    ]


def test_at_least_one_mirror_check_command_is_documented():
    """Guards the test itself: if every mirror-check example were ever removed from the
    docs, the parametrized check below would vanish silently instead of failing."""
    assert _mirror_check_commands(), "no documented `mirror-check` command found"


@pytest.mark.parametrize("raw", _mirror_check_commands())
def test_documented_mirror_check_input_matches_the_handler_signature(raw):
    argv = to_argv(raw)
    args = build_parser().parse_args(argv)
    handler_name, kwargs = _build_kwargs(argv[0], args)
    assert handler_name == "mirror_check"
    # Raises TypeError on an unknown keyword (e.g. the old, invalid `urls` shape) --
    # exactly the failure #323 reported, caught here without a single network request.
    inspect.signature(handlers.mirror_check).bind(**kwargs)


def test_the_invalid_urls_shape_this_issue_reported_is_still_rejected():
    """Negative control proving the check above would have caught #323: the exact
    invalid ``--input`` this issue was filed about must still fail to bind."""
    argv = to_argv(
        """seohead mirror-check --input '{"urls": ["https://example.com", "https://example.com/page"]}'"""
    )
    args = build_parser().parse_args(argv)
    handler_name, kwargs = _build_kwargs(argv[0], args)
    assert handler_name == "mirror_check"
    with pytest.raises(TypeError, match="urls"):
        inspect.signature(handlers.mirror_check).bind(**kwargs)


def test_the_documented_single_origin_shape_binds_cleanly():
    """Positive control: the corrected single-``url`` shape this issue asks for binds fine."""
    argv = to_argv('seohead mirror-check --input \'{"url": "https://example.com"}\'')
    args = build_parser().parse_args(argv)
    _handler_name, kwargs = _build_kwargs(argv[0], args)
    inspect.signature(handlers.mirror_check).bind(**kwargs)
