"""#676: ordinary URL crawls default to one collision-safe native scan."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from seohead import runlog
from seohead.servers import handlers
from seohead.storage import open_scan

BUILD = "a" * 40


@pytest.fixture
def site(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<html><head><title>Fixture</title></head><body>fixture page</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_HOSTS", "127.0.0.1")
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_url_without_storage_flags_routes_to_a_generated_native_scan(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "seohead.servers.scan_handlers.crawl_site_scan",
        lambda url, **kwargs: captured.update(url=url, **kwargs) or {"scan": kwargs["scan_out"]},
    )
    monkeypatch.setattr(
        "seohead.crawl.spider.crawl_site",
        lambda *_args, **_kwargs: pytest.fail("default URL mode must not use the legacy crawler"),
    )

    result = handlers.crawl_site(url="https://example.test/", producer_build=BUILD)

    path = Path(result["scan"])
    assert captured["url"] == "https://example.test/"
    assert captured["scan_out"] == str(path)
    assert path.parent == tmp_path / "scans"
    assert path.suffix == ".sqlite"
    assert not path.exists()  # The replaced native writer owns publication.


def test_default_scan_validates_provenance_before_creating_a_destination(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "seohead.crawl.spider.crawl_site",
        lambda *_args, **_kwargs: pytest.fail("invalid provenance must stop before crawling"),
    )

    with pytest.raises(ValueError, match="producer_build"):
        handlers.crawl_site(url="https://example.test/", producer_build="not-a-sha")

    assert not (tmp_path / "scans").exists()


def test_default_url_capture_publishes_a_valid_native_scan(site, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = handlers.crawl_site(
        url=site,
        producer_build=BUILD,
        overrides={
            "limits.max_urls": 1,
            "speed.min_delay_seconds": 0,
            "robots.policy": "ignore",
        },
    )

    path = Path(result["scan"])
    assert path.parent == tmp_path / "scans"
    assert path.is_file()
    with open_scan(path, require_audit=False) as con:
        assert con.execute("SELECT format_version FROM scan").fetchone()[0] == "scan.v1"
        assert con.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 1


def test_explicit_legacy_directory_retains_its_existing_outputs(site, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / "legacy"

    result = handlers.crawl_site(
        url=site,
        out_dir=str(legacy),
        overrides={
            "limits.max_urls": 1,
            "speed.min_delay_seconds": 0,
            "robots.policy": "ignore",
        },
    )

    assert result["out_dir"] == str(legacy)
    assert (legacy / "pages.jsonl").is_file()
    assert (legacy / "audit.json").is_file()
    assert not (tmp_path / "scans").exists()


def test_run_journal_records_a_generated_scan_path(tmp_path, monkeypatch):
    journal = tmp_path / "runs.jsonl"
    monkeypatch.setenv("SEOHEAD_RUN_LOG", str(journal))
    wrapped = runlog.journaled("crawl_site", lambda: {"scan": "scans/example.sqlite"})

    assert wrapped() == {"scan": "scans/example.sqlite"}
    assert runlog.read_entries(limit=1)[0]["scan"] == "scans/example.sqlite"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"urls": ["https://example.test/a"]}, "list mode"),
        ({"overrides": {"cache.mode": "live"}}, "cache.mode"),
        ({"overrides": {"cache.mode": "replay"}}, "cache.mode"),
    ],
)
def test_default_scan_refuses_unsupported_modes_before_http(tmp_path, monkeypatch, kwargs, message):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "seohead.crawl.spider.crawl_site",
        lambda *_args, **_kwargs: pytest.fail("unsupported default scan mode must not fetch"),
    )
    monkeypatch.setattr(
        "seohead.crawl.collect.collect_urls",
        lambda *_args, **_kwargs: pytest.fail("unsupported default scan mode must not fetch"),
    )

    call = {"producer_build": BUILD, "url": "https://example.test/", **kwargs}
    if "urls" in kwargs:
        call["url"] = None
    with pytest.raises(ValueError, match=message):
        handlers.crawl_site(**call)

    assert not (tmp_path / "scans").exists()
