"""Resuming a sitemap-seeded crawl must not re-count an unchanged rejected seed.

Regression for issue #348: the spider evaluated every supplied sitemap
declaration against ``rules.rejection()`` before checking ``seen``. A
rejected seed was never added to ``seen``, so an identical resume re-ran the
rejection and incremented the restored exclusion tally a second time, and (with
decision logging enabled) wrote a duplicate decision record for the same URL.
"""

from __future__ import annotations

import json
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
            return Response("User-agent: *\nAllow: /\n", "text/plain")
        if url == "https://example.test/":
            return Response('<a href="/later">later</a>')
        if url == "https://example.test/later" and interrupt_later_once and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        if url == "https://example.test/later":
            return Response("later")
        raise AssertionError(url)

    return fetch


SITEMAP = {
    "sitemap_url": "https://example.test/sitemap.xml",
    "sitemap_urls": ["https://example.test/sitemap.xml"],
    "declared": ["https://example.test/from-sitemap"],
}


def run(out_dir, config, fetcher):
    original = spider.crawl_site

    def injected(*args, **kwargs):
        return original(*args, fetcher=fetcher, sleeper=lambda _seconds: None, **kwargs)

    with (
        patch.object(spider, "crawl_site", injected),
        patch.object(handlers, "_seed_urls_from_sitemap", return_value=SITEMAP),
        patch("seohead.sf.core.sitemap_coverage.run_sitemap", return_value={}),
    ):
        return handlers.crawl_site(
            url="https://example.test/",
            sitemap=SITEMAP["sitemap_url"],
            config=str(config),
            robots="ignore",
            min_delay=0,
            out_dir=str(out_dir),
        )


def _write_config(path):
    path.write_text(
        json.dumps(
            {
                "scope": {"exclude_patterns": [r"/from-sitemap$"]},
                "output": {"write_decisions_jsonl": True},
            }
        ),
        encoding="utf-8",
    )


def test_resume_does_not_recount_an_unchanged_rejected_sitemap_seed(tmp_path):
    config = tmp_path / "crawl.json"
    _write_config(config)

    first = run(tmp_path / "resumed", config, make_fetcher(interrupt_later_once=True))
    assert first["finish_reason"] == "interrupted"

    resumed = run(tmp_path / "resumed", config, make_fetcher(interrupt_later_once=False))
    assert resumed["resumed"] is True
    assert resumed["partial"] is False

    # Negative control: an uninterrupted crawl over the identical site/config is
    # the ground truth -- one rejected seed, one exclusion.
    uninterrupted = run(
        tmp_path / "uninterrupted", config, make_fetcher(interrupt_later_once=False)
    )
    assert uninterrupted["discovery"]["excluded"] == {"excluded_by_pattern": 1}

    # Positive control: the resumed run must match it, not double-count.
    assert resumed["discovery"]["excluded"] == {"excluded_by_pattern": 1}

    decisions = [
        json.loads(line)
        for line in (tmp_path / "resumed" / "decisions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line
    ]
    repeated = [d for d in decisions if d["url"].endswith("/from-sitemap")]
    assert len(repeated) == 1


def test_a_newly_discovered_url_after_checkpoint_still_gets_excluded(tmp_path):
    """A non-seed URL discovered only after the checkpoint must still contribute
    its own exclusion decision -- the seed dedup must not swallow genuinely new
    URLs, only re-declared seeds already recorded as seen."""
    config = tmp_path / "crawl.json"
    config.write_text(
        json.dumps(
            {
                "scope": {"exclude_patterns": [r"/later$"]},
                "output": {"write_decisions_jsonl": True},
            }
        ),
        encoding="utf-8",
    )

    def fetch(url: str) -> Response:
        if url.endswith("/robots.txt"):
            return Response("User-agent: *\nAllow: /\n", "text/plain")
        if url == "https://example.test/":
            return Response('<a href="/later">later</a>')
        raise AssertionError(url)

    out_dir = tmp_path / "run"
    original = spider.crawl_site

    def injected(*args, **kwargs):
        return original(*args, fetcher=fetch, sleeper=lambda _seconds: None, **kwargs)

    with patch.object(spider, "crawl_site", injected):
        result = handlers.crawl_site(
            url="https://example.test/",
            config=str(config),
            robots="ignore",
            min_delay=0,
            out_dir=str(out_dir),
        )

    assert result["discovery"]["excluded"] == {"excluded_by_pattern": 1}
