"""Offline, retained-evidence diagnostics for a small or interrupted crawl."""

from __future__ import annotations

import json

import pytest

from seohead.crawl.events import EventSink
from seohead.crawl.settings import load, manifest
from seohead.crawl.sqlite_adapter import crawl_to_scan
from seohead.servers import handlers
from seohead.storage.native_scan import NativeScan

URL = "https://example.test/"
RUNTIME = {key: "test" for key in ("python", "sqlite", "httpx", "lxml", "beautifulsoup4")}


class Response:
    def __init__(self, status: int, text: str, content_type: str = "text/html") -> None:
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = {"content-type": content_type}


def saved_scan(
    tmp_path,
    *,
    html="<html><body>Only page</body></html>",
    robots="User-agent: *\nAllow: /\n",
    overrides=None,
    finalize=True,
):
    path = tmp_path / "scan.sqlite"

    def fetch(url):
        if url.endswith("robots.txt"):
            return Response(200, robots, "text/plain")
        return Response(200, html)

    run = crawl_to_scan(
        URL,
        scan_out=str(path),
        settings=load(overrides={"speed.min_delay_seconds": 0, **(overrides or {})}),
        producer_version="3.0.0",
        producer_revision="a" * 40,
        runtime_versions=RUNTIME,
        fetcher=fetch,
        sleeper=lambda _seconds: None,
    )
    if finalize:
        with NativeScan.open(path) as scan:
            scan.finish_capture(reason=run.finish_reason)
    return path, run


def legacy_run(tmp_path, *, html_page=None, decisions=(), run_fields=None):
    path = tmp_path / "run"
    path.mkdir()
    audit_run = {
        "input_mode": "crawl",
        "source": URL,
        "crawl_finish_reason": "finished",
        "crawl_partial": False,
        "crawl_config": manifest(load()),
        **(run_fields or {}),
    }
    (path / "audit.json").write_text(json.dumps({"run": audit_run, "summary": {}}))
    if html_page is not None:
        (path / "pages.jsonl").write_text(
            json.dumps(
                {
                    "url": URL,
                    "status_code": 200,
                    "content_type": "text/html",
                    "outlinks": 0,
                    **html_page,
                }
            )
            + "\n"
        )
    (path / "decisions.jsonl").write_text("".join(json.dumps(d) + "\n" for d in decisions))
    return path


def codes(result):
    return {item["code"] for item in result["diagnoses"]}


def test_healthy_one_page_is_finished_not_stalled(tmp_path):
    scan, _ = saved_scan(tmp_path)
    result = handlers.crawl_diagnose(scan=str(scan))
    assert result["source"]["lifecycle"] == "finished"
    assert result["observed"]["page_records"] == 1
    assert result["observed"]["site_total_urls"] is None
    assert "complete_one_page" in codes(result)
    assert "worker_state_unverified" not in codes(result)


def test_robots_decision_is_cited_without_bypass_advice(tmp_path):
    scan, _ = saved_scan(
        tmp_path,
        html='<html><a href="/private">Private</a></html>',
        robots="User-agent: *\nDisallow: /private\n",
    )
    result = handlers.crawl_diagnose(scan=str(scan))
    assert "robots_exclusion" in codes(result)
    assert result["decisions"]["by_reason"]["blocked_by_robots"] >= 1
    assert any(d["url"] == "https://example.test/private" for d in result["decisions"]["sample"])
    assert "do not bypass robots silently" in str(result["diagnoses"])


def test_url_budget_and_running_worker_are_not_reported_as_site_causes(tmp_path):
    worker = tmp_path / "worker"
    worker.mkdir()
    scan, _ = saved_scan(worker, finalize=False)
    running = handlers.crawl_diagnose(scan=str(scan))
    assert "worker_state_unverified" in codes(running)
    budget = tmp_path / "budget"
    budget.mkdir()
    scan, run = saved_scan(
        budget,
        html='<html><a href="/a">A</a></html>',
        overrides={"limits.max_urls": 1},
        finalize=False,
    )
    assert run.finish_reason == "url_limit"
    with NativeScan.open(scan) as writer:
        writer.interrupt(run.finish_reason)
    stopped = handlers.crawl_diagnose(scan=str(scan))
    assert "budget_exhausted" in codes(stopped)
    assert stopped["source"]["finish_reason"] == "url_limit"


def test_js_eligibility_and_unavailable_rendering_are_distinct(tmp_path):
    run = legacy_run(
        tmp_path,
        html_page={},
        run_fields={
            "requires_rendering": True,
            "render_escalation": {
                "render_requests": 0,
                "patterns_unprobed": ["/app/*"],
                "patterns_unprobed_reasons": {"/app/*": "browser unavailable"},
            },
        },
    )
    result = handlers.crawl_diagnose(run=str(run))
    assert {"render_eligibility", "render_unavailable", "dependency_unavailable"} <= codes(result)
    assert result["settings"]["rendering_mode"] == "raw"
    assert result["observed"]["render"]["unprobed_patterns"] == 1


def test_fetch_failure_and_non_html_target_do_not_become_clean(tmp_path):
    run = legacy_run(
        tmp_path, html_page={"content_type": "application/pdf", "error_kind": "timeout"}
    )
    result = handlers.crawl_diagnose(run=str(run))
    assert {"fetch_failure", "non_html_start"} <= codes(result)


def test_scan_v2_provider_failure_is_reported_from_saved_event(tmp_path):
    scan, _ = saved_scan(tmp_path, overrides={"storage.format_version": "scan.v2"}, finalize=False)
    sink = EventSink()
    sink.emit(
        "provider_enrichment",
        {
            "provider": "synthetic",
            "operation": "enrich",
            "state": "unavailable",
            "rows": 0,
        },
    )
    with NativeScan.open(scan) as writer:
        writer.record_events(sink)
        writer.finish_capture(reason="finished")
    result = handlers.crawl_diagnose(scan=str(scan))
    assert result["observed"]["provider_failures"] == 1
    assert "provider_failure" in codes(result)


def test_scope_redirect_and_depth_decisions_keep_exact_samples(tmp_path):
    run = legacy_run(
        tmp_path,
        html_page={"outlinks": 3},
        decisions=[
            {"url": "https://outside.test/", "source": URL, "reason": "outside_host", "depth": 1},
            {
                "url": "https://outside.test/old",
                "source": URL,
                "reason": "redirect_off_host",
                "depth": 1,
            },
            {
                "url": "https://example.test/deep",
                "source": URL,
                "reason": "depth_limit",
                "depth": 6,
            },
        ],
    )
    result = handlers.crawl_diagnose(run=str(run), max_decisions=2)
    assert {"scope_exclusion", "redirect_discovery", "depth_exclusion"} <= codes(result)
    assert result["decisions"]["omitted_from_sample"] == 1
    assert result["decisions"]["sample"][0]["url"] == "https://outside.test/"
    assert result["settings"]["limits"]["max_depth"] == 5


def test_missing_or_corrupt_legacy_evidence_does_not_look_complete(tmp_path):
    run = legacy_run(tmp_path, html_page=None)
    result = handlers.crawl_diagnose(run=str(run))
    assert result["observed"]["page_records"] is None
    assert "complete_one_page" not in codes(result)
    (run / "decisions.jsonl").write_text("{broken\n")
    with pytest.raises(ValueError, match="invalid JSON"):
        handlers.crawl_diagnose(run=str(run))


def test_partial_native_link_evidence_does_not_claim_complete_one_page(tmp_path):
    from tests.test_scan_native import _metadata, _record, _runtime

    path = tmp_path / "partial.sqlite"
    with NativeScan.create(path, **_metadata()) as scan:
        scan.enqueue([(URL, 0)])
        scan.commit_page(
            scan.claim(1)[0],
            _record(URL),
            runtime=_runtime(),
            partial_reasons=("link_observations_omitted",),
        )
        scan.finish_capture()
    result = handlers.crawl_diagnose(scan=str(path))
    assert result["source"]["crawl_partial"] is True
    assert result["coverage"]["links"] == "partial"
    assert "coverage_gap" in codes(result)
    assert "complete_one_page" not in codes(result)


def test_missing_legacy_decisions_does_not_claim_complete_one_page(tmp_path):
    run = legacy_run(tmp_path, html_page={"outlinks": 0})
    (run / "links.jsonl").write_text("")
    (run / "decisions.jsonl").unlink()
    result = handlers.crawl_diagnose(run=str(run))
    assert result["source"]["crawl_partial"] is False
    assert result["coverage"]["decisions"] == "unavailable"
    assert "coverage_gap" in codes(result)
    assert "complete_one_page" not in codes(result)


def test_redacted_export_is_opt_in_bounded_and_never_overwrites(tmp_path):
    run = legacy_run(
        tmp_path,
        html_page={},
        decisions=[
            {
                "url": "https://example.test/private?token=synthetic",
                "source": URL,
                "reason": "outside_host",
                "depth": 1,
            }
        ],
    )
    output = tmp_path / "diagnostic.json"
    result = handlers.crawl_diagnose_export(run=str(run), max_decisions=1, export=str(output))
    assert result["redacted_export"] == str(output)
    redacted = output.read_text()
    assert "example.test" not in redacted and "synthetic" not in redacted
    assert output.stat().st_mode & 0o777 == 0o600
    assert json.loads(redacted)["decisions"]["sample"][0]["reason"] == "outside_host"
    with pytest.raises(FileExistsError):
        handlers.crawl_diagnose_export(run=str(run), export=str(output))
    assert len(result["decisions"]["sample"]) == 1


def test_export_redacts_freeform_labels_and_scan_identity(tmp_path):
    secret = "supersecrettoken"
    run = legacy_run(
        tmp_path,
        html_page={"error_kind": secret},
        decisions=[{"url": URL, "source": URL, "reason": secret, "depth": 1}],
        run_fields={"crawl_finish_reason": secret},
    )
    output = tmp_path / "freeform-redacted.json"
    handlers.crawl_diagnose_export(run=str(run), export=str(output))
    redacted = json.loads(output.read_text())
    assert secret not in output.read_text()
    assert redacted["source"]["finish_reason"] == "[redacted]"
    assert redacted["observed"]["page_errors"] == {"[redacted]": 1}
    assert redacted["decisions"]["by_reason"] == {"[redacted]": 1}
    assert redacted["decisions"]["sample"][0]["reason"] == "[redacted]"

    scan_dir = tmp_path / "native"
    scan_dir.mkdir()
    scan, _ = saved_scan(scan_dir)
    output = tmp_path / "native-redacted.json"
    raw = handlers.crawl_diagnose_export(scan=str(scan), export=str(output))
    assert raw["source"]["scan_uuid"] not in output.read_text()
    assert json.loads(output.read_text())["source"]["scan_uuid"] == "[redacted]"


def test_cli_prints_readable_summary_and_json(tmp_path, capsys):
    from seohead.cli import main

    run = legacy_run(
        tmp_path,
        html_page={},
        decisions=[
            {
                "url": "https://example.test/skip",
                "source": URL,
                "reason": "depth_limit",
                "depth": 6,
            }
        ],
    )
    assert main(["crawl-diagnose", "--run", str(run)]) == 0
    captured = capsys.readouterr()
    assert "site total unknown" in captured.err
    assert "decision #1: depth_limit" in captured.err
    assert "elapsed=unknown duration" in captured.err
    assert json.loads(captured.out)["schema_version"] == "crawl_diagnostics.v1"


def test_mcp_diagnosis_has_no_file_write_permission_or_export_argument():
    from seohead.servers.tool_reference import load_seo_tools

    tool = next(spec for spec in load_seo_tools() if spec.name == "seo_crawl_diagnose")
    assert tool.writes is False
    assert "export" not in {argument.name for argument in tool.arguments}
    export = next(spec for spec in load_seo_tools() if spec.name == "seo_crawl_diagnose_export")
    assert export.writes is True
    assert "export" in {argument.name for argument in export.arguments}


def test_cli_export_command_requires_explicit_path(tmp_path, capsys):
    from seohead.cli import main

    run = legacy_run(tmp_path, html_page={})
    output = tmp_path / "cli-redacted.json"
    assert main(["crawl-diagnose-export", "--run", str(run), "--export", str(output)]) == 0
    assert json.loads(output.read_text())["source"]["start_url"] == "[redacted]"
    capsys.readouterr()
    assert main(["crawl-diagnose-export", "--run", str(run)]) == 1
    assert "export path is required" in capsys.readouterr().err
