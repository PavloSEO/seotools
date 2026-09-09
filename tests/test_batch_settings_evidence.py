"""Results-affecting evidence configuration reaches extraction and route admission."""

import json

from seohead.crawl.content_evidence import capture
from seohead.crawl.settings import load
from seohead.crawl.sqlite_render import _rendered_batch
from seohead.tools.parser import parse_html


def test_content_and_rule_settings_reach_the_saved_context():
    rule = {
        "id": "present",
        "kind": "presence",
        "selector": "main",
        "operator": "equals",
        "value": "true",
        "max_matches": 1,
    }
    settings = load(
        overrides={
            "evidence.content_area.include_selector": "main",
            "evidence.content_area.root_selector": "body",
            "evidence.content_area.exclude_tags": ["nav"],
            "evidence.content_area.exclude_selectors": [".promotion"],
            "evidence.extraction_rules": [rule],
        }
    )
    html = "<main><nav>Menu</nav><p>Useful text</p><div class='promotion'>Ad</div></main>"
    parsed = parse_html(html, "https://example.test/")
    contexts = capture(
        page_url_id=1,
        source_document_id=1,
        representation="static",
        html=html,
        parsed=parsed,
        settings=settings,
    )
    content = json.loads(
        next(row["payload_json"] for row in contexts if row["kind"] == "content_evidence")
    )
    extracted = json.loads(
        next(row["payload_json"] for row in contexts if row["kind"] == "extraction_rule_evidence")
    )
    assert content["content_tokens"] == 2
    assert extracted["rules"][0]["value"] is True
    assert extracted["rules"][0]["matched"] is True


def test_rendered_route_admission_is_independent_of_storage():
    parsed = parse_html('<a href="/route">Route</a>', "https://example.test/")
    settings = load(
        overrides={"rendering.escalation.policy": "full", "rendering.rendered_links.crawl": False}
    )
    disabled = _rendered_batch(
        parsed, target_url="https://example.test/", depth=0, settings=settings
    )
    settings = load(
        overrides={"rendering.escalation.policy": "full", "rendering.rendered_links.crawl": True}
    )
    enabled = _rendered_batch(
        parsed, target_url="https://example.test/", depth=0, settings=settings
    )
    assert not disabled[5]
    assert enabled[5][0]["frontier_url"] == "https://example.test/route"


def test_resource_time_and_nesting_limits_stop_followup_fetches(tmp_path):
    from seohead.storage.native_scan import NativeScan
    from seohead.storage.resource_graph import capture as capture_resources
    from tests.test_batch_resource_graph import _Response, _store_html, _v2_metadata

    settings = load(
        overrides={
            "resources.fetch": True,
            "storage.format_version": "scan.v2",
            "resources.graph.max_seconds": 1,
            "resources.graph.max_nesting": 0,
            "resources.graph.max_origins": 1,
        }
    )
    calls = []

    class Client:
        def get(self, url, **kwargs):
            calls.append(url)
            return _Response(200, b'@import "child.css";', {"content-type": "text/css"})

    with NativeScan.create(
        tmp_path / "nested.sqlite", format_version="scan.v2", **_v2_metadata()
    ) as scan:
        _store_html(scan, '<link rel="stylesheet" href="/main.css">')
        result = capture_resources(scan, settings, fetcher=Client().get, clock=lambda: 0)
        assert calls == ["https://example.test/main.css"]
        assert result["budget"] >= 1
    calls.clear()
    times = iter([0, 2, 2, 2])
    with NativeScan.create(
        tmp_path / "time.sqlite", format_version="scan.v2", **_v2_metadata()
    ) as scan:
        _store_html(scan, '<script src="/app.js"></script>')
        result = capture_resources(
            scan, settings, fetcher=Client().get, clock=lambda: next(times, 2)
        )
        assert calls == []
        assert result["budget"] == 1
