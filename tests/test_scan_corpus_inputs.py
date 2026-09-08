"""Issue #669: corpus analyzers may read retained scan bodies without fetching."""

from __future__ import annotations

import asyncio
import hashlib
import json

from seohead import cli
from seohead.servers import handlers
from seohead.storage.corpus_inputs import _indexable
from seohead.storage.native_scan import NativeScan
from seohead.tools.markdown_extract import extract_markdown
from tests.test_scan_native import _metadata, _record, _runtime
from tests.test_scan_resource_integration import _event


def _scan(tmp_path, pages, *, retain_bodies=True, finish=True):
    tmp_path.mkdir(exist_ok=True)
    path = tmp_path / "corpus.sqlite"
    metadata = _metadata(**({} if retain_bodies else {"storage.body_mode": "off"}))
    with NativeScan.create(path, **metadata) as scan:
        scan.enqueue([(url, index) for index, (url, _html) in enumerate(pages)])
        for url, html in pages:
            lease = scan.claim(1)[0]
            record = _record(url)
            record["crawl_depth"] = lease.depth
            record["status_code"] = 200
            record["content_type"] = "text/html"
            if "noindex" in html:
                record["meta_robots"] = "noindex"
            scan.commit_page(
                lease,
                record,
                captures=[_event(url, html.encode(), "text/html; charset=utf-8")],
                runtime=_runtime(),
            )
        if finish:
            assert scan.finish_capture("fixture complete")
    return path


def test_scan_corpus_matches_inline_and_never_changes_source(tmp_path, monkeypatch):
    html = "<html><body><nav>menu</nav><main>same retained content words</main><footer>x</footer></body></html>"
    scan = _scan(tmp_path, [("https://example.test/a", html), ("https://example.test/b", html)])
    before = hashlib.sha256(scan.read_bytes()).hexdigest()
    network_attempts = []

    def refuse_network(*args, **kwargs):
        network_attempts.append((args, kwargs))
        raise AssertionError("saved corpus analysis must not access the network")

    monkeypatch.setattr("socket.getaddrinfo", refuse_network)
    monkeypatch.setattr("socket.socket.connect", refuse_network)

    expected = handlers.duplicate_check(
        items=[
            {"id": "https://example.test/a", "text": extract_markdown(html)["content_markdown"]},
            {"id": "https://example.test/b", "text": extract_markdown(html)["content_markdown"]},
        ]
    )
    actual = handlers.duplicate_check(scan=str(scan))
    boilerplate = handlers.boilerplate_report(scan=str(scan))

    assert {
        key: value for key, value in actual.items() if key not in {"source", "coverage"}
    } == expected
    expected_boilerplate = handlers.boilerplate_report(
        pages=[
            {"url": "https://example.test/a", "html": html},
            {"url": "https://example.test/b", "html": html},
        ]
    )
    assert {
        key: value for key, value in boilerplate.items() if key not in {"source", "coverage"}
    } == expected_boilerplate
    assert network_attempts == []
    assert actual["coverage"]["state"] == "complete"
    assert actual["source"]["representations"] == {"static": 2}
    assert boilerplate["count"] == 2
    assert hashlib.sha256(scan.read_bytes()).hexdigest() == before


def test_scan_corpus_keeps_nonindexable_and_missing_bodies_honest(tmp_path):
    html = "<html><body><main>same retained content words</main></body></html>"
    scan = _scan(
        tmp_path,
        [
            ("https://example.test/a", html),
            (
                "https://example.test/noindex",
                html.replace("<main>", "<meta name='robots' content='noindex'><main>"),
            ),
        ],
    )
    result = handlers.duplicate_check(scan=str(scan))
    assert result["excluded_non_indexable"] == 1

    no_bodies = _scan(
        tmp_path / "no-bodies", [("https://example.test/empty", html)], retain_bodies=False
    )
    missing = handlers.duplicate_check(scan=str(no_bodies))
    assert missing["ok"] is False
    assert missing["coverage"]["state"] == "unavailable"
    assert missing["coverage"]["omission_reasons"] == {
        "document unavailable: omitted/not_enabled": 1
    }


def test_scan_flag_is_a_stdin_safe_source_and_mcp_exposes_it(monkeypatch, capsys):
    class NeverRead:
        closed = False

        def isatty(self):
            return False

        def read(self):
            raise AssertionError("--scan must not consume stdin")

    monkeypatch.setattr(cli.sys, "stdin", NeverRead())
    monkeypatch.setitem(handlers.HANDLERS, "duplicate_check", lambda **kw: {"ok": True, "echo": kw})
    assert cli.main(["duplicate-check", "--scan", "saved.sqlite"]) == 0
    assert json.loads(capsys.readouterr().out)["echo"]["scan"] == "saved.sqlite"

    from seohead.servers.mcp_server import build_server

    tools = asyncio.run(build_server().list_tools())
    assert "scan" in {t.name: t for t in tools}["seo_duplicate_check"].inputSchema["properties"]
    assert "scan" in {t.name: t for t in tools}["seo_boilerplate_report"].inputSchema["properties"]


def test_scan_duplicate_work_budget_is_unavailable_not_a_clean_result(tmp_path, monkeypatch):
    html = "<html><body><main>same short repeated words</main></body></html>"
    scan = _scan(tmp_path, [("https://example.test/a", html), ("https://example.test/b", html)])
    monkeypatch.setattr("seohead.storage.corpus_inputs.MAX_DUPLICATE_CANDIDATE_COMPARISONS", 0)

    result = handlers.duplicate_check(scan=str(scan))

    assert result["ok"] is False
    assert result["reason"] == "duplicate candidate-comparison budget exceeded"
    assert "items" not in result
    assert "same short repeated words" not in json.dumps(result)


def test_scan_indexability_uses_the_crawler_policy_for_status_canonical_and_robots():
    page = {
        "url": "https://example.test/page",
        "status_code": 200,
        "canonical": "",
        "meta_robots": "",
        "x_robots": "",
        "error": "",
    }
    assert _indexable(page, False) is True
    assert _indexable({**page, "status_code": 404}, False) is False
    assert _indexable({**page, "canonical": "https://example.test/other"}, False) is False
    assert _indexable({**page, "meta_robots": "noindex,follow"}, False) is False
    assert _indexable(page, True) is False  # report_only robots evidence takes priority


def test_scan_boilerplate_result_keeps_raw_html_private(tmp_path):
    html = "<html><body><nav>private navigation words</nav><main>private page text</main></body></html>"
    result = handlers.boilerplate_report(
        scan=str(_scan(tmp_path, [("https://example.test/a", html)]))
    )

    assert result["coverage"]["analyzed_documents"] == 1
    assert "private navigation words" not in json.dumps(result)


def test_scan_coverage_names_a_running_capture_even_when_its_retained_body_is_complete(tmp_path):
    html = "<html><body><main>captured once</main></body></html>"
    result = handlers.duplicate_check(
        scan=str(_scan(tmp_path, [("https://example.test/a", html)], finish=False))
    )

    assert result["coverage"]["state"] == "partial"
    assert "scan lifecycle is running" in result["coverage"]["reason"]
    assert result["source"]["lifecycle"] == "running"


def test_inline_duplicate_positional_arguments_keep_the_existing_contract():
    """Adding scan input must not reinterpret an inline caller's threshold as a path."""
    from seohead.servers.handlers import duplicate_check

    items = [
        {"id": "https://example.test/a", "text": "shared product description with details"},
        {"id": "https://example.test/b", "text": "shared product description with details"},
    ]
    positional = duplicate_check(items, 0.92, True, False)
    keyword = duplicate_check(
        items=items, threshold=0.92, with_fingerprints=True, only_indexable=False
    )
    assert positional == keyword
    assert positional["count"] == 2
