"""A resumed report-only crawl must keep its full robots-blocked inventory.

Regression for issue #349: ``CrawlState`` had no field for
``SpiderResult.robots_blocked``, so a resumed report-only crawl restored
everything else but started that list empty. A completed resumed audit then
silently dropped the BLOCKED_BY_ROBOTS / IMPORTANT_URL_BLOCKED_BY_ROBOTS
findings an uninterrupted crawl over the same site would have produced.
"""

from __future__ import annotations

from unittest.mock import patch

import seohead.crawl.spider as spider
from seohead.servers import handlers


class Response:
    def __init__(self, text: str, content_type: str = "text/html") -> None:
        self.status_code = 200
        self.text = text
        self.headers = {"content-type": content_type}


def make_fetcher(*, interrupt_later_once: bool):
    interrupted = False

    def fetch(url: str) -> Response:
        nonlocal interrupted
        if url.endswith("/robots.txt"):
            return Response("User-agent: *\nDisallow: /blocked\n", "text/plain")
        if url == "https://example.test/":
            return Response('<a href="/blocked">blocked</a><a href="/later">later</a>')
        if url == "https://example.test/blocked":
            return Response("blocked")
        if url == "https://example.test/later" and interrupt_later_once and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        if url == "https://example.test/later":
            return Response("later")
        raise AssertionError(url)

    return fetch


def run(out_dir, fetcher):
    original = spider.crawl_site

    def injected(*args, **kwargs):
        return original(*args, fetcher=fetcher, sleeper=lambda _seconds: None, **kwargs)

    with patch.object(spider, "crawl_site", injected):
        return handlers.crawl_site(
            url="https://example.test/",
            robots="report_only",
            min_delay=0,
            out_dir=str(out_dir),
        )


def observed(report):
    checks = report["summary"]["by_check"]
    return {
        "resumed": report["resumed"],
        "partial": report["partial"],
        "finish_reason": report["finish_reason"],
        "robots_blocked": report["discovery"]["robots_blocked"],
        "blocked_by_robots": checks.get("BLOCKED_BY_ROBOTS", 0),
        "important_blocked": checks.get("IMPORTANT_URL_BLOCKED_BY_ROBOTS", 0),
        "urls_collected": report["urls_collected"],
    }


def test_resumed_report_only_crawl_keeps_robots_blocked_inventory(tmp_path):
    first = run(tmp_path / "resumed", make_fetcher(interrupt_later_once=True))
    assert first["finish_reason"] == "interrupted"

    resumed = run(tmp_path / "resumed", make_fetcher(interrupt_later_once=False))
    assert resumed["resumed"] is True
    assert resumed["partial"] is False

    # Positive control: the resumed run must retain the block and both findings.
    resumed_obs = observed(resumed)
    assert resumed_obs["robots_blocked"] == 1
    assert resumed_obs["blocked_by_robots"] == 1
    assert resumed_obs["important_blocked"] == 1

    # Negative control: an uninterrupted crawl over the identical site is the
    # ground truth the resumed run must match, not merely "greater than zero".
    uninterrupted = run(tmp_path / "uninterrupted", make_fetcher(interrupt_later_once=False))
    uninterrupted_obs = observed(uninterrupted)
    assert resumed_obs["robots_blocked"] == uninterrupted_obs["robots_blocked"]
    assert resumed_obs["blocked_by_robots"] == uninterrupted_obs["blocked_by_robots"]
    assert resumed_obs["important_blocked"] == uninterrupted_obs["important_blocked"]
    assert resumed_obs["urls_collected"] == uninterrupted_obs["urls_collected"]


def test_a_schema_mismatched_checkpoint_starts_fresh_not_empty_complete(tmp_path):
    """A checkpoint written under an older schema (no robots_blocked field) must be
    rejected outright rather than read with an empty inventory and reported as a
    complete, trustworthy resumed audit."""
    from seohead.crawl import state as crawl_state

    out_dir = tmp_path / "run"
    run(out_dir, make_fetcher(interrupt_later_once=True))

    state_path = out_dir / "crawl_state.json"
    assert state_path.exists()
    loaded, _note = crawl_state.load(str(state_path), "https://example.test/")
    assert loaded is not None
    assert loaded.robots_blocked == ["https://example.test/blocked"]

    # Simulate a checkpoint saved by an older build that predates the field.
    import json

    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw.pop("robots_blocked", None)
    raw["schema_version"] = "crawl_state.v2"
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    loaded, note = crawl_state.load(str(state_path), "https://example.test/")
    assert loaded is None
    assert "schema" in note
