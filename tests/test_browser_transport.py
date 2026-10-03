"""Offline remote Playwright transport selection and failure boundaries (#808)."""

from __future__ import annotations

import asyncio

import pytest

from seohead import cli
from seohead.crawl import settings
from seohead.servers import handlers
from seohead.servers.mcp_server import build_server
from seohead.tools import browser_transport, render
from tests.test_render_check_identity import _install_stack
from tests.test_render_document import _rendering_config

pytest_plugins = ("tests.test_render_document",)


def _remote(monkeypatch, endpoint="wss://browser.example.test/playwright?token=synthetic"):
    monkeypatch.setattr(browser_transport, "_local_version", lambda: "1.55.1")
    monkeypatch.setenv("SYNTHETIC_PLAYWRIGHT_WS", endpoint)
    return {
        "transport": "remote",
        "remote_protocol": "playwright",
        "remote_endpoint_env": "SYNTHETIC_PLAYWRIGHT_WS",
        "remote_playwright_version": "1.55.0",
    }


def test_local_default_does_not_resolve_an_endpoint():
    endpoint, facts = browser_transport.prepare(None)
    assert endpoint is None and facts == {"mode": "local"}


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"remote_protocol": "cdp"}, "invalid_browser_transport"),
        ({"remote_playwright_version": "1.54.9"}, "remote_version_incompatible"),
    ],
)
def test_unsupported_protocol_or_version_refuses_before_connection(monkeypatch, change, code):
    config = {**_remote(monkeypatch), **change}
    with pytest.raises(browser_transport.BrowserTransportError) as caught:
        browser_transport.prepare(config)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("endpoint", "code"),
    [
        ("https://browser.example.test/playwright", "invalid_remote_endpoint"),
        ("ws://browser.example.test/playwright", "insecure_remote_endpoint"),
        ("wss://user:secret@browser.example.test/playwright", "invalid_remote_endpoint"),
    ],
)
def test_endpoint_validation_never_echoes_the_value(monkeypatch, endpoint, code):
    config = _remote(monkeypatch, endpoint)
    with pytest.raises(browser_transport.BrowserTransportError) as caught:
        browser_transport.prepare(config)
    assert caught.value.code == code
    assert endpoint not in str(caught.value) and "secret" not in str(caught.value)


def test_crawl_settings_require_explicit_remote_configuration():
    with pytest.raises(settings.ConfigError, match="remote_endpoint_env"):
        settings.load(overrides={"rendering.browser.transport": "remote"})
    with pytest.raises(settings.ConfigError, match="protocol"):
        settings.load(overrides={"rendering.browser.remote_protocol": "cdp"})
    with pytest.raises(settings.ConfigError, match="transport=remote"):
        settings.load(overrides={"rendering.browser.remote_endpoint_env": "SYNTHETIC_WS"})
    selected = settings.load(
        overrides={
            "rendering.browser.transport": "remote",
            "rendering.browser.remote_endpoint_env": "SYNTHETIC_WS",
            "rendering.browser.remote_playwright_version": "1.55.0",
        }
    )
    manifest = settings.manifest(selected)
    assert manifest["rendering.browser.transport"] == "remote"
    assert manifest["rendering.browser.remote_endpoint_env"] == "SYNTHETIC_WS"
    assert "wss://" not in str(manifest)


def test_cli_and_shared_handler_forward_the_same_remote_selection(monkeypatch):
    args = cli.build_parser().parse_args(
        [
            "render-check",
            "--url",
            "https://example.test/",
            "--browser-transport",
            "remote",
            "--remote-endpoint-env",
            "SYNTHETIC_PLAYWRIGHT_WS",
            "--remote-playwright-version",
            "1.55.0",
        ]
    )
    _name, kwargs = cli._build_kwargs("render-check", args)
    assert kwargs["transport_config"] == {
        "transport": "remote",
        "remote_protocol": "playwright",
        "remote_endpoint_env": "SYNTHETIC_PLAYWRIGHT_WS",
        "remote_playwright_version": "1.55.0",
    }
    received = []
    monkeypatch.setattr(
        render,
        "render_check",
        lambda url, **options: received.append((url, options)) or {"ok": True},
    )
    assert handlers.render_check(**kwargs) == {"ok": True}
    assert received[0][1]["transport_config"] == kwargs["transport_config"]


def test_mcp_forwards_remote_selection_without_changing_local_default(monkeypatch):
    calls = []
    monkeypatch.setattr(
        handlers, "render_check", lambda **kwargs: calls.append(kwargs) or {"ok": True}
    )
    tool = build_server()._tool_manager.get_tool("seo_render_check")
    assert asyncio.run(tool.run({"url": "https://example.test/"})) == {"ok": True}
    assert "transport_config" not in calls[-1]
    transport = {
        "transport": "remote",
        "remote_protocol": "playwright",
        "remote_endpoint_env": "SYNTHETIC_PLAYWRIGHT_WS",
        "remote_playwright_version": "1.55.0",
    }
    assert asyncio.run(
        tool.run({"url": "https://example.test/", "transport_config": transport})
    ) == {"ok": True}
    assert calls[-1]["transport_config"] == transport


def test_remote_preflight_rejects_missing_endpoint_before_raw_fetch(monkeypatch):
    raw = "<html><body>synthetic</body></html>"
    stack = _install_stack(monkeypatch, raw, raw)
    config = _remote(monkeypatch)
    monkeypatch.delenv("SYNTHETIC_PLAYWRIGHT_WS")
    result = render.render_check("https://example.com/", transport_config=config)
    assert result["reason"] == "invalid_remote_endpoint"
    assert stack["http_calls"] == []
    assert stack["chromium"].launch_calls == []


def test_remote_document_connects_without_launch_and_keeps_pinned_routes(monkeypatch, fake_stack):
    config = _remote(monkeypatch)
    monkeypatch.setenv("SEOHEAD_CHROME", "/missing/local/chrome")
    calls = []

    def connect(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return fake_stack["browser"]

    monkeypatch.setattr(fake_stack["chromium"], "connect", connect, raising=False)
    result = render.render_document("https://example.com/", _rendering_config(**config))
    assert result["ok"] is True
    assert result["renderer"]["transport"]["mode"] == "remote"
    assert result["renderer"]["transport"]["expected_remote_version"] == "1.55.0"
    assert calls == [("wss://browser.example.test/playwright?token=synthetic", {"timeout": 30000})]
    assert fake_stack["chromium"].launch_calls == []
    assert fake_stack["context"].new_page_route_snapshots[0]
    assert fake_stack["context"].ws_routes
    assert fake_stack["context"].closed and fake_stack["browser"].closed
    assert "synthetic" not in str(result)


def test_remote_render_check_uses_same_transport_and_never_falls_back(monkeypatch):
    raw = "<html><head><title>Test</title></head><body>" + "word " * 80 + "</body></html>"
    stack = _install_stack(monkeypatch, raw, raw + "<p>rendered</p>")
    config = _remote(monkeypatch)
    calls = []

    def connect(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return stack["browser"]

    monkeypatch.setattr(stack["chromium"], "connect", connect, raising=False)
    result = render.render_check("https://example.com/", transport_config=config)
    assert result["ok"] is True
    assert result["browser_transport"]["mode"] == "remote"
    assert len(calls) == 1 and stack["chromium"].launch_calls == []


def test_remote_probe_missing_route_capability_is_unavailable(monkeypatch):
    raw = "<html><body>synthetic</body></html>"
    stack = _install_stack(monkeypatch, raw, raw)
    config = _remote(monkeypatch)
    monkeypatch.setattr(
        stack["chromium"], "connect", lambda _endpoint, **_kwargs: stack["browser"], raising=False
    )
    monkeypatch.setattr(stack["context"], "route_web_socket", None)
    result = render.render_check("https://example.com/", transport_config=config)
    assert result["ok"] is False
    assert result["reason"] == "remote_capability_unsupported"
    assert not stack["context"].new_page_route_snapshots
    assert stack["chromium"].launch_calls == []


def test_remote_rendered_html_connects_without_local_launch(monkeypatch):
    raw = "<html><body>synthetic</body></html>"
    stack = _install_stack(monkeypatch, raw, raw)
    config = _remote(monkeypatch)
    monkeypatch.setattr(
        stack["chromium"], "connect", lambda _endpoint, **_kwargs: stack["browser"], raising=False
    )
    result = render.rendered_html("https://example.com/", transport_config=config)
    assert result["ok"] is True
    assert result["browser_transport"]["mode"] == "remote"
    assert stack["chromium"].launch_calls == []


def test_remote_connection_error_is_actionable_and_redacted(monkeypatch, fake_stack):
    config = _remote(monkeypatch)

    def fail_connect(_endpoint, **_kwargs):
        raise RuntimeError("wss://browser.example.test/playwright?token=synthetic rejected")

    monkeypatch.setattr(fake_stack["chromium"], "connect", fail_connect, raising=False)
    result = render.render_document("https://example.com/", _rendering_config(**config))
    assert result["ok"] is False
    assert result["reason"] == "remote_connection_failed"
    assert "synthetic" not in str(result)
    assert fake_stack["chromium"].launch_calls == []


def test_remote_missing_pinned_route_capability_fails_before_page(monkeypatch, fake_stack):
    config = _remote(monkeypatch)
    monkeypatch.setattr(
        fake_stack["chromium"],
        "connect",
        lambda _endpoint, **_kwargs: fake_stack["browser"],
        raising=False,
    )
    monkeypatch.setattr(fake_stack["context"], "route_web_socket", None)
    result = render.render_document("https://example.com/", _rendering_config(**config))
    assert result["reason"] == "remote_capability_unsupported"
    assert not fake_stack["context"].new_page_route_snapshots
    assert fake_stack["browser"].closed
