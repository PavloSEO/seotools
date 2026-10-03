"""Targeted fix verification uses measured page evidence, never missing rows."""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from seohead.crawl import settings
from seohead.servers import handlers
from seohead.verification import classify, select

A = "https://example.test/a"
B = "https://example.test/b"
C = "https://example.test/c"


def _audit(
    urls=(A, B, C),
    issues=(),
    *,
    representation="static",
    scan_uuid=None,
    generated_at="2026-10-01T00:00:00Z",
    **run,
):
    return {
        "schema_version": "2.0",
        "run": {
            "generated_at": generated_at,
            "source": "https://example.test/",
            "crawl_config": settings.manifest(settings.load()),
            "checks_skipped": [],
            "checks_disabled": [],
            "crawl_partial": False,
            **run,
        },
        "summary": {
            "check_coverage": {
                "checks_silent_ids": ["TITLE_MISSING", "DESC_MISSING", "CANONICAL_MISSING"]
            },
            **({"evidence_contract": {"scan_uuid": scan_uuid}} if scan_uuid else {}),
        },
        "pages": [
            {
                "url": url,
                "status_code": 200,
                "content_type": "text/html",
                "metrics": {"representation": representation},
            }
            for url in urls
        ],
        "issues": list(issues),
    }


def _issue(identifier, check, url, **extra):
    return {"id": identifier, "check": check, "target_url": url, "status_code": 200, **extra}


def test_selection_accepts_ids_saved_view_and_supplied_urls():
    baseline = _audit(
        issues=[
            _issue("ISSUE-000001", "TITLE_MISSING", A),
            _issue("ISSUE-000002", "DESC_MISSING", B),
            _issue("ISSUE-000003", "CANONICAL_MISSING", C),
        ]
    )
    chosen, urls = select(
        baseline,
        finding_ids=["ISSUE-000001"],
        urls=[B],
        view={"schema_version": "verification_view.v1", "checks": ["CANONICAL_MISSING"]},
    )
    assert [item["id"] for item in chosen] == ["ISSUE-000001", "ISSUE-000002", "ISSUE-000003"]
    assert urls == [A, B, C]
    with pytest.raises(ValueError, match="absent from baseline"):
        select(baseline, finding_ids=["ISSUE-999999"])
    with pytest.raises(ValueError, match="URLs are absent"):
        select(baseline, urls=["https://other.test/"])


def test_four_outcomes_use_exact_page_and_finding_evidence():
    baseline = _audit(
        issues=[
            _issue("ISSUE-000001", "TITLE_MISSING", A),
            _issue("ISSUE-000002", "DESC_MISSING", B, details={"value": "old"}),
            _issue("ISSUE-000003", "CANONICAL_MISSING", C),
        ]
    )
    after = _audit(
        issues=[
            _issue("ISSUE-000005", "DESC_MISSING", B, details={"value": "old"}),
            _issue("ISSUE-000006", "CANONICAL_MISSING", C, details={"value": "new"}),
        ]
    )
    items = classify(baseline, baseline["issues"], {A: after, B: after, C: after})
    assert [item["status"] for item in items] == ["resolved", "persisting", "changed"]
    assert items[0]["before"]["target_url"] == A and items[0]["after_page"]["url"] == A
    assert items[2]["after"]["details"] == {"value": "new"}


def test_missing_page_graph_check_changed_config_and_js_gap_are_not_fixes():
    graph = _issue("ISSUE-000004", "LOW_LINK_SCORE", B)
    baseline = _audit(issues=[_issue("ISSUE-000001", "TITLE_MISSING", A), graph])
    partial = _audit(urls=(B,), crawl_partial=True)
    statuses = classify(baseline, baseline["issues"], {A: partial, B: partial})
    assert [item["status"] for item in statuses] == ["not_verifiable", "not_verifiable"]
    assert "no page observation" in statuses[0]["reason"]
    assert "site-wide" in statuses[1]["reason"]

    changed_config = _audit(urls=(A,))
    changed_config["run"]["crawl_config"]["robots.policy"] = "ignore"
    result = classify(baseline, baseline["issues"][:1], {A: changed_config})[0]
    assert result["status"] == "not_verifiable" and "robots.policy" in result["reason"]

    rendered = _audit(urls=(A,), issues=baseline["issues"][:1], representation="rendered")
    result = classify(rendered, rendered["issues"], {A: _audit(urls=(A,))})[0]
    assert result["status"] == "not_verifiable" and "representation" in result["reason"]


def test_skipped_check_is_unverifiable_but_a_measured_page_in_partial_run_can_resolve():
    baseline = _audit(urls=(A,), issues=[_issue("ISSUE-000001", "TITLE_MISSING", A)])
    after = _audit(urls=(A,), crawl_partial=True)
    after["run"]["checks_skipped"] = [{"id": "TITLE_MISSING", "reason": "HTML was not parsed"}]
    row = classify(baseline, baseline["issues"], {A: after})[0]
    assert row["status"] == "not_verifiable" and "HTML was not parsed" in row["reason"]
    after["run"]["checks_skipped"] = []
    assert classify(baseline, baseline["issues"], {A: after})[0]["status"] == "resolved"


def test_graph_only_selection_never_launches_a_recrawl(tmp_path, monkeypatch):
    graph = _issue("ISSUE-000004", "LOW_LINK_SCORE", A)
    baseline = _audit(urls=(A,), issues=[graph])
    monkeypatch.setattr(handlers, "crawl_site", lambda **_kwargs: pytest.fail("unneeded crawl"))
    result = handlers.verify_fixes(
        baseline=baseline, finding_ids=[graph["id"]], out_dir=str(tmp_path / "verify")
    )
    assert result["selection"]["urls"] == []
    assert result["findings"][0]["status"] == "not_verifiable"


def test_redirected_html_page_is_changed_not_resolved():
    baseline = _audit(urls=(A,), issues=[_issue("ISSUE-000001", "TITLE_MISSING", A)])
    after = _audit(urls=(A,))
    after["pages"][0]["status_code"] = 301
    item = classify(baseline, baseline["issues"], {A: after})[0]
    assert item["status"] == "changed" and "successful HTML" in item["reason"]


def test_missing_response_and_wrong_source_are_not_fixes():
    baseline = _audit(urls=(A,), issues=[_issue("ISSUE-000001", "TITLE_MISSING", A)])
    after = _audit(urls=(A,))
    after["pages"][0]["status_code"] = None
    item = classify(baseline, baseline["issues"], {A: after})[0]
    assert item["status"] == "not_verifiable" and "no measured HTTP" in item["reason"]
    after["pages"][0]["status_code"] = 200
    after["run"]["source"] = "https://other.test/"
    item = classify(baseline, baseline["issues"], {A: after})[0]
    assert item["status"] == "not_verifiable" and "source origin" in item["reason"]
    del after["issues"]
    item = classify(baseline, baseline["issues"], {A: after})[0]
    assert item["status"] == "not_verifiable" and "complete audit" in item["reason"]


def test_status_check_needs_a_clean_measured_response():
    baseline = _audit(urls=(A,), issues=[_issue("ISSUE-000001", "BROKEN_PAGE_4XX", A)])
    after = _audit(urls=(A,))
    after["summary"]["check_coverage"]["checks_silent_ids"].append("BROKEN_PAGE_4XX")
    after["pages"][0]["status_code"] = 500
    assert classify(baseline, baseline["issues"], {A: after})[0]["status"] == "changed"
    after["pages"][0]["status_code"] = 200
    assert classify(baseline, baseline["issues"], {A: after})[0]["status"] == "resolved"


def test_offline_handler_writes_immutable_linked_json_and_focused_report(tmp_path):
    baseline = _audit(
        urls=(A,), issues=[_issue("ISSUE-000001", "TITLE_MISSING", A)], scan_uuid="before-scan"
    )
    after = _audit(urls=(A,), scan_uuid="after-scan", generated_at="2026-10-02T00:00:00Z")
    out = tmp_path / "verification"
    result = handlers.verify_fixes(
        baseline=baseline, after=after, finding_ids=["ISSUE-000001"], out_dir=str(out)
    )
    saved = json.loads((out / "verification.json").read_text(encoding="utf-8"))
    assert result["summary"]["resolved"] == 1, result["findings"][0]["reason"]
    assert saved["baseline"]["audit_sha256"] == result["baseline"]["audit_sha256"]
    assert saved["findings"][0]["before"]["id"] == "ISSUE-000001"
    assert A in (out / "verification.md").read_text(encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        handlers.verify_fixes(baseline=baseline, after=after, urls=[A], out_dir=str(out))
    view = tmp_path / "view.json"
    view.write_text(
        json.dumps({"schema_version": "verification_view.v1", "finding_ids": ["ISSUE-000001"]}),
        encoding="utf-8",
    )
    from_view = handlers.verify_fixes(
        baseline=baseline, after=after, view=str(view), out_dir=str(tmp_path / "from-view")
    )
    assert from_view["selection"]["finding_ids"] == ["ISSUE-000001"]


@pytest.mark.parametrize(
    "before_id,after_id,before_time,after_time,reason",
    [
        (
            "same-scan",
            "same-scan",
            "2026-10-01T00:00:00Z",
            "2026-10-02T00:00:00Z",
            "same scan UUID",
        ),
        (
            "before-scan",
            "after-scan",
            "2026-10-02T00:00:00Z",
            "2026-10-01T00:00:00Z",
            "not later",
        ),
        (
            "before-scan",
            "after-scan",
            "2026-10-01T00:00:00Z",
            "2026-10-01T00:00:00Z",
            "not later",
        ),
        (
            None,
            "after-scan",
            "2026-10-01T00:00:00Z",
            "2026-10-02T00:00:00Z",
            "lacks a recorded scan UUID",
        ),
        (
            "before-scan",
            None,
            "2026-10-01T00:00:00Z",
            "2026-10-02T00:00:00Z",
            "lacks a recorded scan UUID",
        ),
        ("before-scan", "after-scan", None, "2026-10-02T00:00:00Z", "lacks a timezone-aware"),
        ("before-scan", "after-scan", "2026-10-01T00:00:00Z", None, "lacks a timezone-aware"),
        (
            "before-scan",
            "after-scan",
            "2026-10-01T00:00:00",
            "2026-10-02T00:00:00Z",
            "lacks a timezone-aware",
        ),
    ],
)
def test_offline_after_requires_distinct_later_observation(
    tmp_path, before_id, after_id, before_time, after_time, reason
):
    baseline = _audit(
        urls=(A,),
        issues=[_issue("ISSUE-000001", "TITLE_MISSING", A)],
        scan_uuid=before_id,
        generated_at=before_time,
    )
    after = _audit(urls=(A,), scan_uuid=after_id, generated_at=after_time)
    out = tmp_path / "verification"
    result = handlers.verify_fixes(
        baseline=baseline, after=after, finding_ids=["ISSUE-000001"], out_dir=str(out)
    )
    assert result["summary"] == {
        "resolved": 0,
        "persisting": 0,
        "changed": 0,
        "not_verifiable": 1,
    }
    assert result["collection"]["state"] == "not_verifiable"
    assert reason in result["collection"]["reason"]
    assert reason in result["findings"][0]["reason"]
    assert (
        json.loads((out / "verification.json").read_text(encoding="utf-8"))["summary"]["resolved"]
        == 0
    )


def test_cli_offline_path_uses_the_shared_handler(tmp_path, capsys):
    from seohead.cli import main

    baseline = _audit(
        urls=(A,), issues=[_issue("ISSUE-000001", "TITLE_MISSING", A)], scan_uuid="before-scan"
    )
    before_file, after_file = tmp_path / "before.json", tmp_path / "after.json"
    before_file.write_text(json.dumps(baseline), encoding="utf-8")
    after_file.write_text(
        json.dumps(_audit(urls=(A,), scan_uuid="after-scan", generated_at="2026-10-02T00:00:00Z")),
        encoding="utf-8",
    )
    out = tmp_path / "verification"
    status = main(
        [
            "verify-fixes",
            "--baseline",
            str(before_file),
            "--after",
            str(after_file),
            "--finding-ids",
            "ISSUE-000001",
            "--out-dir",
            str(out),
        ]
    )
    assert status == 0
    assert json.loads(capsys.readouterr().out)["summary"]["resolved"] == 1
    assert (out / "verification.json").is_file()


def test_live_raw_workflow_reuses_recorded_policy_and_only_selected_urls(tmp_path, monkeypatch):
    baseline = _audit(urls=(A, B), issues=[_issue("ISSUE-000001", "TITLE_MISSING", A)])
    calls = []

    def fake_crawl_site(**kwargs):
        calls.append(kwargs)
        folder = Path(kwargs["out_dir"])
        folder.mkdir(parents=True)
        after = _audit(urls=(A,))
        after["run"]["crawl_config"]["limits.max_urls"] = 1
        (folder / "audit.json").write_text(json.dumps(after), encoding="utf-8")
        return {"out_dir": str(folder)}

    monkeypatch.setattr(handlers, "crawl_site", fake_crawl_site)
    result = handlers.verify_fixes(
        baseline=baseline, finding_ids=["ISSUE-000001"], out_dir=str(tmp_path / "verify")
    )
    assert calls[0]["urls"] == [A] and calls[0]["max_urls"] == 1
    assert calls[0]["overrides"] == baseline["run"]["crawl_config"]
    assert result["collection"]["mode"] == "list"
    assert result["findings"][0]["status"] == "resolved"


def test_live_js_workflow_runs_each_selected_url_with_recorded_render_policy(tmp_path, monkeypatch):
    baseline = _audit(
        urls=(A, B),
        issues=[
            _issue("ISSUE-000001", "TITLE_MISSING", A),
            _issue("ISSUE-000002", "TITLE_MISSING", B),
        ],
        representation="rendered",
    )
    baseline["run"]["crawl_config"]["rendering.mode"] = "js"
    seen = []

    def fake_crawl_site(**kwargs):
        seen.append(kwargs["url"])
        folder = Path(kwargs["out_dir"])
        folder.mkdir(parents=True)
        after = _audit(urls=(kwargs["url"],), representation="rendered")
        after["run"]["crawl_config"] = copy.deepcopy(baseline["run"]["crawl_config"])
        after["run"]["crawl_config"]["limits.max_urls"] = 1
        (folder / "audit.json").write_text(json.dumps(after), encoding="utf-8")
        return {"out_dir": str(folder)}

    monkeypatch.setattr(handlers, "crawl_site", fake_crawl_site)
    result = handlers.verify_fixes(baseline=baseline, urls=[A, B], out_dir=str(tmp_path / "verify"))
    assert seen == [A, B]
    assert result["collection"]["mode"] == "js"
    assert result["summary"]["resolved"] == 2


def test_partial_js_recrawl_keeps_completed_evidence_and_names_unfetched_url(tmp_path, monkeypatch):
    baseline = _audit(
        urls=(A, B),
        issues=[
            _issue("ISSUE-000001", "TITLE_MISSING", A),
            _issue("ISSUE-000002", "TITLE_MISSING", B),
        ],
        representation="rendered",
    )
    baseline["run"]["crawl_config"]["rendering.mode"] = "js"

    def fake_crawl_site(**kwargs):
        if kwargs["url"] == B:
            raise RuntimeError("synthetic render failure")
        folder = Path(kwargs["out_dir"])
        folder.mkdir(parents=True)
        after = _audit(urls=(A,), representation="rendered")
        after["run"]["crawl_config"] = copy.deepcopy(baseline["run"]["crawl_config"])
        after["run"]["crawl_config"]["limits.max_urls"] = 1
        (folder / "audit.json").write_text(json.dumps(after), encoding="utf-8")
        return {"out_dir": str(folder)}

    monkeypatch.setattr(handlers, "crawl_site", fake_crawl_site)
    result = handlers.verify_fixes(baseline=baseline, urls=[A, B], out_dir=str(tmp_path / "verify"))
    assert result["collection"]["state"] == "partial"
    assert len(result["collection"]["audits"]) == 1
    assert [item["status"] for item in result["findings"]] == ["resolved", "not_verifiable"]
    assert "synthetic render failure" in result["findings"][1]["reason"]


def test_policy_mismatch_stops_before_network_and_records_not_verifiable(tmp_path, monkeypatch):
    baseline = _audit(urls=(A,), issues=[_issue("ISSUE-000001", "TITLE_MISSING", A)])
    baseline["run"]["crawl_config"]["robots.policy"] = "ignore"
    monkeypatch.setattr(handlers, "crawl_site", lambda **_kwargs: pytest.fail("network attempted"))
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    result = handlers.verify_fixes(
        baseline=baseline, urls=[A], config=str(config), out_dir=str(tmp_path / "verify")
    )
    assert result["collection"]["state"] == "not_run"
    assert result["summary"]["not_verifiable"] == 1
    assert "robots.policy" in result["findings"][0]["reason"]


def test_local_synthetic_recrawl_verifies_a_title_fix_end_to_end(tmp_path, monkeypatch):
    """The real list collector and analyzer run twice against only local synthetic HTML."""
    state = {"title": False}

    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            html = (
                "<html><head>"
                + ("<title>Fixed title for this page</title>" if state["title"] else "")
                + "</head><body><h1>Example</h1><p>Useful synthetic page.</p></body></html>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    try:
        url = f"http://127.0.0.1:{server.server_port}/page"
        baseline_dir = tmp_path / "baseline"
        handlers.crawl_site(
            urls=[url],
            out_dir=str(baseline_dir),
            overrides={"robots.policy": "ignore", "speed.min_delay_seconds": 0},
        )
        baseline = json.loads((baseline_dir / "audit.json").read_text(encoding="utf-8"))
        finding = next(issue for issue in baseline["issues"] if issue["check"] == "TITLE_MISSING")
        state["title"] = True
        result = handlers.verify_fixes(
            baseline=str(baseline_dir / "audit.json"),
            finding_ids=[finding["id"]],
            out_dir=str(tmp_path / "verification"),
        )
        assert result["summary"]["resolved"] == 1
        assert result["collection"]["mode"] == "list"
        assert result["findings"][0]["url"] == url
        assert (tmp_path / "verification" / "recrawl" / "audit.json").is_file()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_js_policy_recrawls_and_compares_rendered_evidence_offline(tmp_path, monkeypatch):
    """The workflow invokes the existing render bridge, with synthetic browser output."""
    import seohead.crawl.spider as spider_module
    from seohead.crawl.collect import PageRecord
    from seohead.crawl.spider import SpiderResult
    from seohead.tools import render as render_tool

    state = {"fixed": False}

    def fake_spider(url, **_kwargs):
        result = SpiderResult()
        result.pages = [
            PageRecord(url=url, status_code=200, content_type="text/html", h1="Example", outlinks=1)
        ]
        result.start_page_evidence = {
            "html": "<html><body><h1>Example</h1></body></html>",
            "outlinks": 1,
        }
        return result

    def fake_render_document(url, _config, **_kwargs):
        title = "<title>Fixed by JavaScript</title>" if state["fixed"] else ""
        return {
            "ok": True,
            "final_url": url,
            "html": f"<html><head>{title}</head><body><h1>Example</h1><p>Rendered page.</p></body></html>",
        }

    monkeypatch.setattr(spider_module, "crawl_site", fake_spider)
    monkeypatch.setattr(render_tool, "render_document", fake_render_document)
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "robots": {"policy": "ignore"},
                "rendering": {"mode": "js", "escalation": {"policy": "full", "max_render_urls": 1}},
            }
        ),
        encoding="utf-8",
    )
    baseline_dir = tmp_path / "baseline"
    handlers.crawl_site(url=A, out_dir=str(baseline_dir), config=str(config), max_urls=1)
    baseline = json.loads((baseline_dir / "audit.json").read_text(encoding="utf-8"))
    finding = next(issue for issue in baseline["issues"] if issue["check"] == "TITLE_MISSING")
    assert (
        next(page for page in baseline["pages"] if page["url"] == A)["metrics"]["representation"]
        == "rendered"
    )
    state["fixed"] = True
    result = handlers.verify_fixes(
        baseline=str(baseline_dir / "audit.json"),
        finding_ids=[finding["id"]],
        config=str(config),
        out_dir=str(tmp_path / "verification"),
    )
    assert result["collection"]["mode"] == "js"
    assert result["summary"]["resolved"] == 1, result["findings"][0]["reason"]
    assert result["findings"][0]["after_page"]["metrics"]["representation"] == "rendered"
