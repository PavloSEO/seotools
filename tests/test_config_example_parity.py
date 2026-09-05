"""``config.example.json`` claims to be the template for ``config.json`` (docs/USAGE.md).

Issue #342: a manually maintained copy had drifted from ``DEFAULT_CONFIG``
(``seohead/sf/config.py``) -- missing current export tabs, seven threshold keys, and the
entire ``tasks_pipeline`` block. ``load_config`` deep-merges defaults underneath whatever the
file supplies, so the drift never broke a run; it only meant the advertised template hid
controls a reader had no way to discover from it.

The contract chosen here: ``config.example.json`` is a **complete** template, not a sparse
override example, and ``DEFAULT_CONFIG`` stays its one semantic owner. This test is the
parity/projection check that contract needs -- it fails the moment a new default is added to
the code without a matching line in the example, instead of letting the file quietly fall
behind again.
"""

from __future__ import annotations

import json
from pathlib import Path

from seohead.sf.config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parent.parent


def _load_example() -> dict:
    with open(ROOT / "config.example.json", encoding="utf-8") as handle:
        return json.load(handle)


def test_config_example_is_a_complete_projection_of_default_config():
    """Every path and value in config.example.json must equal DEFAULT_CONFIG exactly:
    the example is generated from the defaults, not maintained by hand alongside them."""
    example = _load_example()
    assert example == DEFAULT_CONFIG, (
        "config.example.json has drifted from DEFAULT_CONFIG; regenerate it with "
        "`json.dump(DEFAULT_CONFIG, ..., indent=2)` (seohead/sf/config.py) rather than "
        "hand-editing it out of sync"
    )


def test_config_example_parses_and_deep_merges_to_the_same_defaults():
    """Positive control distinct from the exact-equality check above: loading the shipped
    example file through the real config loader must reproduce DEFAULT_CONFIG unchanged --
    proving a full template is also a safe, no-op starting point to copy and edit."""
    from seohead.sf.config import deep_merge

    example = _load_example()
    assert deep_merge(DEFAULT_CONFIG, example) == DEFAULT_CONFIG


def test_a_sparse_override_file_still_only_changes_what_it_names():
    """Negative control: a config file that is NOT the complete template (a sparse
    override, the contract this file deliberately did not choose) must still only touch the
    keys it names -- guarding against a future regeneration script that accidentally starts
    stripping unrelated defaults instead of just projecting them."""
    from seohead.sf.config import deep_merge

    sparse = {"thresholds": {"thin_content_words": 400}}
    merged = deep_merge(DEFAULT_CONFIG, sparse)
    assert merged["thresholds"]["thin_content_words"] == 400
    untouched = {k: v for k, v in merged["thresholds"].items() if k != "thin_content_words"}
    expected = {k: v for k, v in DEFAULT_CONFIG["thresholds"].items() if k != "thin_content_words"}
    assert untouched == expected
    assert merged["exports"] == DEFAULT_CONFIG["exports"]
