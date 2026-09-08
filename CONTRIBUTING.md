# Contributing

Contributions are welcome when they keep the toolkit headless, evidence-first, and safe to call
from both a terminal and an agent.

All participants are expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before opening a pull request

1. Open an issue for substantial product-scope changes.
2. Add behavior to the core layer first.
3. Register a public tool in all shared interfaces: handler, CLI, and MCP.
4. Add offline tests, including failure and missing-data cases.
5. Update documentation, tool counts, side-effect descriptions, and provider costs.
6. Add a changelog fragment; do not edit `CHANGELOG.md`.
7. Confirm that no credential, private URL, client data, crawl binary, raw log, local path, or
   generated report is included.

Run:

```bash
ruff check .
ruff format --check .
pytest -q
seohead sf run --exports-dir examples/exports --out /tmp/seohead-report --tasks
```

## Changelog fragments

A changelog entry is a file of its own under `changelog.d/`, named for the issue the pull
request closes: `changelog.d/638.md`. Never edit `CHANGELOG.md` in a pull request. Two branches
writing two different files cannot conflict, which is the whole point — every branch used to add
its entry to the top of `## Unreleased` and every branch after the first had to hand-resolve a
conflict whose answer was always "keep both" (#638).

- One pull request closing two issues writes one fragment per issue, each named for its own
  issue. One issue landing as two independent entries writes `<issue>.md` and
  `<issue>-<slug>.md`.
- A change that closes no issue writes `no-issue-<slug>.md`, with a slug that describes the
  change.
- The file holds the entry exactly as it belongs in the changelog: one or more top-level `- `
  bullets with their continuation lines. No heading, no front matter, no title. Multi-paragraph
  prose is expected — the entry explains what was wrong, what the evidence was, and what
  changed.
- English, like every other public file.

At release time the maintainer folds the fragments in and removes them:

```bash
python scripts/build_changelog.py --prune
```

Assembly rewrites only the region between the two marker comments under `## Unreleased`, in
issue order, so running it twice changes nothing and released history is never touched.

## Design rules

- New modules and test files follow [docs/NAMING.md](docs/NAMING.md): a basename says what the
  thing does, not where it lives, and repeats across packages only for an entry point (`cli.py`)
  or an output-format token (`md.py`).
- Core modules do not import the CLI or server layer.
- Missing data is not reported as a clean result.
- Provider production mode and paid operations are explicit.
- New network behavior is bounded, polite, and testable without the network.
- New file mutation is opt-in, atomic where possible, and documented.
- A report renderer formats existing evidence; it does not invent new calculations.
- Public prose, code comments, docstrings, and error messages are English.

By submitting a contribution, you agree that it may be distributed under the repository's MIT
licence and that you have the right to contribute it.
