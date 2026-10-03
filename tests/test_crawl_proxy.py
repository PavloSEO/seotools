"""Offline proxy-policy tests: the fake proxy is the only reachable socket."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
from unittest.mock import patch

import httpx
import pytest

from seohead.crawl import settings
from seohead.recon import net
from seohead.runlog import safe_arguments


def _answers(real, *, target="93.184.216.34"):
    def lookup(host, port, *args, **kwargs):
        if host == "example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (target, port))]
        return real(host, port, *args, **kwargs)

    return lookup


def test_proxy_config_requires_explicit_safe_reference_and_never_records_auth(monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(net.socket, "getaddrinfo", _answers(real))
    assert settings.manifest(settings.load())["http.proxy"]["mode"] == "direct"
    monkeypatch.setenv("SEOHEAD_TEST_PROXY", "http://user:password@example.test:3128")
    resolved = settings.load(overrides={"http.proxy": "env:SEOHEAD_TEST_PROXY"})
    route = settings.resolve_proxy(resolved)
    assert route.identity == "http://example.test:3128"
    assert route.authenticated
    manifest = settings.manifest(resolved)
    serialized = json.dumps(manifest)
    assert manifest["http.proxy"] == {
        "mode": "proxy",
        "endpoint": "http://example.test:3128",
        "authenticated": True,
    }
    assert manifest["http.proxy_identity"] == "http://example.test:3128"
    assert manifest["http.proxy_authenticated"] is True
    assert "password" not in serialized
    assert "user:password" not in serialized
    assert settings.fingerprint(resolved) != settings.fingerprint(settings.load())
    assert settings.parse_setting_assignment("http.proxy=env:SEOHEAD_TEST_PROXY") == (
        "http.proxy",
        "env:SEOHEAD_TEST_PROXY",
    )
    with pytest.raises(settings.ConfigError, match=r"cache\.mode=off"):
        settings.load(overrides={"http.proxy": "env:SEOHEAD_TEST_PROXY", "cache.mode": "live"})


@pytest.mark.parametrize(
    "value,reason",
    [
        ("http://user:secret@example.test:3128", "inline authentication"),
        ("https://example.test:3128", "only an http"),
        ("socks5h://example.test:3128", "only an http"),
        ("http://example.test", "explicit port"),
        ("http://example.test:3128/path", "path"),
        ("env:MISSING_PROXY", "missing or empty"),
    ],
)
def test_unsupported_proxy_is_refused_before_network(monkeypatch, value, reason):
    monkeypatch.delenv("MISSING_PROXY", raising=False)
    with pytest.raises(settings.ConfigError, match=reason) as error:
        settings.load(overrides={"http.proxy": value})
    assert "secret" not in str(error.value)


def test_proxy_auth_percent_encoding_is_decoded_and_ambiguous_usernames_rejected(monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(net.socket, "getaddrinfo", _answers(real))
    monkeypatch.setenv("SEOHEAD_TEST_PROXY", "http://user%40team:p%3Aq@example.test:3128")
    route = net.resolve_proxy_route("env:SEOHEAD_TEST_PROXY")
    assert route.authenticated
    assert route.proxy.auth == ("user@team", "p:q")
    monkeypatch.setenv("SEOHEAD_TEST_PROXY", "http://user%3Ateam:p@example.test:3128")
    with pytest.raises(ValueError, match="username cannot contain a colon"):
        net.resolve_proxy_route("env:SEOHEAD_TEST_PROXY")


def test_private_proxy_opt_in_does_not_allow_private_target(monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(net.socket, "getaddrinfo", _answers(real, target="127.0.0.1"))
    with pytest.raises(ValueError, match="proxy_allow_private"):
        net.resolve_proxy_route("http://127.0.0.1:3128")
    route = net.resolve_proxy_route("http://127.0.0.1:3128", allow_private=True)
    assert route.identity == "http://127.0.0.1:3128"
    with pytest.raises(ValueError, match="private"):
        net.validate_url("https://example.test/private")


def test_mixed_proxy_dns_answers_are_refused_before_connection(monkeypatch):
    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda host, port, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ],
    )
    with pytest.raises(ValueError, match="private address"):
        net.resolve_proxy_route("http://proxy.example.test:3128")


def test_http_and_https_connect_use_only_vetted_target_and_proxy_ips(monkeypatch):
    seen = []

    class Proxy(socketserver.StreamRequestHandler):
        def handle(self):
            line = self.rfile.readline().decode().strip()
            headers = []
            while True:
                item = self.rfile.readline().decode().strip()
                if not item:
                    break
                headers.append(item)
            seen.append((line, headers))
            if line.startswith("CONNECT "):
                self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            else:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

    with socketserver.TCPServer(("127.0.0.1", 0), Proxy) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real = socket.getaddrinfo
        monkeypatch.setattr(net.socket, "getaddrinfo", _answers(real))
        monkeypatch.setenv(
            "SEOHEAD_TEST_PROXY",
            f"http://user:password@127.0.0.1:{server.server_address[1]}",
        )
        route = net.resolve_proxy_route("env:SEOHEAD_TEST_PROXY", allow_private=True)
        try:
            for scheme in ("http", "https"):
                client, _ = net.http_client(2.0, **net.crawl_transport_options(route))
                try:
                    if scheme == "http":
                        assert client.get("http://example.test/").text == "ok"
                    else:
                        with pytest.raises(net.NetworkUnavailable, match="proxy CONNECT failed"):
                            client.get("https://example.test/")
                finally:
                    client.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert seen[0][0] == "GET http://93.184.216.34/ HTTP/1.1"
    assert "Host: example.test" in seen[0][1]
    assert seen[1][0] == "CONNECT 93.184.216.34:443 HTTP/1.1"
    assert all(
        any(row.lower().startswith("proxy-authorization: basic ") for row in headers)
        for _, headers in seen
    )
    assert all("password" not in line for line, _ in seen)


def test_rebinding_and_private_redirect_fail_before_proxy_contact(monkeypatch):
    calls = []
    real = socket.getaddrinfo

    def lookup(host, port, *args, **kwargs):
        if host == "example.test":
            calls.append(host)
            address = "93.184.216.34" if len(calls) == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(net.socket, "getaddrinfo", lookup)
    route = net.resolve_proxy_route("http://127.0.0.1:9", allow_private=True)
    client, _ = net.http_client(1.0, **net.crawl_transport_options(route))
    try:
        with pytest.raises(ValueError, match="private"):
            client.get("http://example.test/")
    finally:
        client.close()
    assert len(calls) >= 2


def test_proxied_redirect_to_private_target_is_blocked_before_second_hop(monkeypatch):
    seen = []

    class Proxy(socketserver.StreamRequestHandler):
        def handle(self):
            seen.append(self.rfile.readline().decode().strip())
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            self.wfile.write(
                b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/private\r\n"
                b"Content-Length: 0\r\n\r\n"
            )

    with socketserver.TCPServer(("127.0.0.1", 0), Proxy) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real = socket.getaddrinfo
        monkeypatch.setattr(net.socket, "getaddrinfo", _answers(real))
        route = net.resolve_proxy_route(
            f"http://127.0.0.1:{server.server_address[1]}", allow_private=True
        )
        client, _ = net.http_client(
            2.0, follow_redirects=True, **net.crawl_transport_options(route)
        )
        try:
            with pytest.raises(net.BlockedRedirectError, match="private"):
                client.get("http://example.test/")
        finally:
            client.close()
            server.shutdown()
            thread.join(timeout=5)
    assert seen == ["GET http://93.184.216.34/ HTTP/1.1"]


def test_proxy_407_is_distinct_from_target_status(monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(net.socket, "getaddrinfo", _answers(real))

    class Proxy(socketserver.StreamRequestHandler):
        def handle(self):
            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            self.wfile.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n"
            )

    with socketserver.TCPServer(("127.0.0.1", 0), Proxy) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        route = net.resolve_proxy_route(
            f"http://127.0.0.1:{server.server_address[1]}", allow_private=True
        )
        client, _ = net.http_client(2.0, **net.crawl_transport_options(route))
        try:
            with pytest.raises(net.NetworkUnavailable, match="proxy authentication failed"):
                client.get("http://example.test/")
        finally:
            client.close()
            server.shutdown()
            thread.join(timeout=5)


def test_proxy_transport_errors_omit_exception_secrets(monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(net.socket, "getaddrinfo", _answers(real))
    route = net.resolve_proxy_route("http://example.test:3128")
    with patch.object(
        httpx.HTTPTransport,
        "handle_request",
        side_effect=httpx.ReadError(
            "Authorization: Bearer synthetic-secret Cookie: sid=synthetic-secret"
        ),
    ):
        client, _ = net.http_client(1.0, **net.crawl_transport_options(route))
        try:
            with pytest.raises(net.NetworkUnavailable, match="proxied request failed") as error:
                client.get("http://example.test/?token=synthetic-secret")
        finally:
            client.close()
    assert "synthetic-secret" not in str(error.value)


def test_crawl_policy_ignores_ambient_proxies_but_retains_explicit_ca(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    options = net.crawl_transport_options()
    assert options["trust_env"] is False
    assert "proxy_route" not in options
    with patch("ssl.create_default_context", return_value="verifying-context") as context:
        monkeypatch.setenv("SSL_CERT_FILE", "/synthetic/ca.pem")
        assert net.crawl_transport_options()["verify"] == "verifying-context"
        context.assert_called_once_with(cafile="/synthetic/ca.pem", capath=None)


def test_cli_and_mcp_reach_the_same_proxy_setting(monkeypatch):
    from seohead import cli
    from seohead.servers import handlers
    from seohead.servers.mcp_server import build_server

    assignment = "http.proxy=env:SEOHEAD_TEST_PROXY"
    args = cli.build_parser().parse_args(
        ["crawl-site", "--url", "https://example.test/", "--set", assignment]
    )
    cli_kwargs = cli._build_kwargs("crawl-site", args)[1]
    assert cli_kwargs["overrides"] == {"http.proxy": "env:SEOHEAD_TEST_PROXY"}
    captured = {}
    monkeypatch.setattr(
        handlers, "crawl_site", lambda **kwargs: captured.update(kwargs) or {"ok": True}
    )
    tool = build_server()._tool_manager.get_tool("seo_crawl_site")
    result = tool.fn(
        url="https://example.test/",
        overrides=cli_kwargs["overrides"],
    )
    assert result["ok"]
    assert captured["overrides"] == cli_kwargs["overrides"]
    described = handlers.crawl_describe_settings()["settings"]
    assert {item["path"] for item in described} >= {"http.proxy", "http.proxy_allow_private"}


def test_inline_proxy_credentials_never_reach_runlog_or_parse_errors():
    secret_url = "http://username:supersecret@example.test:3128"
    assert safe_arguments({"overrides": {"http.proxy": secret_url}}) == {
        "overrides": {"http.proxy": "[redacted]"}
    }
    assert safe_arguments({"params": {"keywords": ["first"]}}) == {
        "params": {"keywords": ["first"]}
    }
    with pytest.raises(settings.ConfigError) as error:
        settings.parse_setting_assignment("http.proxy " + secret_url)
    assert "supersecret" not in str(error.value)


def test_saved_proxy_scan_is_inspectable_but_resume_is_refused_without_egress(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from seohead.crawl.sqlite_adapter import crawl_to_scan
    from seohead.servers.scan_handlers import resume_inputs
    from seohead.storage.native_scan import NativeScan
    from tests.test_scan_native import _metadata

    real = socket.getaddrinfo
    monkeypatch.setattr(net.socket, "getaddrinfo", _answers(real))
    monkeypatch.setenv("SEOHEAD_TEST_PROXY", "http://user:proxy-secret-745@example.test:3128")
    config = settings.load(
        overrides={
            "http.proxy": "env:SEOHEAD_TEST_PROXY",
            "robots.policy": "ignore",
            "limits.max_urls": 1,
            "speed.min_delay_seconds": 0,
        }
    )
    path = tmp_path / "proxy-scan.sqlite"
    crawl_to_scan(
        "https://example.test/",
        scan_out=str(path),
        settings=config,
        producer_version="test",
        producer_revision="a" * 40,
        runtime_versions=_metadata()["runtime_versions"],
        fetcher=lambda _url: SimpleNamespace(
            status_code=200,
            content=b"<html><title>Synthetic</title></html>",
            text="<html><title>Synthetic</title></html>",
            headers={"content-type": "text/html"},
        ),
        sleeper=lambda _: None,
    )
    monkeypatch.delenv("SEOHEAD_TEST_PROXY")
    inspected = NativeScan.inspect(path)
    assert inspected["scan"]["source_kind"] == "native"
    assert "proxy-secret-745" not in path.read_bytes().decode("latin-1")
    with pytest.raises(ValueError, match="proxied scans cannot be resumed"):
        resume_inputs(str(path))


def test_legacy_and_sqlite_crawls_route_robots_and_pages_through_proxy(monkeypatch, tmp_path):
    from seohead.servers.handlers import crawl_site

    seen = []

    class Proxy(socketserver.StreamRequestHandler):
        def handle(self):
            request = self.rfile.readline().decode().strip()
            headers = []
            while True:
                row = self.rfile.readline().decode().strip()
                if not row:
                    break
                headers.append(row)
            seen.append((request, headers))
            if "robots.txt" in request:
                body = b"User-agent: *\nAllow: /\nSitemap: http://example.test/sitemap.xml\n"
            elif "sitemap.xml" in request:
                body = (
                    b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    b"<url><loc>http://example.test/</loc></url></urlset>"
                )
            elif "script.js" in request:
                body = b"const synthetic = true;"
            else:
                body = (
                    b"<html><head><title>Fixture</title><script src='/script.js'></script></head>"
                    b"<body><h1>Fixture</h1></body></html>"
                )
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                + str(len(body)).encode()
                + b"\r\n\r\n"
                + body
            )

    with socketserver.TCPServer(("127.0.0.1", 0), Proxy) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        real = socket.getaddrinfo
        monkeypatch.setattr(net.socket, "getaddrinfo", _answers(real))
        overrides = {
            "http.proxy": f"http://127.0.0.1:{server.server_address[1]}",
            "http.proxy_allow_private": True,
            "speed.min_delay_seconds": 0,
            "limits.max_urls": 1,
            "sitemaps.auto_discover": True,
        }
        try:
            legacy = crawl_site(
                url="http://example.test/",
                out_dir=str(tmp_path / "legacy"),
                overrides=overrides,
            )
            scan = crawl_site(
                url="http://example.test/",
                scan_out=str(tmp_path / "native.sqlite"),
                producer_build="a" * 40,
                overrides={
                    **overrides,
                    "resources.fetch": True,
                    "storage.format_version": "scan.v2",
                },
            )
            listed = crawl_site(
                urls=["http://example.test/"],
                out_dir=str(tmp_path / "list"),
                overrides=overrides,
            )
        finally:
            server.shutdown()
            thread.join(timeout=5)
    audit = json.loads((tmp_path / "legacy" / "audit.json").read_text())
    assert audit["run"]["crawl_config"]["http.proxy"]["mode"] == "proxy"
    assert legacy["urls_collected"] == 1
    assert scan["urls_collected"] == 1
    assert listed["urls_collected"] == 1
    assert (tmp_path / "native.sqlite").exists()
    requests = [item[0] for item in seen]
    assert requests.count("GET http://93.184.216.34/robots.txt HTTP/1.1") >= 2
    assert requests.count("GET http://93.184.216.34/ HTTP/1.1") >= 2
    assert any("/sitemap.xml " in request for request in requests)
    assert any("/script.js " in request for request in requests)
    assert all("93.184.216.34" in request for request in requests)
