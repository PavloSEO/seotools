"""Generated command-input catalogue must stay tied to public interfaces."""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path

from seohead import cli
from seohead.input_contracts import COMMAND_CONTRACTS, SF_CONTRACTS, coverage_gaps, render_markdown
from seohead.servers import handlers
from seohead.sf import cli as sf_cli

ROOT = Path(__file__).resolve().parent.parent


def _destinations(parser: argparse.ArgumentParser) -> set[str]:
    return {action.dest for action in parser._actions}


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("parser has no subcommands")


def test_every_direct_command_is_catalogued_once_with_its_handler():
    names = [contract.command for contract in COMMAND_CONTRACTS]

    assert set(names) == set(cli.COMMANDS)
    assert len(names) == len(set(names)) == len(cli.COMMANDS)
    assert {contract.handler for contract in COMMAND_CONTRACTS} == set(handlers._RAW_HANDLERS)


def test_sf_family_is_catalogued_once_per_subcommand():
    sf_subcommands = set(_subparsers(sf_cli.build_parser()))
    missing, stale = coverage_gaps(cli.COMMANDS, sf_subcommands)

    assert not missing
    assert not stale
    assert {contract.command for contract in SF_CONTRACTS} >= {
        f"sf {name}" for name in sf_subcommands
    }


def test_coverage_gate_rejects_synthetic_new_direct_and_sf_commands():
    missing_direct, _stale_direct = coverage_gaps((*cli.COMMANDS, "synthetic-direct"), ())
    missing_sf, _stale_sf = coverage_gaps((), ("synthetic-sf",))

    assert "synthetic-direct" in missing_direct
    assert "sf synthetic-sf" in missing_sf


def test_declared_arguments_exist_in_the_handler_or_cli_parser():
    parser = cli.build_parser()
    cli_subcommands = _subparsers(parser)
    sf_subcommands = _subparsers(sf_cli.build_parser())

    for contract in COMMAND_CONTRACTS:
        handler = handlers._RAW_HANDLERS[contract.handler or ""]
        accepted = set(inspect.signature(handler).parameters)
        cli_args = _destinations(cli_subcommands[contract.command])
        for form in contract.forms:
            unknown = set(form.arguments) - accepted - cli_args
            assert not unknown, f"{contract.command} names unknown input arguments: {unknown}"

    for contract in SF_CONTRACTS:
        if not contract.command.startswith("sf "):
            continue
        cli_args = _destinations(sf_subcommands[contract.command.removeprefix("sf ")])
        for form in contract.forms:
            unknown = set(form.arguments) - cli_args
            assert not unknown, f"{contract.command} names unknown input arguments: {unknown}"


def test_generated_reference_matches_metadata_and_does_not_claim_scan_adapters():
    rendered = render_markdown()

    assert (ROOT / "docs" / "INPUTS.md").read_text(encoding="utf-8") == rendered
    assert (
        "`duplicate-check` and\n`boilerplate-report` currently accept inline corpora only"
        in rendered
    )
    by_command = {contract.command: contract for contract in COMMAND_CONTRACTS}
    for command in ("duplicate-check", "boilerplate-report"):
        assert {form.kind for form in by_command[command].forms} == {"inline_corpus"}
    assert [form.kind for form in by_command["scan-body-diff"].forms] == [
        "scan_artifact",
        "selector",
    ]
    assert "required together" in rendered
    assert "`crawl-site --resume`\nis the explicit exception" in rendered
