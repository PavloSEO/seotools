"""Native rendered-DOM consumer stays bounded and preserves legacy escalation."""

from __future__ import annotations

from dataclasses import dataclass

from seohead.crawl.render_escalation import escalate
from seohead.tools.render import _bounded_dom_script, _safe_policy_facts


@dataclass
class _Page:
    url: str


def _config():
    return {
        "mode": "js",
        "escalation": {"sample_per_pattern": 1, "max_render_urls": 2, "max_render_seconds": 0},
    }


def test_native_consumer_receives_every_result_without_retaining_html():
    consumed = []

    def render(url):
        if url.endswith("/failed"):
            return {"ok": False, "url": url, "error": "browser failed", "renderer": {"engine": "x"}}
        return {
            "ok": True,
            "url": url,
            "final_url": url + "?rendered",
            "html": "<html>" + url + "</html>",
            "renderer": {"engine": "playwright-chromium"},
        }

    def consumer(url, fetched, representation):
        consumed.append((url, fetched.get("html"), representation, fetched.get("ok")))
        return {"accepted": bool(fetched.get("ok")), "state": "stored", "reason": ""}

    result = escalate(
        [_Page("https://example.test/ok"), _Page("https://example.test/failed")],
        _config(),
        probe=lambda _url: {"ok": True, "needs_escalation": True},
        render_fetch=render,
        representation_label="rendered",
        render_consumer=consumer,
    )
    assert {item[0] for item in consumed} == {
        "https://example.test/ok",
        "https://example.test/failed",
    }
    assert result.representations["https://example.test/ok"] == "rendered"
    assert result.representations["https://example.test/failed"] == "static"
    assert "html" not in result.rendered["https://example.test/ok"]
    assert result.rendered["https://example.test/ok"]["capture"] == {
        "accepted": True,
        "state": "stored",
        "reason": "",
    }
    assert result.rendered["https://example.test/failed"]["capture"]["accepted"] is False


def test_legacy_escalation_keeps_existing_html_result_contract():
    result = escalate(
        [_Page("https://example.test/ok")],
        _config(),
        probe=lambda _url: {"ok": True, "needs_escalation": True},
        render_fetch=lambda url: {
            "ok": True,
            "url": url,
            "final_url": url,
            "html": "<html>ok</html>",
        },
        representation_label="rendered",
    )
    assert result.rendered["https://example.test/ok"]["html"] == "<html>ok</html>"


def test_capture_policy_exposes_only_boolean_retention_facts():
    assert _safe_policy_facts(
        {
            "credentials_used": True,
            "cache_control_no_store": True,
            "authorization": "secret",
            "profile_path": "/private/profile",
        }
    ) == {"credentials_used": True, "cache_control_no_store": True}


def test_bounded_dom_script_never_contains_a_prefix_transport_path():
    script = _bounded_dom_script(1024)
    assert "TextEncoder" in script
    assert "bytes > limit" in script
    assert "substring" not in script
