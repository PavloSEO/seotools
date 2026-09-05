"""Every accumulator a crawl builds up must survive its own checkpoint.

Three separate defects in this repository were the same omission: a field was
added to ``SpiderResult``, the checkpoint was not extended to carry it, and a
resumed crawl silently reported less than an uninterrupted one. #141 lost the
link graph, the exclusion tally and the query-variant budget; #188 lost form
findings and the start page's rendering evidence. Each was found on a live run,
long after the field was added.

The list below is the fix for the pattern rather than for the two fields: every
field on ``SpiderResult`` must be classified here, so adding a new one to the
dataclass without deciding how it survives a resume fails immediately, by name.
"""

from __future__ import annotations

import dataclasses

from seohead.crawl.spider import SpiderResult

# Carried inside crawl_state.json.
CHECKPOINTED = {
    "excluded",
    "max_depth_reached",
    "forms",
    "start_page_evidence",
    # Crawl-wide report-only robots evidence, not per-invocation data (#349):
    # a resumed run must retain every block an uninterrupted run would have.
    "robots_blocked",
}

# Written to their own sidecar file as they are produced and read back on
# resume, because they are the two structures large enough that reserialising
# them on every checkpoint would dominate the cost of taking one.
SIDECAR = {"pages", "links"}

# Recomputed from scratch on every invocation, so carrying them would be wrong,
# not merely unnecessary: each describes this call, not the crawl as a whole.
PER_INVOCATION = {
    # A constant describing the output format, not crawl state.
    "schema_version",
    "resume_note",
    "resumed",
    "partial",
    "stopped_reason",
    "finish_reason",
    "effective_delay",
    "effective_concurrency",
    "crawl_delay_applied",
    "robots_note",
    "seed_urls",
    "limitations",
    "cache_stats",
    "cache_replay",
}


def test_every_spider_result_field_is_classified_for_resume():
    """A new field on SpiderResult must be placed in one of the three groups above.

    Failing here is the point: the alternative is discovering months later, on a
    real crawl, that a resumed run quietly reports less than an uninterrupted one.
    """
    known = CHECKPOINTED | SIDECAR | PER_INVOCATION
    actual = {f.name for f in dataclasses.fields(SpiderResult)}
    unclassified = actual - known
    assert not unclassified, (
        f"new SpiderResult field(s) {sorted(unclassified)}: decide whether each survives a "
        "resume (add to CHECKPOINTED and to crawl_state.CrawlState), lives in a sidecar, or "
        "is genuinely per-invocation — see issue #188"
    )
    stale = known - actual
    assert not stale, f"classified field(s) no longer on SpiderResult: {sorted(stale)}"


def test_the_checkpoint_carries_every_field_it_claims_to():
    """CHECKPOINTED above and CrawlState must not drift apart: a field listed here but
    absent from the state dataclass would read as handled while being dropped."""
    from seohead.crawl.state import CrawlState

    state_fields = {f.name for f in dataclasses.fields(CrawlState)}
    missing = CHECKPOINTED - state_fields
    assert not missing, f"claimed checkpointed but absent from CrawlState: {sorted(missing)}"


# SIDECAR above says *what* is reloadable on resume; it says nothing about whether the
# one production caller actually wires the corresponding path unconditionally. #242 was
# exactly that gap: handlers.crawl_site tied the "pages" sidecar's path to
# output.write_pages_jsonl on top of out_dir, so a resumed run silently lost pages while
# this file's classification of "pages" as SIDECAR stayed correct and green throughout --
# the third resume defect (after #141, #188) to slip past a guard watching the field
# rather than the caller wiring it to spider.crawl_site. Mapping each SIDECAR field to
# the keyword argument spider.crawl_site accepts for it means a future SIDECAR field is
# caught here too, not just today's two -- see test_sidecar_paths_reach_the_spider_below.
_SIDECAR_KWARGS = {"pages": "out_path", "links": "links_path"}


def test_sidecar_paths_reach_the_spider_whenever_out_dir_is_set(tmp_path, monkeypatch):
    """handlers.crawl_site must hand spider.crawl_site a real path for every SIDECAR
    field whenever out_dir is configured, independent of any other setting --
    output.write_pages_jsonl in particular, since disabling the human-readable pages.jsonl
    export must not also disable the resume mechanism that field depends on.

    This is deliberately caller-level rather than field-level: it does not know why a
    setting might one day gate a SIDECAR path, only that out_dir being set is the sole
    condition SIDECAR promises to honour. If a genuinely new reason to omit a sidecar path
    ever appears, that is a real design question worth a deliberate exception here, not a
    reason to skip this test.
    """
    import contextlib
    import json

    import seohead.crawl.spider as spider_mod
    from seohead.servers import handlers

    assert set(_SIDECAR_KWARGS) == SIDECAR, (
        "a SIDECAR field has no entry in _SIDECAR_KWARGS above -- name the keyword "
        "argument spider.crawl_site accepts for it before this test can cover it"
    )

    class _StopAtSpider(Exception):
        pass

    captured: dict[str, object] = {}

    def fake_crawl_site(*_args, **kwargs):
        captured.update(kwargs)
        raise _StopAtSpider

    monkeypatch.setattr(spider_mod, "crawl_site", fake_crawl_site)

    # write_pages_jsonl is the one existing toggle shaped like #242's cause: something
    # that turns a human-readable export off and, if wired into the same path, would
    # take the sidecar down with it. Both settings must still reach the spider.
    for write_pages_jsonl in (True, False):
        out_dir = tmp_path / f"run-{write_pages_jsonl}"
        config_path = tmp_path / f"crawl-{write_pages_jsonl}.json"
        config_path.write_text(json.dumps({"output": {"write_pages_jsonl": write_pages_jsonl}}))
        captured.clear()
        with contextlib.suppress(_StopAtSpider):
            handlers.crawl_site(
                url="https://example.com/", out_dir=str(out_dir), config=str(config_path)
            )
        for field_name, kwarg_name in _SIDECAR_KWARGS.items():
            assert captured.get(kwarg_name), (
                f"with out_dir set and write_pages_jsonl={write_pages_jsonl}, "
                f"spider.crawl_site received {kwarg_name}={captured.get(kwarg_name)!r} for "
                f"SIDECAR field {field_name!r} -- a resumed run would silently lose it "
                "(issue #242)"
            )
