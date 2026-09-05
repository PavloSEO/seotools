# seohead-seotools documentation

Headless evidence and audit-automation toolkit: it analyzes Screaming Frog exports, adds bounded
live URL and infrastructure checks plus explicit external data sources, and exposes one shared
core through two interfaces — CLI and local MCP. It is not a general-purpose crawler.

## Where to start

| You are… | Read |
|---|---|
| Setting the toolkit up from zero | [SETUP.md](SETUP.md) — versions, deps, first run |
| Looking for a copy-paste command | [USAGE.md](USAGE.md) — runnable examples |
| Importing or inspecting a saved crawl | [STORAGE.md](STORAGE.md) — SQLite import, producer provenance and saved reports; no retained bodies yet |
| New to the toolkit | [GUIDELINE.md](GUIDELINE.md) — what it is, the first run, reading an audit honestly, the usual mistakes |
| A native crawl stopped early | [RECOVERY.md](RECOVERY.md) — the checkpoint, the exact resume requirement, resume vs. intentional fresh start |
| Wondering what this can do end to end | [scenarios/](scenarios/README.md) — 56 chains, each with its commands, its output, its cost and its limits |
| Looking for a tool | [TOOLS.md](TOOLS.md) — reference for all 54 |
| Looking for a tool's exact arguments, types, defaults, or cost | [TOOL_REFERENCE.md](TOOL_REFERENCE.md) — generated from the MCP tool definitions |
| Looking for a check the SF audit runs | [CHECKS.md](CHECKS.md) — all 139, generated from the registry |
| Wondering how this compares to a licensed crawler | [COVERAGE_SF_ISSUES.md](COVERAGE_SF_ISSUES.md) — all 320 published issues, each with a status |
| Looking for a method, not a command | [SKILLS.md](SKILLS.md) — map of the 22 skills |
| Looking for a no-key workflow | [RECIPES.md](RECIPES.md) — exports, traffic decline, bounded live audit |
| About to change code | [ARCHITECTURE.md](ARCHITECTURE.md) — layers and invariants |
| Naming a new module or test file | [NAMING.md](NAMING.md) — what a name must say, and what is deliberately left alone |
| Running or writing tests | [TESTING.md](TESTING.md) — how to run, what they cover |
| Trying to avoid known traps | [GOTCHAS.md](GOTCHAS.md) — money, quotas, footguns |
| Arguing with a past decision | [DECISIONS.md](DECISIONS.md) — why it was done that way |
| Understanding the product and its role beside Screaming Frog | [COMPARISON.md](COMPARISON.md) — canonical positioning, workflow, and boundaries |

## What lives here

### Current

- **[TOOLS.md](TOOLS.md)** — what every tool does, which of them touch the
  network, which have side effects, where the boundaries are. Grouped by layer:
  recon, live tools, bounded site audit, own-crawl, external data sources, SF crawl audit.
- **[TOOL_REFERENCE.md](TOOL_REFERENCE.md)** — every tool's arguments (name,
  type, default), its cost (network/writes/idempotent/spend), and its own
  docstring's behavior and failure-mode notes. Generated from the MCP tool
  definitions in `seohead/servers/mcp_server.py` and `sf_mcp.py`
  (`scripts/generate_tool_reference.py`); `tests/test_docs_drift.py` fails the
  build if it drifts.
- **[CHECKS.md](CHECKS.md)** — the 139 checks the SF crawl audit runs: what each fires
  on, what evidence it needs, and the fix that ships with the finding. Generated
  from `seohead/sf/core/registry.py` (`scripts/generate_checks_reference.py`);
  `tests/test_docs_drift.py` fails the build if it drifts from the registry.
- **[SETUP.md](SETUP.md)** — install from scratch: Python version, dependency
  groups, venv, optional system tools (SF CLI, `whois`), environment variable
  names (names only, never values), first run checks.
- **[USAGE.md](USAGE.md)** — the CLI/MCP/Docker calling conventions with
  copy-paste commands.
- **[ARCHITECTURE.md](ARCHITECTURE.md)** — the package layout, the main
  invariant ("the core does not know who called it"), the data flow diagram,
  the four registration points of a new tool, test requirements.
- **[NAMING.md](NAMING.md)** — what a module or test file name must say, when a
  basename may legitimately repeat across packages, and what naming decisions
  are deliberately left open.
- **[TESTING.md](TESTING.md)** — how to run the suite, what the 1500+ tests
  cover, what they deliberately do not, and which missing tests to write first.
- **[GOTCHAS.md](GOTCHAS.md)** — operational traps captured by tests and code
  contracts: API money, quotas, stdin quirks, and explicit mutation flags.
- **[DECISIONS.md](DECISIONS.md)** — decisions with their price: why no GUI,
  why `load` instead of `networkidle`, why the metrics are called `metrics_lab`,
  why the technology fingerprint database is not shipped.
- **[COMPARISON.md](COMPARISON.md)** — where the set is stronger than the
  market and where it loses, to whom. Wins and holes are both named; the main
  hole is that the bundled `crawl-site` is bounded, not a general-purpose
  web-scale crawler like Screaming Frog.
- **[COVERAGE_GAPS.md](COVERAGE_GAPS.md)** — the map of what the audit still
  lacks, with implemented items marked as done.
- **[CHECKLIST_AUDIT.md](CHECKLIST_AUDIT.md)** — the audit registry checked
  category by category against an external ~320-item technical-SEO
  checklist, with each claim marked verified or unverified and evidence
  quoted from the registry.
- **[SKILLS.md](SKILLS.md)** — the 22 technical workflow skills: when to apply each,
  which tools it drives, which tools deliberately have no skill.
- **[RECIPES.md](RECIPES.md)** — three agent workflows that use existing exports, bounded
  public evidence, or a user-authorized browser without pretending that provider credentials exist.
- **[RECOVERY.md](RECOVERY.md)** — resuming a native `crawl-site` run that stopped early: the
  `crawl_state.json` checkpoint, the identical-invocation requirement, and how to tell a
  successful resume from an intentional fresh start.

### Repository contracts

- [AGENTS.md](../AGENTS.md) defines invariants and editing rules for coding agents.
- [PROVENANCE.md](../PROVENANCE.md) defines the clean public-history boundary.
- [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) records bundled data and
  interoperability references.
- [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md) defines participation standards.
- [CITATION.cff](../CITATION.cff) provides versioned citation metadata.

## Documentation must not lie silently

Nobody recounts the numbers in prose by hand, so `tests/test_docs_drift.py`
recounts them. It fails when:

- a README, skill, or doc states a wrong number of tools, skills, or
  audit checks;
- a skill references a `seohead` command that does not exist;
- a README table row names a non-existent command or a wrong MCP tool name;
- a skill's frontmatter name does not match its folder or has no `description`;
- `docs/TOOLS.md` no longer names every registered CLI command, or its severity
  breakdown disagrees with the check registry;
- `docs/CHECKS.md` disagrees with what `scripts/generate_checks_reference.py`
  would produce from the registry right now, or is missing a check id.
- `docs/TOOL_REFERENCE.md` disagrees with what `scripts/generate_tool_reference.py`
  would produce from the MCP tool definitions right now, or is missing a tool.
- a command shown in a fenced code block anywhere in the docs no longer runs
  against fixtures (`tests/test_docs_commands_execute.py`).

The contract test derives counts and command names directly from registries, so public prose
cannot silently drift away from the interfaces users actually receive.
