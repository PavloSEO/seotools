"""Offline regression for the recovery route documented in docs/RECOVERY.md (issue #320).

Exercises exactly the three cases that route promises to distinguish, through
``seohead.servers.handlers.crawl_site`` -- the same public entry point ``crawl-site``
dispatches to -- with a fake fetcher standing in for the network, no real HTTP:

1. An interrupted crawl, retried with the identical invocation, resumes (``resumed`` is
   ``True``) and does not refetch a URL the checkpoint had already recorded as seen.
2. The interrupted run itself reports ``finish_reason == "interrupted"``.
3. Retrying instead with a changed, results-affecting setting (``max_urls``) is an
   intentional fresh start: ``resumed`` is ``False``, the checkpoint's own state directory
   is reused, and the start URL is refetched from scratch -- the exact tradeoff the doc says
   changing scope/limits between two invocations makes.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import seohead.crawl.spider as spider
from seohead.servers import handlers

PAGES = {
    "https://example.test/": '<a href="/a">a</a><a href="/b">b</a>',
    "https://example.test/a": '<a href="/c">c</a>',
    "https://example.test/b": "done",
    "https://example.test/c": "done",
}


@dataclass
class _Response:
    status_code: int
    text: str
    headers: dict


def _fetcher(hits: list, interrupt_after: str | None = None):
    """A fake fetcher recording every URL it is asked for; raises KeyboardInterrupt the
    first time it is asked for ``interrupt_after``, simulating Ctrl-C mid-crawl."""
    interrupted = False

    def fetch(url: str) -> _Response:
        nonlocal interrupted
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: *\nAllow: /\n", {"content-type": "text/plain"})
        hits.append(url)
        if interrupt_after and url.endswith(interrupt_after) and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return _Response(
            200, "<html><body>" + PAGES[url] + "</body></html>", {"content-type": "text/html"}
        )

    return fetch


def _write_config(path: Path, out_dir: Path, max_urls: int) -> str:
    path.write_text(
        json.dumps(
            {
                "output": {"dir": str(out_dir)},
                "limits": {"max_urls": max_urls},
                "speed": {"min_delay_seconds": 0, "concurrency": 1},
                "robots": {"policy": "ignore"},
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def _run(config_path: str, fake_fetcher):
    """Drive handlers.crawl_site with the fake fetcher standing in for spider.crawl_site's
    real network call, the way the fixture-server tests elsewhere in this suite stand in for
    an actual HTTP client."""
    original = spider.crawl_site
    with patch.object(
        spider, "crawl_site", lambda *a, **kw: original(*a, fetcher=fake_fetcher, **kw)
    ):
        return handlers.crawl_site(url="https://example.test/", config=config_path)


def test_identical_retry_after_interruption_resumes_without_refetching_seen_urls(tmp_path):
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    config_path = _write_config(tmp_path / "crawl.json", out_dir, max_urls=4)

    first_hits: list[str] = []
    first = _run(config_path, _fetcher(first_hits, interrupt_after="/a"))
    assert first["finish_reason"] == "interrupted"
    assert (out_dir / "crawl_state.json").exists(), "no checkpoint written; nothing to resume"

    retry_hits: list[str] = []
    second = _run(config_path, _fetcher(retry_hits))
    assert second["resumed"] is True
    assert "resuming from checkpoint" in second["discovery"]["resume_note"]
    # The positive control: the root ("/") was already recorded as seen before the
    # interruption, so an identical retry must not ask for it again.
    assert "https://example.test/" not in retry_hits


def test_changed_max_urls_is_an_intentional_fresh_start_and_refetches(tmp_path):
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    limited = _write_config(tmp_path / "limited.json", out_dir, max_urls=1)
    _run(limited, _fetcher([]))
    assert (out_dir / "crawl_state.json").exists()

    expanded = _write_config(tmp_path / "expanded.json", out_dir, max_urls=4)
    expanded_hits: list[str] = []
    restarted = _run(expanded, _fetcher(expanded_hits))
    # The negative control: a results-affecting setting changed, so this must NOT read as a
    # resume, and the note must name why -- not silently degrade to "no checkpoint found".
    assert restarted["resumed"] is False
    assert "changed" in restarted["discovery"]["resume_note"]
    # And the refetch consequence the doc warns about: the start URL is fetched again,
    # exactly once, rather than being skipped as already-seen.
    assert Counter(expanded_hits)["https://example.test/"] == 1


def test_run_manifest_carries_the_same_two_fields_the_recovery_route_tells_you_to_check(
    tmp_path,
):
    """docs/RECOVERY.md tells a reader to audit ``run.crawl_resumed`` and
    ``run.crawl_finish_reason`` in audit.json -- this is the field, not just the top-level
    result, that a resumed batch run is actually inspected through."""
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    config_path = _write_config(tmp_path / "crawl.json", out_dir, max_urls=4)

    _run(config_path, _fetcher([], interrupt_after="/a"))
    result = _run(config_path, _fetcher([]))
    assert result["resumed"] is True
    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["run"]["crawl_resumed"] is True
    assert audit["run"]["crawl_finish_reason"] == "finished"
