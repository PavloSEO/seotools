"""Resuming a sitemap-seeded crawl must not lose an accepted seed's provenance.

Regression for the #348 comment that extended its acceptance criteria: the
spider restored the checkpointed frontier and ``seen`` set on resume, but not
the accepted-seed identities behind them. An accepted sitemap declaration is
added to ``seen`` the same way a rejected one is, so on resume the seed loop
found it already in ``seen`` and skipped appending it to ``result.seed_urls``
-- a completed resumed audit then reported ``discovery.sitemap_seeded: 0``
where an uninterrupted crawl over the identical site and sitemap reported the
true count.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import seohead.crawl.spider as spider
from seohead.crawl import state as crawl_state
from seohead.servers import handlers


class Response:
    def __init__(self, text: str, content_type: str = "text/html") -> None:
        self.status_code = 200
        self.text = text
        self.headers = {"content-type": content_type}


def make_fetcher(*, interrupt_orphan_once: bool):
    interrupted = False

    def fetch(url: str) -> Response:
        nonlocal interrupted
        if url.endswith("/robots.txt"):
            return Response("User-agent: *\nAllow: /\n", "text/plain")
        if url == "https://example.test/":
            return Response("home")
        if url == "https://example.test/orphan" and interrupt_orphan_once and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        if url == "https://example.test/orphan":
            return Response("orphan")
        raise AssertionError(url)

    return fetch


SITEMAP = {
    "sitemap_url": "https://example.test/sitemap.xml",
    "sitemap_urls": ["https://example.test/sitemap.xml"],
    "declared": ["https://example.test/orphan"],
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
    path.write_text(json.dumps({"limits": {"max_urls": 10}}), encoding="utf-8")


def test_resume_keeps_an_accepted_sitemap_seed_counted(tmp_path):
    config = tmp_path / "crawl.json"
    _write_config(config)

    first = run(tmp_path / "resumed", config, make_fetcher(interrupt_orphan_once=True))
    assert first["finish_reason"] == "interrupted"

    resumed = run(tmp_path / "resumed", config, make_fetcher(interrupt_orphan_once=False))
    assert resumed["resumed"] is True
    assert resumed["partial"] is False

    # Negative control: an uninterrupted crawl over the identical site/sitemap is
    # the ground truth -- one accepted seed.
    uninterrupted = run(
        tmp_path / "uninterrupted", config, make_fetcher(interrupt_orphan_once=False)
    )
    assert uninterrupted["discovery"]["sitemap_seeded"] == 1
    assert uninterrupted["urls_collected"] == 2

    # Positive control: the resumed run must match it, not report zero.
    assert resumed["discovery"]["sitemap_seeded"] == 1
    assert resumed["urls_collected"] == 2


def test_a_pre_change_checkpoint_declines_resume_rather_than_inventing_zero(tmp_path):
    """A checkpoint saved under the older schema (no accepted_seed_urls field)
    must be refused outright, not read with the field defaulted to empty and
    reported as a trustworthy resumed audit."""
    config = tmp_path / "crawl.json"
    _write_config(config)

    out_dir = tmp_path / "run"
    run(out_dir, config, make_fetcher(interrupt_orphan_once=True))

    state_path = out_dir / "crawl_state.json"
    assert state_path.exists()
    loaded, _note = crawl_state.load(str(state_path), "https://example.test/")
    assert loaded is not None
    assert loaded.accepted_seed_urls == ["https://example.test/orphan"]

    # Simulate a checkpoint saved by a build that predates the field.
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    raw.pop("accepted_seed_urls", None)
    raw["schema_version"] = "crawl_state.v3"
    state_path.write_text(json.dumps(raw), encoding="utf-8")

    loaded, note = crawl_state.load(str(state_path), "https://example.test/")
    assert loaded is None
    assert "schema" in note
