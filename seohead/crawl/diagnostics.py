"""Offline explanations for unexpectedly small or unfinished native crawls."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

MAX_DECISIONS = 20
MAX_AUDIT_BYTES = 64 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 8 * 1024 * 1024
SAFE_FINISH_REASONS = frozenset(
    {
        "finished",
        "running",
        "url_limit",
        "request_limit",
        "duration_limit",
        "robots_unavailable",
        "errors",
        "interrupted",
        "storage_backpressure",
        "finalization_blocked",
        "offline_reanalysis",
        "operator_requested_stop",
        "capture_finished_no_audit",
    }
)
SAFE_ERROR_KINDS = frozenset({"timeout", "connection", "blocked_redirect", "decoding"})
SAFE_DECISION_REASONS = frozenset(
    {
        "blocked_by_robots",
        "depth_limit",
        "outside_host",
        "excluded_host",
        "excluded_by_pattern",
        "outside_segment",
        "redirect_off_host",
        "url_too_long",
        "query_variants_limit",
        "nofollow",
        "link_observations_limit",
        "form_observations_limit",
    }
)
SAFE_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "text/xml",
        "text/css",
        "application/json",
        "application/xml",
        "application/pdf",
        "application/javascript",
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
    }
)


def _render_failure_kind(reason: str) -> str:
    lowered = reason.lower()
    if any(
        marker in lowered
        for marker in (
            "not installed",
            "missing dependency",
            "browser unavailable",
            "executable doesn't exist",
        )
    ):
        return "missing_dependency"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    return "other"


def _settings(config: dict[str, Any]) -> dict[str, Any]:
    """Only settings needed to explain admission and fetch decisions leave the artifact."""

    def get(path: str) -> Any:
        if path in config:
            return config[path]
        value: Any = config
        for part in path.split("."):
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value

    def scoped(path: str) -> Any:
        value = get(path)
        if isinstance(value, list):
            return {
                "count": len(value),
                "sample": [str(x)[:200] for x in value[:10]],
                "truncated": len(value) > 10,
            }
        return value

    return {
        "user_agent": get("http.user_agent"),
        "robots_token": get("robots.user_agent_token"),
        "robots_policy": get("robots.policy"),
        "scope": {
            k: scoped(f"scope.{k}")
            for k in (
                "internal",
                "include_patterns",
                "exclude_patterns",
                "exclude_hosts",
                "segments_only",
            )
        },
        "limits": {
            k: get(f"limits.{k}")
            for k in (
                "max_urls",
                "max_requests",
                "max_depth",
                "max_crawl_seconds",
                "max_query_variants_per_path",
                "max_url_length",
            )
        },
        "discovery": {
            k: get(f"discovery.{k}")
            for k in ("hyperlinks.crawl", "redirects.crawl", "follow_nofollow")
        },
        "rendering_mode": get("rendering.mode"),
    }


def _scan(path: Path, limit: int) -> dict[str, Any]:
    from seohead.storage import open_scan

    con = open_scan(path, require_audit=False)
    try:
        header = dict(con.execute("SELECT * FROM scan WHERE singleton=1").fetchone())
        config = json.loads(header["config_json"])
        frontier = {
            state: count
            for state, count in con.execute("SELECT state,COUNT(*) FROM frontier GROUP BY state")
        }
        for state in ("queued", "inflight", "done", "excluded"):
            frontier.setdefault(state, 0)
        page_count = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        link_count = con.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        decision_count = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        reason_rows = list(
            con.execute(
                "SELECT reason,COUNT(*) FROM decisions GROUP BY reason ORDER BY COUNT(*) DESC,reason LIMIT 21"
            )
        )
        reasons = dict(reason_rows[:20])
        samples = [
            dict(row)
            for row in con.execute(
                "SELECT decision_id,url,reason,source,depth FROM decisions ORDER BY decision_id LIMIT ?",
                (limit,),
            )
        ]
        type_rows = list(
            con.execute(
                "SELECT content_type,COUNT(*) FROM pages GROUP BY content_type ORDER BY COUNT(*) DESC,content_type LIMIT 11"
            )
        )
        content_types = dict(type_rows[:10])
        error_rows = list(
            con.execute(
                "SELECT error_kind,COUNT(*) FROM pages WHERE error_kind!='' GROUP BY error_kind "
                "ORDER BY COUNT(*) DESC,error_kind LIMIT 11"
            )
        )
        errors = dict(error_rows[:10])
        redirects = con.execute("SELECT COUNT(*) FROM pages WHERE redirect_url!=''").fetchone()[0]
        start = con.execute(
            "SELECT p.status_code,p.content_type,p.outlinks,p.error_kind,p.representation "
            "FROM pages p JOIN urls u USING(url_id) WHERE u.url=?",
            (header["start_url"],),
        ).fetchone()
        runtime = con.execute(
            "SELECT max_depth_reached,elapsed_seconds,throttle_state_json FROM resume_state WHERE singleton=1"
        ).fetchone()
        render_rows = con.execute(
            "SELECT payload_json FROM context_items WHERE kind='render_phase_summary' ORDER BY item_key"
        )
        render = {
            "render_requests": 0,
            "unprobed_patterns": 0,
            "budget_exhausted": False,
            "failure_reasons": {},
        }
        for row in render_rows:
            payload = json.loads(row[0])
            render["render_requests"] += payload.get("render_requests", 0)
            render["unprobed_patterns"] += len(payload.get("patterns_unprobed", []))
            render["budget_exhausted"] |= bool(
                payload.get("render_budget_exhausted") or payload.get("time_budget_exhausted")
            )
            for reason in payload.get("patterns_unprobed_reasons", {}).values():
                kind = _render_failure_kind(str(reason))
                render["failure_reasons"][kind] = render["failure_reasons"].get(kind, 0) + 1
        audit_row = con.execute("SELECT document_json FROM audit WHERE singleton=1").fetchone()
        audit_run = json.loads(audit_row[0]).get("run", {}) if audit_row else {}
        event_table = (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scan_events'"
            ).fetchone()
            is not None
        )
        provider_failures = None
        if event_table:
            provider_failures = sum(
                json.loads(row[0]).get("state")
                in {"failed", "error", "unavailable", "timeout", "blocked"}
                for row in con.execute(
                    "SELECT payload_json FROM scan_events WHERE event_type='provider_enrichment'"
                )
            )
        return {
            "source": {
                "kind": "scan",
                "source_kind": header["source_kind"],
                "scan_uuid": header["scan_uuid"],
                "format_version": header["format_version"],
                "path": str(path),
                "lifecycle": header["lifecycle"],
                "finish_reason": header["finish_reason"],
                "crawl_partial": bool(header["crawl_partial"]),
                "start_url": header["start_url"],
            },
            "settings": _settings(config),
            "observed": {
                "page_records": page_count,
                "links_recorded": link_count,
                "decisions_recorded": decision_count,
                "site_total_urls": None,
                "frontier": frontier if header["source_kind"] == "native" else None,
                "content_types": content_types,
                "content_types_truncated": len(type_rows) > 10,
                "page_errors": errors,
                "page_error_groups_truncated": len(error_rows) > 10,
                "provider_failures": provider_failures,
                "redirects_recorded": redirects,
                "max_depth_reached": runtime[0] if runtime is not None else None,
                "elapsed_seconds": runtime[1] if runtime is not None else None,
                "requests_used": json.loads(runtime[2]).get("requests_used")
                if runtime is not None
                else None,
                "start_page": dict(start) if start is not None else None,
                "render": render,
                "requires_rendering": audit_run.get("requires_rendering"),
            },
            "decisions": {
                "by_reason": reasons,
                "sample": samples,
                "omitted_from_sample": max(0, decision_count - len(samples)),
                "reason_groups_truncated": len(reason_rows) > 20,
            },
            "limitations_count": len(json.loads(header["limitations_json"])),
            "coverage": {
                "pages": "recorded",
                "links": "partial" if header["crawl_partial"] else "recorded",
                "decisions": "recorded",
                "frontier": "recorded"
                if header["source_kind"] == "native"
                else "historical"
                if header["source_kind"] == "reanalysis"
                else "unavailable",
                "audit": "recorded" if audit_row else "unavailable",
                "provider_events": "recorded" if event_table else "unavailable",
                "render": "recorded"
                if render["render_requests"] or render["unprobed_patterns"]
                else "not_requested"
                if _settings(config)["rendering_mode"] == "raw"
                else "not_recorded",
            },
        }
    finally:
        con.close()


def _jsonl(
    path: Path, limit: int, *, decision: bool
) -> tuple[int, list[dict[str, Any]], Counter[str]]:
    total = 0
    sample: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    if not path.is_file():
        return 0, sample, counts
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_JSONL_LINE_BYTES:
                raise ValueError(f"{path.name}:{line_number} exceeds the 8 MiB line budget")
            try:
                value = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path.name}:{line_number} is invalid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path.name}:{line_number} must be an object")
            total += 1
            if decision:
                reason = value.get("reason")
                if isinstance(reason, str):
                    counts[reason] += 1
                if len(sample) < limit:
                    sample.append(
                        {
                            "decision_id": total,
                            **{key: value.get(key) for key in ("url", "reason", "source", "depth")},
                        }
                    )
            elif len(sample) < limit:
                sample.append(value)
    return total, sample, counts


def _legacy_pages(path: Path, start_url: str | None) -> dict[str, Any]:
    total, redirects = 0, 0
    first = None
    content_types: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    if not path.is_file():
        return {
            "total": None,
            "start": None,
            "content_types": {},
            "errors": {},
            "redirects": None,
            "types_truncated": False,
            "errors_truncated": False,
        }
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_JSONL_LINE_BYTES:
                raise ValueError(f"{path.name}:{line_number} exceeds the 8 MiB line budget")
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{path.name}:{line_number} is invalid JSON") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path.name}:{line_number} must be an object")
            total += 1
            selected = {
                k: row.get(k)
                for k in ("status_code", "content_type", "outlinks", "error_kind", "representation")
            }
            if row.get("url") == start_url:
                first = selected
            content_types[str(row.get("content_type") or "unknown")] += 1
            if row.get("error_kind"):
                errors[str(row["error_kind"])] += 1
            redirects += bool(row.get("redirect_url"))
    return {
        "total": total,
        "start": first,
        "content_types": dict(content_types.most_common(10)),
        "errors": dict(errors.most_common(10)),
        "redirects": redirects,
        "types_truncated": len(content_types) > 10,
        "errors_truncated": len(errors) > 10,
    }


def _render_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    unprobed = value.get("patterns_unprobed")
    failure_reasons = value.get("patterns_unprobed_reasons")
    return {
        "render_requests": value.get("render_requests"),
        "unprobed_patterns": len(unprobed) if isinstance(unprobed, list) else None,
        "budget_exhausted": bool(
            value.get("render_budget_exhausted") or value.get("time_budget_exhausted")
        ),
        "failure_reasons": dict(
            Counter(
                _render_failure_kind(str(reason))
                for reason in (
                    failure_reasons.values() if isinstance(failure_reasons, dict) else ()
                )
            )
        ),
    }


def _run(path: Path, limit: int) -> dict[str, Any]:
    audit_path = path / "audit.json"
    if not path.is_dir() or not audit_path.is_file():
        raise ValueError("run must contain audit.json; unfinished legacy runs lack enough context")
    if audit_path.stat().st_size > MAX_AUDIT_BYTES:
        raise ValueError("audit.json exceeds the 64 MiB diagnostic read budget")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    run = audit.get("run") if isinstance(audit, dict) else None
    if not isinstance(run, dict):
        raise ValueError("audit.json has no run object")
    if run.get("input_mode") not in ("crawl", "crawl-list"):
        raise ValueError("run is not a native crawl audit")
    pages = _legacy_pages(path / "pages.jsonl", run.get("source"))
    decisions_count, decisions, reasons = _jsonl(path / "decisions.jsonl", limit, decision=True)
    links_count = (
        _jsonl(path / "links.jsonl", 0, decision=False)[0]
        if (path / "links.jsonl").is_file()
        else None
    )
    settings = run.get("crawl_config") or {}
    if not isinstance(settings, dict):
        raise ValueError("audit.json crawl_config must be an object")
    return {
        "source": {
            "kind": "run",
            "source_kind": "legacy_directory",
            "path": str(path),
            "lifecycle": "finished"
            if run.get("crawl_finish_reason") == "finished"
            else "interrupted",
            "finish_reason": run.get("crawl_finish_reason"),
            "crawl_partial": run.get("crawl_partial"),
            "start_url": run.get("source"),
        },
        "settings": _settings(settings),
        "observed": {
            "page_records": pages["total"],
            "links_recorded": links_count,
            "decisions_recorded": decisions_count if (path / "decisions.jsonl").is_file() else None,
            "site_total_urls": None,
            "frontier": None,
            "content_types": pages["content_types"],
            "content_types_truncated": pages["types_truncated"],
            "page_errors": pages["errors"],
            "page_error_groups_truncated": pages["errors_truncated"],
            "provider_failures": None,
            "redirects_recorded": pages["redirects"],
            "max_depth_reached": None,
            "elapsed_seconds": None,
            "requests_used": None,
            "start_page": pages["start"],
            "render": _render_summary(run.get("render_escalation")),
            "requires_rendering": run.get("requires_rendering"),
        },
        "decisions": {
            "by_reason": dict(reasons.most_common(20)),
            "sample": decisions,
            "omitted_from_sample": max(0, decisions_count - len(decisions)),
            "reason_groups_truncated": len(reasons) > 20,
        },
        "limitations_count": len(run.get("checks_skipped", [])),
        "coverage": {
            "pages": "recorded" if (path / "pages.jsonl").is_file() else "unavailable",
            "links": "partial"
            if run.get("crawl_partial") is True
            else "recorded"
            if (path / "links.jsonl").is_file()
            else "unavailable",
            "decisions": "recorded" if (path / "decisions.jsonl").is_file() else "unavailable",
            "frontier": "unavailable",
            "audit": "recorded",
            "provider_events": "unavailable",
            "render": "recorded" if run.get("render_escalation") else "unknown",
        },
    }


def _explain(result: dict[str, Any]) -> list[dict[str, Any]]:
    source, obs, decisions = result["source"], result["observed"], result["decisions"]
    coverage = result["coverage"]
    reasons = decisions["by_reason"]
    findings = []

    def add(code: str, conclusion: str, evidence: list[str], next_step: str) -> None:
        findings.append(
            {"code": code, "conclusion": conclusion, "evidence": evidence, "next_step": next_step}
        )

    complete_discovery = (
        source.get("crawl_partial") is False
        and coverage["pages"] == "recorded"
        and coverage["links"] == "recorded"
        and coverage["decisions"] == "recorded"
    )
    if not complete_discovery:
        add(
            "coverage_gap",
            "Discovery or decision evidence is partial or unavailable; zero observed links cannot prove the crawl exhausted the site's routes.",
            ["source.crawl_partial", "coverage.pages", "coverage.links", "coverage.decisions"],
            "Inspect the missing or omitted evidence before treating a one-page result as complete.",
        )

    if source["lifecycle"] == "running":
        add(
            "worker_state_unverified",
            "The artifact still says running; a saved file cannot prove a worker is alive.",
            ["source.lifecycle", "observed.frontier"],
            "Check the process and scan status before resuming or requeueing.",
        )
    if source["lifecycle"] in {"failed", "interrupted"} and source["finish_reason"] not in {
        "url_limit",
        "request_limit",
        "duration_limit",
        "robots_unavailable",
    }:
        add(
            "worker_or_run_failure",
            "The retained run ended without a completed crawl; its exact cause needs the recorded failure evidence.",
            ["source.lifecycle", "source.finish_reason", "observed.page_errors"],
            "Inspect the failed URL and process records before a focused retry.",
        )
    if source["finish_reason"] in {"url_limit", "request_limit", "duration_limit"}:
        add(
            "budget_exhausted",
            f"The recorded finish reason is {source['finish_reason']}.",
            ["source.finish_reason", "settings.limits"],
            "Review the explicit crawl budget and scope before another run.",
        )
    if source["finish_reason"] == "robots_unavailable":
        add(
            "robots_unavailable",
            "The run stopped because robots evidence was unavailable.",
            ["source.finish_reason", "settings.robots_policy"],
            "Check robots.txt reachability under the recorded user-agent; do not bypass it silently.",
        )
    robots = sum(count for reason, count in reasons.items() if "robots" in reason)
    if robots:
        add(
            "robots_exclusion",
            f"{robots} recorded decisions mention robots policy.",
            ["decisions.by_reason", "settings.robots_policy"],
            "Inspect the cited rules and user-agent token; do not bypass robots silently.",
        )
    scope = sum(
        count
        for reason, count in reasons.items()
        if any(word in reason for word in ("scope", "host", "segment", "include", "exclude"))
    )
    if scope:
        add(
            "scope_exclusion",
            f"{scope} recorded decisions exclude discovered URLs by scope.",
            ["decisions.by_reason", "settings.scope"],
            "Inspect the excluded URL samples and change the named scope setting explicitly if intended.",
        )
    if any("depth" in reason for reason in reasons):
        add(
            "depth_exclusion",
            "Recorded decisions stopped discovery at a depth limit.",
            ["decisions.by_reason", "settings.limits.max_depth"],
            "Review max_depth and the excluded decision samples.",
        )
    if any("redirect" in reason for reason in reasons) or obs["redirects_recorded"]:
        add(
            "redirect_discovery",
            "Redirect outcomes are present in retained evidence.",
            ["observed.redirects_recorded", "decisions.by_reason", "settings.discovery.redirects"],
            "Inspect redirect targets and the explicit discovery.redirects.crawl setting.",
        )
    if obs["page_errors"]:
        add(
            "fetch_failure",
            "Page fetch failures were recorded.",
            ["observed.page_errors"],
            "Inspect the failed URL records and retry only after the transport or provider is available.",
        )
    if obs.get("provider_failures"):
        add(
            "provider_failure",
            "The saved event timeline records failed provider operations.",
            ["observed.provider_failures", "coverage.provider_events"],
            "Inspect the provider event and dependency status before retrying enrichment.",
        )
    first = obs["start_page"] or {}
    if first.get("content_type") and not str(first["content_type"]).startswith("text/html"):
        add(
            "non_html_start",
            "The start response was not recorded as HTML.",
            ["observed.start_page.content_type"],
            "Check the target URL and Content-Type before expecting link discovery.",
        )
    if obs.get("requires_rendering") is True:
        add(
            "render_eligibility",
            "The saved audit marked the start page as requiring rendering.",
            ["observed.requires_rendering", "settings.rendering_mode"],
            "Run a focused render-check or explicitly configure rendering after verifying the page.",
        )
    render = obs.get("render") or {}
    if isinstance(render, dict) and (
        render.get("unprobed_patterns") or render.get("patterns_unprobed")
    ):
        add(
            "render_unavailable",
            "Some rendering probes produced no verdict.",
            ["observed.render"],
            "Check browser availability and the recorded probe failure; do not treat raw-only pages as rendered.",
        )
    if isinstance(render, dict) and render.get("failure_reasons", {}).get("missing_dependency"):
        add(
            "dependency_unavailable",
            "A rendering dependency was recorded as unavailable.",
            ["observed.render.failure_reasons"],
            "Install or restore the optional local browser dependency before a focused render retry.",
        )
    if isinstance(render, dict) and (
        render.get("budget_exhausted")
        or render.get("render_budget_exhausted")
        or render.get("time_budget_exhausted")
    ):
        add(
            "render_budget",
            "The recorded render budget was exhausted.",
            ["observed.render"],
            "Review rendering.escalation limits before a focused diagnostic run.",
        )
    if (
        obs["page_records"] == 1
        and source["finish_reason"] == "finished"
        and first.get("outlinks") == 0
        and not reasons
        and not obs["page_errors"]
        and complete_discovery
    ):
        add(
            "complete_one_page",
            "One page finished with no recorded internal outlinks; site-wide URL total is unknown.",
            ["observed.start_page.outlinks", "source.finish_reason", "decisions.by_reason"],
            "Inspect source links or run a focused render-check if client-side navigation is expected.",
        )
    elif obs["page_records"] == 1 and first.get("outlinks") == 0:
        add(
            "no_discovery",
            "The fetched start page recorded no internal outlinks.",
            ["observed.start_page.outlinks", "source.finish_reason"],
            "Inspect eligible anchors, scope decisions and a focused render-check if navigation is client-side.",
        )
    if not findings:
        add(
            "insufficient_evidence",
            "No recorded decision uniquely explains the low progress.",
            ["coverage", "observed"],
            "Inspect the start page and exact decision samples; avoid changing crawler policy without evidence.",
        )
    return findings


def diagnose(
    *,
    scan: str | None = None,
    run: str | None = None,
    max_decisions: int = MAX_DECISIONS,
    export: str | None = None,
) -> dict[str, Any]:
    """Summarize stored evidence only; no URL is fetched and no crawl is resumed."""
    if bool(scan) == bool(run):
        raise ValueError("provide exactly one of scan or run")
    if type(max_decisions) is not int or not 1 <= max_decisions <= MAX_DECISIONS:
        raise ValueError(f"max_decisions must be 1..{MAX_DECISIONS}")
    result = _scan(Path(scan), max_decisions) if scan else _run(Path(run), max_decisions)
    result["schema_version"] = "crawl_diagnostics.v1"
    result["diagnoses"] = _explain(result)
    if export is not None:
        redacted = json.loads(json.dumps(result))
        source = redacted["source"]
        for key in ("path", "start_url", "scan_uuid"):
            if key in source:
                source[key] = "[redacted]"
        if source.get("finish_reason") is not None and (
            not isinstance(source["finish_reason"], str)
            or source["finish_reason"] not in SAFE_FINISH_REASONS
        ):
            source["finish_reason"] = "[redacted]"
        if type(source.get("crawl_partial")) is not bool:
            source["crawl_partial"] = None
        settings = redacted["settings"]
        for key in ("user_agent", "robots_token", "scope"):
            settings[key] = "[redacted]"
        if settings.get("robots_policy") not in ("respect", "report_only", "ignore"):
            settings["robots_policy"] = "[redacted]"
        if settings.get("rendering_mode") not in ("raw", "js", "legacy_fragment"):
            settings["rendering_mode"] = "[redacted]"
        settings["limits"] = {
            key: value if type(value) is int and value >= 0 else None
            for key, value in settings["limits"].items()
        }
        settings["discovery"] = {
            key: value if type(value) is bool else None
            for key, value in settings["discovery"].items()
        }
        observed = redacted["observed"]
        if type(observed.get("requires_rendering")) is not bool:
            observed["requires_rendering"] = None

        def safe_groups(groups: dict[str, int], allowed: frozenset[str]) -> dict[str, int]:
            redacted_groups: Counter[str] = Counter()
            for key, count in groups.items():
                redacted_groups[key if key in allowed else "[redacted]"] += count
            return dict(redacted_groups)

        media_groups: Counter[str] = Counter()
        for key, count in observed["content_types"].items():
            media = key.split(";", 1)[0].strip().lower()
            media_groups[media if media in SAFE_MEDIA_TYPES else "[redacted]"] += count
        observed["content_types"] = dict(media_groups)
        observed["page_errors"] = safe_groups(observed["page_errors"], SAFE_ERROR_KINDS)
        if observed["start_page"]:
            observed["start_page"]["content_type"] = "[redacted]"
            observed["start_page"]["error_kind"] = "[redacted]"
            for key in ("status_code", "outlinks"):
                value = observed["start_page"].get(key)
                observed["start_page"][key] = value if type(value) is int and value >= 0 else None
            if observed["start_page"].get("representation") not in (
                "static",
                "rendered",
                "legacy_fragment",
            ):
                observed["start_page"]["representation"] = "[redacted]"
        if isinstance(observed.get("render"), dict):
            render = observed["render"]
            for key in ("render_requests", "unprobed_patterns"):
                value = render.get(key)
                render[key] = value if type(value) is int and value >= 0 else None
            render["budget_exhausted"] = render.get("budget_exhausted") is True
            render["failure_reasons"] = safe_groups(
                render.get("failure_reasons") or {},
                frozenset({"missing_dependency", "timeout", "other"}),
            )
        redacted["decisions"]["by_reason"] = safe_groups(
            redacted["decisions"]["by_reason"], SAFE_DECISION_REASONS
        )
        for item in redacted["decisions"]["sample"]:
            item["url"] = "[redacted]"
            item["source"] = "[redacted]"
            if not isinstance(item["reason"], str) or item["reason"] not in SAFE_DECISION_REASONS:
                item["reason"] = "[redacted]"
            if type(item["depth"]) is not int or item["depth"] < 0:
                item["depth"] = None
        path = Path(export)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(redacted, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        result["redacted_export"] = str(path)
    return result
