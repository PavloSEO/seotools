"""Offline contract sketch for the SQLite crawl adapter.

The full test matrix belongs to the D integration owner after C's seed/candidate
API and shared spider helpers merge.  Keep this file network-free.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from seohead.crawl import sqlite_adapter
from seohead.crawl.settings import load
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.storage import ScanError
from seohead.storage.native_scan import NativeScan


def _call(tmp_path, **settings):
    return crawl_to_scan(
        "https://example.test/",
        scan_out=str(tmp_path / "scan.sqlite"),
        settings=load(overrides=settings),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=lambda _url: pytest.fail("guard failures must happen before any fetch"),
        sleeper=lambda _seconds: None,
    )


def test_sqlite_adapter_refuses_cache_before_network(tmp_path):
    with pytest.raises(ValueError, match=r"cache\.mode=off"):
        _call(tmp_path, **{"cache.mode": "live"})


def test_sqlite_adapter_accepts_js_config_and_collects_raw_html_with_an_offline_fetcher(tmp_path):
    calls = []

    def fetcher(url):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n")
        return _Response(200, "<html><head><title>raw</title></head><body>raw</body></html>")

    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(tmp_path / "scan.sqlite"),
        settings=load(overrides={"speed.min_delay_seconds": 0, "rendering.mode": "js"}),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )
    assert calls == ["https://example.test/robots.txt", "https://example.test/"]
    assert run.pages == 1
    assert run.start_page_gate == {
        "html": "<html><head><title>raw</title></head><body>raw</body></html>",
        "outlinks": 0,
        "external_outlinks": 0,
    }


class _Response:
    def __init__(self, status_code, text, headers=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {"content-type": "text/html"}


class _Scan:
    last = None

    def __init__(self, path):
        self.path = path
        self.queue = []
        self.records = []
        self.links = []
        self.forms = []
        self.context = []
        self.preflight_calls = 0
        self.lifecycle = "running"
        self.limitations = []
        self.counts = {"pages": 0, "links": 0, "forms": 0}
        self.runtime = {
            "max_depth_reached": 0,
            "elapsed_seconds": 0.0,
            "circuit_timeout_streak": 0,
            "circuit_server_error_streak": 0,
            "crawl_delay_applied": None,
            "throttle": {"delay_seconds": 0.0, "concurrency": 1, "consecutive_ok": 0},
        }

    @classmethod
    def create(cls, path, **_kwargs):
        cls.last = cls(path)
        cls.last.limitations = _kwargs.get("limitations", [])
        return cls.last

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def write_context(self, items):
        self.context.extend(items)

    def seed_frontier(self, entries):
        for entry in entries:
            if not entry["reason"]:
                self.queue.append(
                    SimpleNamespace(
                        url=entry["frontier_url"],
                        depth=entry["depth"],
                        queue_ordinal=len(self.queue),
                    )
                )

    def begin_collection(self):
        return None

    def preflight_capture(self):
        self.preflight_calls += 1

    def resume_snapshot(self, *, include_edges=False):
        return {
            "scan": {
                "lifecycle": self.lifecycle,
                "crawl_partial": 0,
                "limitations_json": json.dumps(self.limitations),
                "capabilities_json": json.dumps({"resume": {"state": "complete"}}),
            },
            "counts": self.counts,
            "runtime": self.runtime,
        }

    def claim(self, _limit):
        items, self.queue = self.queue, []
        return items

    def commit_page(self, lease, record, *, links, forms, **_kwargs):
        self.records.append(record)
        self.links.extend(links)
        self.forms.extend(forms)
        self.counts["pages"] += 1
        self.counts["links"] += len(links)
        self.counts["forms"] += len(forms)

    def exclude_lease(self, _lease, _reason, **_kwargs):
        return None

    def resume_or_finalize(self):
        self.lifecycle = "finished"
        return True

    def interrupt(self, reason):
        self.lifecycle = "interrupted"


def test_sqlite_adapter_uses_shared_parser_batch_and_injected_robots(monkeypatch, tmp_path):
    fake = _Scan(str(tmp_path / "scan.sqlite"))
    monkeypatch.setattr(sqlite_adapter, "NativeScan", _Scan)
    calls = []

    def fetcher(url):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n")
        return _Response(
            200,
            "<html><head><title>One</title></head><body><a href='/two#part' rel='nofollow'>Two</a><form method='post' action='/send'><input type='password'></form></body></html>",
        )

    result = crawl_to_scan(
        "https://example.test/",
        scan_out=fake.path,
        settings=load(overrides={"speed.min_delay_seconds": 0, "link_attributes.capture": True}),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )
    assert calls == ["https://example.test/robots.txt", "https://example.test/"]
    assert result.pages == 1 and result.links == 1 and result.forms == 1
    assert _Scan.last.links[0]["destination"].endswith("#part")
    assert _Scan.last.links[0]["rel"] == ("nofollow",)
    assert _Scan.last.forms[0]["method"] == "post"
    assert _Scan.last.preflight_calls >= 2


def test_sqlite_adapter_persists_real_scan_page_link_and_form_fields(tmp_path):
    scan_path = tmp_path / "real.sqlite"

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n")
        return _Response(
            200,
            "<html><head><title>One</title></head><body><a href='/two#part' rel='nofollow'>Two</a><form method='post' action='/send'><input type='password'></form></body></html>",
        )

    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=load(overrides={"speed.min_delay_seconds": 0, "link_attributes.capture": True}),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )
    con = sqlite3.connect(scan_path)
    try:
        page = con.execute("SELECT title, crawl_depth, representation FROM pages").fetchone()
        link = con.execute("SELECT rel_json, raw_href FROM links").fetchone()
        form = con.execute("SELECT method, action, has_password FROM forms").fetchone()
    finally:
        con.close()
    assert run.pages == 1 and page == ("One", 0, "static")
    assert link == ('["nofollow"]', "/two#part")
    assert form == ("post", "https://example.test/send", 1)
    assert run.start_page_gate == {
        "html": "<html><head><title>One</title></head><body><a href='/two#part' rel='nofollow'>Two</a><form method='post' action='/send'><input type='password'></form></body></html>",
        "outlinks": 1,
        "external_outlinks": 0,
    }


def test_existing_scan_rejects_changed_effective_config_before_fetch(tmp_path):
    scan_path = tmp_path / "resume.sqlite"

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n")
        return _Response(200, "<html><body>one</body></html>")

    base = load(overrides={"speed.min_delay_seconds": 0})
    crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=base,
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )
    with pytest.raises(ScanError, match="configuration differs"):
        crawl_to_scan(
            "https://example.test/",
            scan_out=str(scan_path),
            settings=load(overrides={"speed.min_delay_seconds": 0, "limits.max_depth": 1}),
            producer_version="3.0.0",
            producer_revision="a" * 40,
            runtime_versions={
                "python": "test",
                "sqlite": "test",
                "httpx": "test",
                "lxml": "test",
                "beautifulsoup4": "test",
            },
            fetcher=lambda _url: pytest.fail("changed config must fail before fetch"),
            sleeper=lambda _seconds: None,
        )


def test_interrupted_scan_rebuilds_start_gate_after_the_start_page_is_fetched(tmp_path):
    scan_path = tmp_path / "interrupted.sqlite"
    settings = load(overrides={"speed.min_delay_seconds": 0})
    with NativeScan.create(
        scan_path,
        start_url="https://example.test/",
        config=settings,
        config_fingerprint=None,
        writer_version="3.0.0",
        writer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        limitations=["raw HTML only: response/document retention is unavailable until G"],
    ) as scan:
        scan.seed_frontier(
            [
                {
                    "requested_url": "https://example.test/",
                    "frontier_url": "https://example.test/",
                    "depth": 0,
                    "reason": "",
                    "source": "start",
                    "reserve_query": False,
                    "seed": False,
                }
            ]
        )
        scan.write_context(
            [
                {
                    "kind": "robots_summary",
                    "item_key": "run",
                    "payload_version": "scan_context.v1",
                    "payload_json": json.dumps(
                        {
                            "policy": "respect",
                            "token": "SEOHEAD-Tools",
                            "fetch_state": "fetched",
                            "final_response_id": None,
                            "note": "",
                            "parsed": {"groups": [], "sitemaps": []},
                        },
                        sort_keys=True,
                    ),
                    "completeness": "complete",
                    "reason": "",
                }
            ]
        )
        scan.interrupt("test interruption")

    calls = []

    def fetcher(url):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n")
        return _Response(200, "<html><body>resumed</body></html>")

    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=settings,
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )
    assert run.pages == 1
    assert run.resumed is True
    assert run.start_page_gate == {
        "html": "<html><body>resumed</body></html>",
        "outlinks": 0,
        "external_outlinks": 0,
    }
    assert calls == ["https://example.test/"]


def test_real_scan_report_only_robots_query_budget_and_fragment_provenance(tmp_path):
    scan_path = tmp_path / "report-only.sqlite"

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nDisallow: /blocked\n")
        if url == "https://example.test/":
            return _Response(
                200,
                "<html><body><a href='/blocked#kept'>Blocked</a><a href='/facet?a=1'>A</a><a href='/facet?b=2'>B</a></body></html>",
            )
        return _Response(200, "<html><body>child</body></html>")

    crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=load(
            overrides={
                "speed.min_delay_seconds": 0,
                "speed.concurrency": 3,
                "robots.policy": "report_only",
                "limits.max_query_variants_per_path": 1,
            }
        ),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )
    con = sqlite3.connect(scan_path)
    try:
        context = con.execute(
            "SELECT payload_json FROM context_items WHERE kind='robots_blocked_url'"
        ).fetchone()
        decision = con.execute(
            "SELECT reason FROM decisions WHERE reason='query_variants_limit'"
        ).fetchone()
        href = con.execute(
            "SELECT u.url FROM links AS l JOIN urls AS u ON u.url_id=l.destination_url_id WHERE u.url LIKE '%#kept'"
        ).fetchone()
    finally:
        con.close()
    assert context is not None and '"policy": "report_only"' in context[0]
    assert decision == ("query_variants_limit",)
    assert href == ("https://example.test/blocked#kept",)


def test_respect_robots_excludes_later_claimed_lease_after_earlier_page_commits(tmp_path):
    """A blocked second lease must not violate C's contiguous commit prefix."""
    scan_path = tmp_path / "respect.sqlite"

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nDisallow: /private\n")
        if url == "https://example.test/":
            return _Response(200, "<html><body>public</body></html>")
        raise AssertionError(f"the blocked URL must not be fetched: {url}")

    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=load(overrides={"speed.min_delay_seconds": 0, "speed.concurrency": 2}),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        seed_urls=["https://example.test/private"],
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )

    with sqlite3.connect(scan_path) as con:
        states = dict(
            con.execute("SELECT u.url, f.state FROM frontier f JOIN urls u USING(url_id)")
        )
        reasons = [row[0] for row in con.execute("SELECT reason FROM decisions")]
    assert run.pages == 1
    assert states["https://example.test/"] == "done"
    assert states["https://example.test/private"] == "excluded"
    assert reasons == ["blocked_by_robots"]


def test_exact_url_limit_with_empty_frontier_is_complete(tmp_path):
    scan_path = tmp_path / "limit.sqlite"

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n")
        return _Response(200, "<html><body>only page</body></html>")

    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=load(overrides={"speed.min_delay_seconds": 0, "limits.max_urls": 1}),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )

    assert run.pages == 1
    assert run.finish_reason == "finished"
    assert run.partial is False


def test_resume_duration_includes_already_committed_elapsed_time(tmp_path):
    scan_path = tmp_path / "duration.sqlite"
    settings = load(overrides={"speed.min_delay_seconds": 0, "limits.max_crawl_seconds": 1})
    with NativeScan.create(
        scan_path,
        start_url="https://example.test/",
        config=settings,
        config_fingerprint=None,
        writer_version="3.0.0",
        writer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        limitations=["raw HTML only: response/document retention is unavailable until G"],
    ) as scan:
        scan.seed_frontier(
            [
                {
                    "requested_url": "https://example.test/",
                    "frontier_url": "https://example.test/",
                    "depth": 0,
                    "reason": "",
                    "source": "start",
                    "reserve_query": False,
                    "seed": False,
                }
            ]
        )
        scan.write_context(
            [
                {
                    "kind": "robots_summary",
                    "item_key": "run",
                    "payload_version": "scan_context.v1",
                    "payload_json": json.dumps(
                        {
                            "policy": "respect",
                            "token": "SEOHEAD-Tools",
                            "fetch_state": "fetched",
                            "final_response_id": None,
                            "note": "",
                            "parsed": {"groups": [], "sitemaps": []},
                        },
                        sort_keys=True,
                    ),
                    "completeness": "complete",
                    "reason": "",
                }
            ]
        )
        scan.con.execute("UPDATE resume_state SET elapsed_seconds=1.0 WHERE singleton=1")
        scan.interrupt("interrupted")

    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=settings,
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        fetcher=lambda _url: (_ for _ in ()).throw(
            AssertionError("duration limit must prevent a fetch")
        ),
        sleeper=lambda _seconds: None,
    )
    assert run.finish_reason == "duration_limit"
    assert run.partial is True
    assert run.resumed is True


def test_server_error_circuit_uses_legacy_five_response_threshold(tmp_path):
    scan_path = tmp_path / "server-errors.sqlite"

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n")
        return _Response(500, "<html><body>failure</body></html>")

    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=load(
            overrides={
                "speed.min_delay_seconds": 0,
                "speed.stop_after_consecutive_timeouts": 99,
            }
        ),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        seed_urls=[
            "https://example.test/one",
            "https://example.test/two",
            "https://example.test/three",
            "https://example.test/four",
        ],
        fetcher=fetcher,
        sleeper=lambda _seconds: None,
    )

    assert run.pages == 5
    assert run.finish_reason == "errors"
    assert run.partial is True


def test_dispatch_gate_spaces_requests_across_multiple_claim_batches(tmp_path):
    scan_path = tmp_path / "paced.sqlite"
    now = [0.0]
    page_dispatches = []

    def clock():
        return now[0]

    def sleeper(delay):
        now[0] += delay

    def fetcher(url):
        if url.endswith("/robots.txt"):
            return _Response(200, "User-agent: SEOHEAD-Tools\nAllow: /\n")
        page_dispatches.append(now[0])
        return _Response(200, "<html><body>page</body></html>")

    crawl_to_scan(
        "https://example.test/",
        scan_out=str(scan_path),
        settings=load(overrides={"speed.min_delay_seconds": 0.1, "speed.concurrency": 2}),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            "python": "test",
            "sqlite": "test",
            "httpx": "test",
            "lxml": "test",
            "beautifulsoup4": "test",
        },
        seed_urls=["https://example.test/one", "https://example.test/two"],
        fetcher=fetcher,
        sleeper=sleeper,
        clock=clock,
    )

    assert len(page_dispatches) == 3
    assert all(
        page_dispatches[index + 1] - page_dispatches[index] >= 0.1
        for index in range(len(page_dispatches) - 1)
    )


@pytest.mark.parametrize(
    ("tag", "count", "reason"),
    [
        ("form", 2001, "form_observations_omitted"),
        ("a", 20001, "link_observations_omitted"),
    ],
)
def test_partial_observation_reason_reaches_the_caller(tmp_path, tag, count, reason):
    from seohead.servers.scan_handlers import _response

    element = (
        "<form method='post' action='/submit'></form>"
        if tag == "form"
        else "<a href='/next' rel='nofollow'>Link</a>"
    )
    html = "<html><body>" + element * count + "</body></html>"
    run = crawl_to_scan(
        "https://example.test/",
        scan_out=str(tmp_path / "scan.sqlite"),
        settings=load(overrides={"speed.min_delay_seconds": 0, "robots.policy": "ignore"}),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions={
            key: "test" for key in ("python", "sqlite", "httpx", "lxml", "beautifulsoup4")
        },
        fetcher=lambda url: _Response(200, html),
        sleeper=lambda seconds: None,
    )
    output = _response(run, audit_available=False, audit_reason="test", finalized=False)
    assert output["partial"] is True
    assert reason in output["limitations"]
    stored = NativeScan.inspect(tmp_path / "scan.sqlite")["scan"]
    assert output["limitations"] == json.loads(stored["limitations_json"])
