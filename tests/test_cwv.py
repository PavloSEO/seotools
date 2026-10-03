"""Offline field CWV acceptance across provider, CLI, MCP, audit and report."""

from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.error
from io import BytesIO

import pytest

from seohead import cli
from seohead.audit.site import PAGE_TOOLS, SITE_TOOLS, audit_site
from seohead.data_sources import crux, cwv, pagespeed, providers
from seohead.reports import build_report

URL = "https://example.test/page"
ORIGIN = "https://example.test"
PERIOD = {
    "firstDate": {"year": 2026, "month": 8, "day": 1},
    "lastDate": {"year": 2026, "month": 8, "day": 28},
}


def _record(
    lcp: object = 2500,
    inp: object = 200,
    cls: object = "0.1",
    *,
    url: str | None = URL,
    form_factor: str | None = "PHONE",
) -> dict:
    key = {"url": url} if url else {"origin": ORIGIN}
    if form_factor:
        key["formFactor"] = form_factor
    return {
        "record": {
            "key": key,
            "collectionPeriod": PERIOD,
            "metrics": {
                "largest_contentful_paint": {"percentiles": {"p75": lcp}},
                "interaction_to_next_paint": {"percentiles": {"p75": inp}},
                "cumulative_layout_shift": {"percentiles": {"p75": cls}},
            },
        }
    }


def _query(body: dict, *, url: str | None = URL, form_factor: str | None = "PHONE") -> dict:
    return crux.query(
        url=url,
        origin=None if url else ORIGIN,
        form_factor=form_factor,
        api_key="synthetic",
        fetcher=lambda _payload, _key: json.dumps(body),
    )


@pytest.mark.parametrize(
    ("name", "good", "poor"),
    [
        ("largest_contentful_paint", 2500, 4000),
        ("interaction_to_next_paint", 200, 500),
        ("cumulative_layout_shift", 0.1, 0.25),
    ],
)
def test_official_p75_boundaries(name: str, good: float, poor: float):
    for value, expected in (
        (good, "good"),
        (good + 0.001, "needs_improvement"),
        (poor, "needs_improvement"),
        (poor + 0.001, "poor"),
    ):
        metrics = _record()["record"]["metrics"]
        metrics[name]["percentiles"]["p75"] = value
        result = _query({"record": {**_record()["record"], "metrics": metrics}})
        assert result["assessment"]["metrics"][name]["state"] == expected
        assert result["assessment"]["policy"] == cwv.POLICY


def test_scope_form_factor_period_and_partial_metrics():
    origin = _query(_record(url=None, form_factor=None), url=None, form_factor=None)
    assert origin["assessment"]["target_kind"] == "origin"
    assert origin["assessment"]["form_factor"] == "ALL_FORM_FACTORS"
    assert origin["assessment"]["collection_period"] == {
        "first_date": "2026-08-01",
        "last_date": "2026-08-28",
    }
    assert origin["assessment"]["provider_access"] == "read_only"
    assert origin["assessment"]["cost_mode"] == "free_within_quota"
    assert providers.provider_registry()["providers"]["crux"]["cost_mode"] == "free_within_quota"
    desktop = _query(_record(form_factor="DESKTOP"), form_factor="DESKTOP")
    assert desktop["assessment"]["form_factor"] == "DESKTOP"
    partial_body = _record()
    del partial_body["record"]["metrics"]["interaction_to_next_paint"]
    partial = _query(partial_body)
    assert partial["state"] == "partial"
    assert partial["assessment"]["overall"] == "partial"
    assert partial["assessment"]["missing_metrics"] == ["interaction_to_next_paint"]
    assert partial["assessment"]["metrics"]["largest_contentful_paint"]["state"] == "good"


def test_crux_returned_url_is_the_measured_scope():
    requested = URL + "?campaign=synthetic"
    result = _query(_record(url=URL), url=requested)
    assert result["target"] == requested
    assert result["record_target"] == URL
    assert result["assessment"]["target"] == URL
    assert result["assessment"]["requested_target"] == requested


@pytest.mark.parametrize("bad", [None, "nan", "inf", -1, "n/a", True])
def test_invalid_p75_is_unavailable_not_zero(bad: object):
    result = _query(_record(cls=bad))
    assert result["assessment"]["metrics"]["cumulative_layout_shift"] == {
        "label": "CLS",
        "p75": None,
        "unit": "score",
        "state": "unavailable",
        "reason": "missing_or_invalid_p75",
    }
    assert result["assessment"]["overall"] == "partial"


def test_invalid_period_and_no_field_data_are_unavailable():
    invalid = _record()
    invalid["record"]["collectionPeriod"] = {"firstDate": {"year": 2026, "month": 2, "day": 30}}
    assert _query(invalid)["assessment"]["overall"] == "unavailable"

    def missing(_payload, _key):
        raise urllib.error.HTTPError(URL, 404, "not found", {}, BytesIO(b"{}"))

    absent = crux.query(url=URL, api_key="synthetic", fetcher=missing)
    assert absent["state"] == "no_field_data"
    assert absent["assessment"]["overall"] == "unavailable"
    assert absent["assessment"]["target_kind"] == "url"


def test_missing_credential_and_provider_failure_remain_distinct(monkeypatch, tmp_path):
    from seohead.data_sources import credentials

    monkeypatch.delenv("CRUX_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    unconfigured = crux.query(url=URL, fetcher=lambda *_: pytest.fail("must not call provider"))
    assert unconfigured["assessment"]["overall"] == "unavailable"
    assert (
        unconfigured["assessment"]["metrics"]["largest_contentful_paint"]["reason"]
        == "not_configured"
    )

    def failed(_payload, _key):
        raise urllib.error.HTTPError(URL, 503, "unavailable", {}, BytesIO(b"{}"))

    unavailable = crux.query(url=URL, api_key="synthetic", fetcher=failed)
    assert unavailable["state"] == "failed"
    assert (
        unavailable["assessment"]["metrics"]["largest_contentful_paint"]["reason"]
        == "provider_failed"
    )


def test_psi_lab_cannot_be_assessed_as_field():
    psi = pagespeed._parse(
        {
            "lighthouseResult": {
                "categories": {},
                "audits": {"largest-contentful-paint": {"numericValue": 1000}},
            }
        },
        URL,
        "mobile",
    )
    assert psi["lab_only"] is True
    with pytest.raises(ValueError, match="CrUX current"):
        cwv.assess(psi)


def test_bounded_sample_and_private_cache(tmp_path):
    calls = []

    def fetcher(payload, _key):
        calls.append(payload)
        return json.dumps(_record(url=payload["url"], form_factor=None))

    urls = [URL, URL, ORIGIN + "/second"]
    first = crux.sample_urls(
        urls, max_samples=1, cache_dir=tmp_path, api_key="synthetic", fetcher=fetcher
    )
    assert (first["requested"], first["sampled"], first["omitted"], first["requests"]) == (
        2,
        1,
        1,
        1,
    )
    second = crux.sample_urls(
        urls, max_samples=1, cache_dir=tmp_path, api_key="synthetic", fetcher=fetcher
    )
    assert second["cache_hits"] == 1 and second["requests"] == 0
    assert len(calls) == 1
    assert "synthetic" not in next(tmp_path.glob("crux-*.json")).read_text()
    audit = audit_site(
        ORIGIN, urls=[URL], skip=[*SITE_TOOLS, *PAGE_TOOLS], tools={}, crux_evidence=first
    )
    assert audit["summary"]["field_cwv"]["sampling"]["omitted"] == 1
    assert audit["summary"]["field_cwv"]["state"] == "partial"
    with pytest.raises(ValueError, match="sample budget"):
        crux.sample_urls(urls, max_samples=26, api_key="synthetic", fetcher=fetcher)


def test_saved_sample_rejects_missing_or_duplicate_target_records_before_audit():
    one = _query(_record())
    sample = {
        "provider": "crux",
        "metric_source": "field",
        "state": "complete",
        "requested": 2,
        "sampled": 2,
        "omitted": 0,
        "requests": 2,
        "cache_hits": 0,
        "records": [one],
    }

    def audit():
        return audit_site(
            ORIGIN,
            urls=[URL],
            skip=[*SITE_TOOLS, *PAGE_TOOLS],
            tools={},
            crux_evidence=sample,
        )

    missing = audit()
    assert missing["ok"] is False and "counts" in missing["error"]
    sample["records"] = [one, one]
    duplicated = audit()
    assert duplicated["ok"] is False and "repeats a requested URL" in duplicated["error"]


def test_distinct_requests_collapsing_to_one_crux_record_are_partial():
    urls = [URL + "?campaign=a", URL + "?campaign=b"]
    batch = crux.sample_urls(
        urls,
        api_key="synthetic",
        fetcher=lambda _payload, _key: json.dumps(_record(url=URL, form_factor=None)),
    )
    assert batch["requested"] == batch["sampled"] == 2
    assert batch["duplicate_record_targets"] == 1
    assert batch["state"] == "partial"
    audit = audit_site(
        ORIGIN, urls=[URL], skip=[*SITE_TOOLS, *PAGE_TOOLS], tools={}, crux_evidence=batch
    )
    assert audit["ok"] is True
    assert audit["summary"]["field_cwv"]["state"] == "partial"
    forged = dict(batch, state="complete", duplicate_record_targets=0)
    still_partial = audit_site(
        ORIGIN, urls=[URL], skip=[*SITE_TOOLS, *PAGE_TOOLS], tools={}, crux_evidence=forged
    )
    assert still_partial["summary"]["field_cwv"]["state"] == "partial"
    assert still_partial["summary"]["field_cwv"]["sampling"]["duplicate_record_targets"] == 1


def test_oversized_http_response_is_unavailable_without_unbounded_read(monkeypatch):
    monkeypatch.setattr(crux, "MAX_RESPONSE_BYTES", 80)

    class OversizedResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            assert limit == 81
            return b"x" * 81

    monkeypatch.setattr(crux, "open_no_redirect", lambda *_args, **_kwargs: OversizedResponse())
    result = crux.query(url=URL, api_key="synthetic")
    assert result["state"] == "response_too_large"
    assert result["assessment"]["overall"] == "unavailable"
    assert (
        result["assessment"]["metrics"]["largest_contentful_paint"]["reason"]
        == "response_too_large"
    )
    injected = crux.query(url=URL, api_key="synthetic", fetcher=lambda *_args: "x" * 81)
    assert injected["state"] == "response_too_large"
    history = crux.history(url=URL, api_key="synthetic", fetcher=lambda *_args: "x" * 81)
    assert history["state"] == "response_too_large"


def test_oversized_or_corrupt_cache_is_unavailable_without_provider_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(crux, "MAX_CACHE_BYTES", 80)
    digest = hashlib.sha256(f"{URL}\0{None}".encode()).hexdigest()
    cache = tmp_path / f"crux-{digest}.json"
    cache.write_bytes(b"x" * 81)

    def forbidden_fetch(*_args):
        pytest.fail("an invalid cache must not silently spend a provider request")

    oversized = crux.sample_urls(
        [URL], cache_dir=tmp_path, api_key="synthetic", fetcher=forbidden_fetch
    )
    assert oversized["state"] == "unavailable"
    assert oversized["requests"] == 0 and oversized["cache_hits"] == 0
    assert (
        oversized["records"][0]["assessment"]["metrics"]["largest_contentful_paint"]["reason"]
        == "cache_too_large"
    )
    cache.write_text("{broken", encoding="utf-8")
    corrupt = crux.sample_urls(
        [URL], cache_dir=tmp_path, api_key="synthetic", fetcher=forbidden_fetch
    )
    assert corrupt["state"] == "unavailable"
    assert (
        corrupt["records"][0]["assessment"]["metrics"]["largest_contentful_paint"]["reason"]
        == "cache_invalid"
    )


def test_provider_artifact_retains_assessment_but_public_envelope_is_redacted(tmp_path):
    output = providers.provider_collect(
        "crux",
        "current",
        {"url": URL, "form_factor": "PHONE", "api_key": "synthetic"},
        transport=lambda _payload, _key: json.dumps(_record(lcp=4100)),
        artifact_dir=tmp_path,
    )
    assert output["evidence"]["status"] == "complete"
    assert output["evidence"]["period"] == PERIOD
    assert output["result"] is None
    assert URL not in json.dumps(output)
    saved = json.loads(next(tmp_path.glob("provider-*.json")).read_text())
    assert saved["result"]["assessment"]["metrics"]["largest_contentful_paint"]["state"] == "poor"


def test_audit_and_md_report_use_supplied_field_evidence_without_network(tmp_path):
    evidence = _query(_record(lcp=4100))
    result = audit_site(
        ORIGIN,
        urls=[URL],
        skip=[*SITE_TOOLS, *PAGE_TOOLS],
        tools={},
        crux_evidence=evidence,
    )
    assert result["ok"] is True
    assert result["summary"]["field_cwv"]["state"] == "complete"
    assert result["field_cwv"][0]["overall"] == "poor"
    assert any(f["source"] == "crux_field" and f["url"] == URL for f in result["findings"])
    report = build_report(result, fmt="md", path=str(tmp_path / "audit.md"))
    assert report["ok"] is True
    text = (tmp_path / "audit.md").read_text()
    assert "CrUX field CWV" in text and "4100.0 ms" in text and "2026-08-28" in text
    assert "free_within_quota" in text

    none = audit_site(ORIGIN, urls=[URL], skip=[*SITE_TOOLS, *PAGE_TOOLS], tools={})
    assert none["summary"]["field_cwv"]["state"] == "not_requested"
    assert not any(f["source"] == "crux_field" for f in none["findings"])
    forged = dict(evidence, metric_source="PSI Lighthouse lab")
    refused = audit_site(
        ORIGIN, urls=[URL], skip=[*SITE_TOOLS, *PAGE_TOOLS], tools={}, crux_evidence=forged
    )
    assert refused["ok"] is False


def test_cli_and_mcp_return_same_field_assessment(monkeypatch, capsys):
    monkeypatch.setenv("CRUX_API_KEY", "synthetic")
    monkeypatch.setattr(crux, "_default_fetcher", lambda _payload, _key: json.dumps(_record()))
    assert cli.main(["crux-report", "--url", URL, "--form-factor", "PHONE"]) == 0
    direct = json.loads(capsys.readouterr().out)
    assert direct["assessment"]["overall"] == "good"

    pytest.importorskip("mcp")
    from mcp import types

    from seohead.servers.mcp_server import build_server

    server = build_server()._mcp_server
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(
            name="seo_crux_report", arguments={"url": URL, "form_factor": "PHONE"}
        ),
    )
    response = asyncio.run(server.request_handlers[types.CallToolRequest](request)).root
    assert response.isError is False
    assert response.structuredContent["assessment"]["metrics"] == direct["assessment"]["metrics"]


def test_cli_and_mcp_site_audit_consume_identical_local_evidence(tmp_path, capsys):
    evidence = _query(_record(inp=550))
    source = tmp_path / "crux.json"
    source.write_text(json.dumps(evidence))
    skipped = ",".join([*SITE_TOOLS, *PAGE_TOOLS])
    assert (
        cli.main(
            [
                "site-audit",
                "--url",
                ORIGIN,
                "--urls",
                URL,
                "--skip",
                skipped,
                "--crux-evidence",
                str(source),
            ]
        )
        == 0
    )
    from_cli = json.loads(capsys.readouterr().out)
    assert from_cli["field_cwv"][0]["metrics"]["interaction_to_next_paint"]["state"] == "poor"

    pytest.importorskip("mcp")
    from mcp import types

    from seohead.servers.mcp_server import build_server

    server = build_server()._mcp_server
    request = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(
            name="seo_site_audit",
            arguments={
                "url": ORIGIN,
                "urls": [URL],
                "skip": [*SITE_TOOLS, *PAGE_TOOLS],
                "crux_evidence": evidence,
            },
        ),
    )
    response = asyncio.run(server.request_handlers[types.CallToolRequest](request)).root
    assert response.isError is False
    assert response.structuredContent["field_cwv"] == from_cli["field_cwv"]
    assert response.structuredContent["summary"]["field_cwv"] == from_cli["summary"]["field_cwv"]


def test_cli_rejects_oversized_saved_evidence_before_audit(tmp_path, monkeypatch, capsys):
    source = tmp_path / "oversized-crux.json"
    source.write_bytes(b"x" * 81)
    monkeypatch.setattr(cli, "MAX_CRUX_EVIDENCE_BYTES", 80)
    assert cli.main(["site-audit", "--url", ORIGIN, "--crux-evidence", str(source)]) == 1
    assert "CrUX evidence file exceeds" in capsys.readouterr().err
