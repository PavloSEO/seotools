"""Offline acceptance for the full SF export manifest and issue #715."""

from __future__ import annotations

import csv
import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from seohead.sf.config import DEFAULT_CONFIG, LITE_EXPORTS, apply_profile, deep_merge, load_config
from seohead.sf.core import audit as audit_core
from seohead.sf.core import runner
from seohead.sf.core.spiderconfig import read_speed
from seohead.sf.export_manifest import (
    help_names,
    profile_exports,
    requests_from_config,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sf_export_help_19_8.json"
SF_HELP_FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
HELP_FLAGS = {"tabs": "export-tabs", "bulk": "bulk-export", "reports": "save-report"}

_KEY_FILES = {
    "internal_all": "internal_all.csv",
    "resp_4xx": "response_codes_client_error_(4xx).csv",
    "resp_5xx": "response_codes_server_error_(5xx).csv",
    "resp_3xx": "response_codes_redirection_(3xx).csv",
    "sitemap_in": "urls_in_sitemap.csv",
    "sitemap_not_in": "urls_not_in_sitemap.csv",
    "sitemap_orphan": "orphan_urls.csv",
    "sitemap_non_indexable": "non_indexable_urls_in_sitemap.csv",
    "titles_multiple": "page_titles_multiple.csv",
    "security_mixed": "security_mixed_content.csv",
    "images_missing_alt": "images_missing_alt_text.csv",
    "images_missing_size": "images_missing_size_attributes.csv",
    "inlinks_4xx": "response_codes_internal_external_client_error_(4xx)_inlinks.csv",
    "inlinks_5xx": "response_codes_internal_external_server_error_(5xx)_inlinks.csv",
    "inlinks_3xx": "response_codes_internal_external_redirection_(3xx)_inlinks.csv",
    "redirect_chains": "redirect_chains.csv",
    "hreflang": "hreflang_non_200_hreflang_urls.csv",
    "all_hreflang": "all_hreflang_urls.csv",
    "titles_duplicate": "page_titles_duplicate.csv",
    "desc_duplicate": "meta_description_duplicate.csv",
    "images_over_kb": "images_over_150_kb.csv",
    "security_hsts": "security_missing_hsts_header.csv",
    "structured_data_missing": "structured_data_missing.csv",
    "resp_no_response": "response_codes_no_response.csv",
    "resp_blocked": "response_codes_blocked_by_robots.txt.csv",
}
_RAW_FILES = {
    "structured_data_validation_errors": "structured_data_validation_errors.csv",
    "structured_data_validation_warnings": "structured_data_validation_warnings.csv",
    "javascript_all": "javascript_all.csv",
    "canonicals_all": "canonicals_all.csv",
    "h1_all": "h1_all.csv",
}


def _help_text(group: str) -> str:
    names = SF_HELP_FIXTURE["exports"][group]
    flag = HELP_FLAGS[group]
    return (
        f"Running: Screaming Frog SEO Spider {SF_HELP_FIXTURE['version']}\n"
        f"The option '--{flag}' supports the following arguments:\n\n" + "\n".join(names) + "\n\n"
    )


def _install_fake_sf(
    monkeypatch,
    output_root: Path,
    *,
    omit: str | None = None,
    corrupt: str | None = None,
    sitemap_status: bool = True,
):
    """Use the checked SF 19.8 help fixture and write only synthetic .test exports."""
    monkeypatch.setattr(runner, "resolve_cli", lambda *_args, **_kwargs: "/fake/sf")
    monkeypatch.setattr(runner, "_query_export_help", lambda _cli, group: _help_text(group))
    seen: dict[str, object] = {}

    def fake_run(cmd, _timeout, output_folder, _log):
        seen["cmd"] = list(cmd)
        fresh = Path(output_folder) / "2026-10-03-synthetic-run"
        fresh.mkdir(parents=True, exist_ok=True)
        _write_synthetic_exports(fresh, omit=omit, corrupt=corrupt, sitemap_status=sitemap_status)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(runner, "_run_watched", fake_run)
    return seen


def _write_csv(path: Path, header: list[str], rows: list[list[object]] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows or [])


def _write_synthetic_exports(
    directory: Path,
    *,
    omit: str | None = None,
    corrupt: str | None = None,
    sitemap_status: bool = True,
):
    requests = requests_from_config(DEFAULT_CONFIG["exports"])
    written: set[str] = set()
    for request in requests:
        names = [_KEY_FILES[key] for key in request.keys]
        if request.raw_id:
            names.append(_RAW_FILES[request.raw_id])
        for name in names:
            if name == omit or name in written:
                continue
            path = directory / name
            if name == corrupt:
                path.write_bytes(b"")
                written.add(name)
                continue
            if name == "internal_all.csv":
                _write_csv(
                    path,
                    [
                        "Address",
                        "Content Type",
                        "Status Code",
                        "Indexability",
                        "Title 1",
                        "Meta Description 1",
                        "H1-1",
                        "Canonical Link Element 1",
                        "Word Count",
                        "Crawl Depth",
                        "Inlinks",
                        "Response Time",
                    ],
                    [
                        [
                            "https://example.test/",
                            "text/html",
                            200,
                            "Indexable",
                            "Synthetic title",
                            "Synthetic description",
                            "Synthetic heading",
                            "https://example.test/",
                            350,
                            0,
                            0,
                            0.1,
                        ]
                    ],
                )
            elif name == "urls_in_sitemap.csv":
                header = (
                    ["Address", "Content Type", "Status Code", "Indexability"]
                    if sitemap_status
                    else ["Address", "Content Type", "Indexability"]
                )
                _write_csv(
                    path,
                    header,
                    [
                        ["https://example.test/", "text/html", 200, "Indexable"]
                        if sitemap_status
                        else ["https://example.test/", "text/html", "Indexable"],
                        ["https://example.test/redirect", "text/html", 301, "Non-Indexable"]
                        if sitemap_status
                        else ["https://example.test/redirect", "text/html", "Non-Indexable"],
                        ["https://example.test/missing", "text/html", 404, "Non-Indexable"]
                        if sitemap_status
                        else ["https://example.test/missing", "text/html", "Non-Indexable"],
                    ],
                )
            elif name == "all_hreflang_urls.csv":
                _write_csv(path, ["Source", "Destination", "Hreflang"])
            else:
                _write_csv(path, ["Address", "Status Code"])
            written.add(name)
    return written


def _full_config():
    return load_config(None)


def test_full_manifest_is_exact_projection_of_declared_requests_and_help_fixture():
    exports = DEFAULT_CONFIG["exports"]
    assert exports == profile_exports()
    requests = requests_from_config(exports)
    help_exports = SF_HELP_FIXTURE["exports"]
    assert all(request.name in help_exports[request.group] for request in requests)
    assert len({(request.group, request.name) for request in requests}) == len(requests)

    issue_keys = {key for request in requests for key in (*request.keys, *request.derived_keys)}
    assert {
        "hreflang",
        "all_hreflang",
        "titles_duplicate",
        "desc_duplicate",
        "images_over_kb",
        "security_hsts",
        "structured_data_missing",
        "sitemap_redirects",
        "sitemap_non_200",
        "resp_no_response",
        "resp_blocked",
    } <= issue_keys
    raw = {request.raw_id for request in requests if request.raw_id}
    assert {"javascript_all", "canonicals_all", "h1_all"} <= raw
    sitemap = next(request for request in requests if "sitemap_in" in request.keys)
    assert set(sitemap.derived_keys) == {"sitemap_redirects", "sitemap_non_200"}
    assert sitemap.derived_required_columns == ("Status Code",)
    assert all(
        "Sitemaps:Redirects" not in request.name and "Sitemaps:Non-200" not in request.name
        for request in requests
    )


def test_help_parser_and_unknown_name_fail_before_a_crawl(tmp_path, monkeypatch):
    tabs_help = _help_text("tabs")
    assert "Structured Data:Missing" in help_names(tabs_help, "tabs")
    monkeypatch.setattr(runner, "resolve_cli", lambda *_args, **_kwargs: "/fake/sf")
    monkeypatch.setattr(runner, "_query_export_help", lambda _cli, group: _help_text(group))
    monkeypatch.setattr(
        runner, "_run_watched", lambda *_args, **_kwargs: pytest.fail("SF must not start")
    )
    config = _full_config()
    config["profile"] = "custom"
    config["exports"] = {
        "tabs": ["Sitemaps:Redirects"],
        "bulk": [],
        "reports": [],
        "fetch_all_inlinks": False,
    }
    with pytest.raises(
        RuntimeError, match=r"Sitemaps:Redirects.*No crawl or audit analysis was started"
    ):
        runner.run_sf(
            mode="crawl-list",
            source="urls.txt",
            output_folder=str(tmp_path / "out"),
            config=config,
            log=lambda _message: None,
        )
    assert not (tmp_path / "out").exists()


def test_custom_and_lite_exports_are_not_expanded_to_full():
    custom = deep_merge(
        DEFAULT_CONFIG,
        {
            "profile": "custom",
            "exports": {"tabs": ["Internal:All"], "bulk": [], "reports": []},
        },
    )
    assert apply_profile(custom)["exports"]["tabs"] == ["Internal:All"]
    lite = apply_profile(deep_merge(DEFAULT_CONFIG, {"profile": "lite"}))
    assert lite["exports"] == LITE_EXPORTS
    assert "JavaScript:All" not in lite["exports"]["tabs"]


def test_full_fake_mode_a_output_derives_sitemap_status_and_validates_audit(tmp_path, monkeypatch):
    from seohead.sf.reporters.jsonfile import load_schema

    _install_fake_sf(monkeypatch, tmp_path)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    result = audit_core.run_audit(
        input_mode="crawl-list",
        source=str(urls),
        config=_full_config(),
        output_dir=str(tmp_path / "report" / "exports"),
        live_recheck=False,
        log=lambda _message: None,
    )
    document = result.to_json()
    jsonschema.validate(document, load_schema())
    assert result.run["profile"] == "full"
    assert result.run["sf_export_manifest"]["state"] == "complete"
    assert result.run["exports_derived"] == {
        "sitemap_redirects": "sitemap_in",
        "sitemap_non_200": "sitemap_in",
    }
    assert result.run["sf_capability"]["version"] == SF_HELP_FIXTURE["version"]
    assert "sitemap_redirects" in result.run["exports_used"]
    assert "titles_duplicate" in result.run["exports_used"]
    raw_files = {
        item["raw_id"]: item["files"]
        for item in result.run["sf_export_manifest"]["requests"]
        if item["raw_id"]
    }
    assert raw_files["javascript_all"] == ["javascript_all.csv"]
    assert raw_files["canonicals_all"] == ["canonicals_all.csv"]
    assert raw_files["h1_all"] == ["h1_all.csv"]
    assert {issue.check for issue in result.issues} >= {
        "SITEMAP_URL_3XX",
        "SITEMAP_URL_4XX_5XX",
    }


def test_missing_requested_export_stops_before_rules_or_audit_output(tmp_path, monkeypatch):
    from seohead.sf.core import audit as audit_module

    _install_fake_sf(monkeypatch, tmp_path, omit="page_titles_duplicate.csv")
    monkeypatch.setattr(
        audit_module, "run_rules", lambda *_args, **_kwargs: pytest.fail("rules must not run")
    )
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    out = tmp_path / "report"
    with pytest.raises(RuntimeError, match=r"export manifest is incomplete.*Page Titles:Duplicate"):
        audit_module.run_audit(
            input_mode="crawl-list",
            source=str(urls),
            config=_full_config(),
            output_dir=str(out / "exports"),
            live_recheck=False,
            log=lambda _message: None,
        )
    assert not (out / "audit.json").exists()
    assert not (out / "tasks.json").exists()


def test_stale_full_exports_do_not_satisfy_the_current_run(tmp_path, monkeypatch):
    _install_fake_sf(monkeypatch, tmp_path, omit="page_titles_duplicate.csv")
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    out = tmp_path / "report" / "exports"
    _write_synthetic_exports(out / "old-run")
    with pytest.raises(RuntimeError, match="Page Titles:Duplicate"):
        audit_core.run_audit(
            input_mode="crawl-list",
            source=str(urls),
            config=_full_config(),
            output_dir=str(out),
            live_recheck=False,
            log=lambda _message: None,
        )
    assert (out / "old-run" / "page_titles_duplicate.csv").is_file()
    assert not (tmp_path / "report" / "audit.json").exists()


def test_unmeasured_sitemap_status_does_not_become_zero_findings(tmp_path, monkeypatch):
    _install_fake_sf(monkeypatch, tmp_path, sitemap_status=False)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"cannot derive sitemap status findings.*Status Code"):
        audit_core.run_audit(
            input_mode="crawl-list",
            source=str(urls),
            config=_full_config(),
            output_dir=str(tmp_path / "report" / "exports"),
            live_recheck=False,
            log=lambda _message: None,
        )


def test_corrupt_raw_tab_stops_before_analysis(tmp_path, monkeypatch):
    _install_fake_sf(monkeypatch, tmp_path, corrupt="javascript_all.csv")
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"JavaScript:All.*unreadable"):
        audit_core.run_audit(
            input_mode="crawl-list",
            source=str(urls),
            config=_full_config(),
            output_dir=str(tmp_path / "report" / "exports"),
            live_recheck=False,
            log=lambda _message: None,
        )


def _synthetic_speed_config(flag: int = 0, rate: float = 10.0) -> bytes:
    import struct

    from seohead.sf.core import spiderconfig as sc

    return (
        b"sr\x00)"
        + sc.PERF_CLASS
        + b"\x00\x00\x00\x00\x00\x00\x00\x01\x02\x00\x02Z\x00\x11mLimitPerformanceD\x00\x15"
        + sc.PERF_FIELD
        + sc.FIELDS_END
        + bytes([flag])
        + struct.pack(">d", rate)
        + b"sr\x00&seo.tail"
    )


def test_unrequested_rate_provenance_distinguishes_base_speed_from_unknown(tmp_path, monkeypatch):
    from seohead.sf.core import spiderconfig

    base = tmp_path / "audit.seospiderconfig"
    base.write_bytes(_synthetic_speed_config(flag=1, rate=1.5))
    monkeypatch.setattr(spiderconfig, "find_base_config", lambda _configured=None: str(base))

    configured = runner._rate_limit_provenance(
        {"sf_cli": {"seospiderconfig": "audit.seospiderconfig"}}, "crawl"
    )
    assert configured["state"] == "verified_from_base"
    assert configured["effective_urls_per_second"] == 1.5
    assert configured["base_source"] == "latest_saved_sf_config"

    monkeypatch.setattr(spiderconfig, "find_base_config", lambda _configured=None: None)
    unknown = runner._rate_limit_provenance({"sf_cli": {}}, "crawl")
    assert unknown["state"] == "not_requested_unverified"
    assert unknown["requested_urls_per_second"] is None
    assert unknown["effective_urls_per_second"] is None


def test_requested_rate_is_read_back_and_source_config_is_unchanged(tmp_path, monkeypatch):
    seen = _install_fake_sf(monkeypatch, tmp_path)
    base = tmp_path / "base.seospiderconfig"
    base_blob = _synthetic_speed_config()
    base.write_bytes(base_blob)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    config = _full_config()
    config["sf_cli"]["seospiderconfig"] = str(base)
    config["sf_cli"]["max_urls_per_second"] = 1.5

    result = audit_core.run_audit(
        input_mode="crawl-list",
        source=str(urls),
        config=config,
        output_dir=str(tmp_path / "report" / "exports"),
        live_recheck=False,
        log=lambda _message: None,
    )

    cmd = seen["cmd"]
    derived = Path(cmd[cmd.index("--config") + 1])
    assert derived.is_file()
    assert base.read_bytes() == base_blob
    assert read_speed(derived.read_bytes()) == (True, 1.5)
    rate = result.run["sf_rate_limit"]
    assert rate["state"] == "verified"
    assert rate["requested_urls_per_second"] == rate["effective_urls_per_second"] == 1.5
    assert rate["base_source"] == "configured"
    assert rate["base_config_sha256"]
    assert rate["derived_config_sha256"]
    assert "base.seospiderconfig" not in json.dumps(rate)


def test_rate_without_a_base_fails_before_sf_help_or_process(tmp_path, monkeypatch):
    from seohead.sf.core import spiderconfig

    monkeypatch.setattr(runner, "resolve_cli", lambda *_args, **_kwargs: "/fake/sf")
    monkeypatch.setattr(spiderconfig, "CRAWL_CONFIG_GLOBS", (str(tmp_path / "none" / "*"),))
    monkeypatch.setattr(
        runner, "_query_export_help", lambda *_args: pytest.fail("SF help must not run")
    )
    monkeypatch.setattr(runner, "_run_watched", lambda *_args: pytest.fail("SF must not start"))
    config = _full_config()
    config["sf_cli"]["seospiderconfig"] = str(tmp_path / "missing.seospiderconfig")
    config["sf_cli"]["max_urls_per_second"] = 1.5
    with pytest.raises(RuntimeError, match="no base Screaming Frog config"):
        runner.run_sf(
            mode="crawl-list",
            source="urls.txt",
            output_folder=str(tmp_path / "out"),
            config=config,
            log=lambda _message: None,
        )
    assert not (tmp_path / "out").exists()


def test_doctor_reports_read_back_base_rate_and_missing_setup(tmp_path, monkeypatch, capsys):
    from seohead.sf import cli as sf_cli
    from seohead.sf.core import spiderconfig

    base = tmp_path / "base.seospiderconfig"
    base.write_bytes(_synthetic_speed_config(flag=1, rate=1.5))
    monkeypatch.setattr(spiderconfig, "find_base_config", lambda *_args: str(base))
    sf_cli._report_base_config({"sf_cli": {"seospiderconfig": str(base)}})
    assert "enabled at 1.5 URLs/s (read back from config)" in capsys.readouterr().out

    monkeypatch.setattr(spiderconfig, "find_base_config", lambda *_args: None)
    sf_cli._report_base_config({"sf_cli": {"seospiderconfig": "missing.seospiderconfig"}})
    missing = capsys.readouterr().out
    assert "request-rate limit: NOT VERIFIED" in missing
    assert "Config → Speed" in missing and "seohead sf save-config" in missing


def test_cli_preserves_zero_rate_override_for_preflight(monkeypatch, capsys):
    from seohead.sf import cli as sf_cli

    captured = {}

    def stop_before_run(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("synthetic preflight stop")

    monkeypatch.setattr(sf_cli, "run_audit", stop_before_run)
    result = sf_cli.main(
        ["run", "--crawl-list", "urls.txt", "--max-urls-per-second", "0", "--quiet"]
    )
    assert result == 1
    assert captured["config_overrides"]["sf_cli"]["max_urls_per_second"] == 0.0
    assert "synthetic preflight stop" in capsys.readouterr().err


def test_load_crawl_rate_is_not_applicable_and_does_not_require_base(tmp_path, monkeypatch):
    seen = _install_fake_sf(monkeypatch, tmp_path)
    config = _full_config()
    config["sf_cli"]["max_urls_per_second"] = 1.5
    info = {}
    runner.run_sf(
        mode="load-crawl",
        source=str(tmp_path / "saved.seospider"),
        output_folder=str(tmp_path / "out"),
        config=config,
        log=lambda _message: None,
        run_info=info,
    )
    assert info["rate_limit"]["state"] == "not_applicable"
    assert "--config" not in seen["cmd"]


def test_mode_b_keeps_partial_exports_analyzable_without_full_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_core, "run_sitemap", lambda *_args, **_kwargs: {})
    exports = tmp_path / "exports"
    exports.mkdir()
    _write_csv(
        exports / "internal_all.csv",
        ["Address", "Content Type", "Status Code", "Indexability", "Title 1"],
        [["https://example.test/", "text/html", 200, "Indexable", "Example"]],
    )
    result = audit_core.run_audit(
        input_mode="parse-exports",
        exports_dir=str(exports),
        config=_full_config(),
        live_recheck=False,
        log=lambda _message: None,
    )
    assert result.run["profile"] == "full"
    assert "sf_export_manifest" not in result.run
    assert any(skip.id == "HREFLANG_ERROR" for skip in result.skipped)


def test_mcp_entrypoint_writes_only_after_full_manifest_succeeds(tmp_path, monkeypatch):
    from seohead.servers import sf_mcp

    _install_fake_sf(monkeypatch, tmp_path)
    monkeypatch.setattr(audit_core, "run_sitemap", lambda *_args, **_kwargs: {})
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_full_config()), encoding="utf-8")
    out = tmp_path / "mcp-out"
    result = sf_mcp._do_run(
        "crawl-list", str(urls), profile="full", out=str(out), config=str(config_path)
    )
    assert Path(result["json_path"]).is_file()
    assert Path(result["md_path"]).is_file()
    saved = json.loads(Path(result["json_path"]).read_text(encoding="utf-8"))
    from seohead.sf.reporters.jsonfile import load_schema

    jsonschema.validate(saved, load_schema())
    assert saved["run"]["sf_export_manifest"]["state"] == "complete"


def test_mcp_incomplete_manifest_does_not_write_audit_or_tasks(tmp_path, monkeypatch):
    from seohead.servers import sf_mcp

    _install_fake_sf(monkeypatch, tmp_path, omit="page_titles_duplicate.csv")
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_full_config()), encoding="utf-8")
    out = tmp_path / "mcp-out"
    with pytest.raises(RuntimeError, match="Page Titles:Duplicate"):
        sf_mcp._do_run(
            "crawl-list", str(urls), profile="full", out=str(out), config=str(config_path)
        )
    assert not (out / "audit.json").exists()
    assert not (out / "audit.md").exists()
    assert not (out / "tasks.json").exists()


def test_cli_full_profile_writes_tasks_only_after_manifest_completes(tmp_path, monkeypatch):
    from seohead.sf import cli

    _install_fake_sf(monkeypatch, tmp_path)
    urls = tmp_path / "urls.txt"
    urls.write_text("https://example.test/\n", encoding="utf-8")
    out = tmp_path / "cli-out"
    code = cli.main(
        [
            "run",
            "--crawl-list",
            str(urls),
            "--profile",
            "full",
            "--out",
            str(out),
            "--no-live-recheck",
            "--tasks",
            "--quiet",
        ]
    )
    assert code == 0
    audit = json.loads((out / "audit.json").read_text(encoding="utf-8"))
    assert audit["run"]["sf_export_manifest"]["state"] == "complete"
    assert (out / "tasks.json").is_file()


def test_doctor_reports_base_config_rate_state(tmp_path, monkeypatch, capsys):
    from seohead.sf import cli as sf_cli
    from seohead.sf.core import spiderconfig

    base = tmp_path / "base.seospiderconfig"
    base.write_bytes(_synthetic_speed_config(flag=1, rate=1.5))
    monkeypatch.setattr(spiderconfig, "find_base_config", lambda *_args: str(base))
    sf_cli._report_base_config({"sf_cli": {"seospiderconfig": str(base)}})
    output = capsys.readouterr().out
    assert "resolved:" in output
    assert "enabled at 1.5 URLs/s (read back from config)" in output

    monkeypatch.setattr(spiderconfig, "find_base_config", lambda *_args: None)
    sf_cli._report_base_config({"sf_cli": {"seospiderconfig": "missing.seospiderconfig"}})
    missing = capsys.readouterr().out
    assert "request-rate limit: NOT VERIFIED" in missing
    assert "Config → Speed" in missing and "seohead sf save-config" in missing
