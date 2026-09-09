"""The render time allowance survives phase boundaries instead of resetting."""
import json
from types import SimpleNamespace

import pytest

from seohead.crawl import render_escalation, sqlite_render
from seohead.crawl.collect import PageRecord
from seohead.crawl.settings import load


class ContextScan:
    def __init__(self, saved=None):
        self.saved = saved

    def read_context(self, kind):
        return self.saved if kind == "render_elapsed" else None

    def write_context(self, items):
        for item in items:
            if item["kind"] == "render_elapsed":
                self.saved = json.loads(item["payload_json"])


def test_render_phase_consumes_saved_allowance(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(sqlite_render.time, "monotonic", lambda: clock[0])
    calls = []

    def escalate(_pages, config, **_kwargs):
        calls.append(config["escalation"]["max_render_seconds"])
        clock[0] += 6
        return render_escalation.EscalationResult(mode="js")

    monkeypatch.setattr(render_escalation, "escalate", escalate)
    settings = load(overrides={"rendering.mode": "js", "rendering.escalation.max_render_seconds": 10})
    scan = ContextScan()
    result = SimpleNamespace(pages=[PageRecord(url="https://example.test/")], links=[])
    sqlite_render.run_render_escalation(scan, result, settings)
    sqlite_render.run_render_escalation(scan, result, settings)
    final = sqlite_render.run_render_escalation(scan, result, settings)
    assert calls == [10, 4]
    assert final.time_budget_exhausted
    assert scan.saved["seconds"] == 12
    assert settings["rendering"]["escalation"]["max_render_seconds"] == 10


def test_killed_render_phase_does_not_get_a_fresh_finite_budget(monkeypatch):
    monkeypatch.setattr(render_escalation, "escalate", lambda *_a, **_k: pytest.fail("unexpected rendering"))
    scan = ContextScan({"schema_version": "render_elapsed.v1", "seconds": 2, "active": True})
    settings = load(overrides={"rendering.mode": "js", "rendering.escalation.max_render_seconds": 10})
    result = SimpleNamespace(pages=[PageRecord(url="https://example.test/")], links=[])
    assert sqlite_render.run_render_escalation(scan, result, settings).time_budget_exhausted


def test_full_policy_renders_eligible_pages_without_sampling():
    records = [SimpleNamespace(url=f"https://example.test/{n}", is_html=True, status_code=200) for n in range(3)]
    records.append(SimpleNamespace(url="https://example.test/failure", is_html=False, status_code=500))
    seen = []
    result = render_escalation.escalate(
        records,
        {"mode": "js", "escalation": {"policy": "full", "max_render_urls": 2}},
        probe=lambda *_: pytest.fail("full policy must not probe"),
        render_fetch=lambda url: seen.append(url) or {"ok": True, "html": "<title>Rendered</title>"},
    )
    assert len(seen) == 2
    assert result.probe_requests == 0
    assert result.render_budget_exhausted
    assert all("failure" not in url for url in seen)
