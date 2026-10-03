"""Synthetic, socket-free remote submission and transport safety tests."""

from __future__ import annotations

import socket
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from seohead.crawl import settings
from seohead.recon import net
from seohead.recon.remote_policy import (
    RemoteEgressPolicy,
    RemoteTargetError,
    current_remote_policy,
)
from seohead.tools import render


def _dns(monkeypatch, answers):
    calls = []

    def resolve(host, port, *, type):
        calls.append((host, port))
        answer = answers(host, len(calls)) if callable(answers) else answers.get(host, host)
        if isinstance(answer, str):
            answer = [answer]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in answer]

    monkeypatch.setattr(net.socket, "getaddrinfo", resolve)
    return calls


def _transport(monkeypatch, responder=None):
    requests = []

    def handle(_self, request):
        requests.append(request)
        if responder is not None:
            return responder(request)
        return httpx.Response(
            200,
            text="<html>ok</html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return requests


def _config(**overrides):
    return settings.load(overrides={"limits.max_requests": 20, **overrides})


def test_submission_requires_project_safe_target_and_finite_budgets(monkeypatch):
    _dns(monkeypatch, {"public.example.test": "93.184.216.34"})
    policy = RemoteEgressPolicy("project-a", max_total_requests=200)
    policy.authorize_submission("project-a", "https://public.example.test/", _config())
    with pytest.raises(RemoteTargetError) as foreign:
        policy.authorize_submission("project-b", "https://public.example.test/", _config())
    assert foreign.value.code == "foreign_project"
    for url in (
        "file:///etc/passwd",
        "https://user:secret@public.example.test/",
        "https://public.example.test/#fragment",
        "http://127.0.0.1/",
    ):
        with pytest.raises(RemoteTargetError) as rejected:
            policy.authorize_submission("project-a", url, _config())
        assert rejected.value.code == "unsafe_target"
        assert "secret" not in str(rejected.value)
    with pytest.raises(RemoteTargetError) as unbounded:
        policy.authorize_submission("project-a", "https://public.example.test/", settings.load())
    assert unbounded.value.code == "budget_exceeded"
    with pytest.raises(RemoteTargetError) as too_fast:
        policy.authorize_submission(
            "project-a",
            "https://public.example.test/",
            _config(**{"speed.min_delay_seconds": 0}),
        )
    assert too_fast.value.code == "budget_exceeded"
    with pytest.raises(RemoteTargetError) as proxy:
        policy.authorize_submission(
            "project-a",
            "https://public.example.test/",
            {**_config(), "http": {"proxy": "http://proxy.example.test:8080"}},
        )
    assert proxy.value.code in {"invalid_config", "unsupported_proxy"}
    remote_browser = _config()
    remote_browser["rendering"]["browser"]["transport"] = "remote"
    with pytest.raises(RemoteTargetError) as backend:
        policy.authorize_submission("project-a", "https://public.example.test/", remote_browser)
    assert backend.value.code in {"invalid_config", "unsupported_browser"}


@pytest.mark.parametrize(
    "name", ["max_requests_per_origin", "max_total_requests", "max_concurrency"]
)
@pytest.mark.parametrize("value", [True, 1.5, float("nan"), float("inf")])
def test_remote_numeric_caps_require_exact_positive_integers(name, value):
    with pytest.raises(ValueError, match="positive integers"):
        RemoteEgressPolicy("project-a", **{name: value})


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "0.5"])
def test_remote_delay_floor_rejects_nonfinite_or_nonnumeric_values(value):
    with pytest.raises(ValueError, match="delay floor"):
        RemoteEgressPolicy("project-a", min_delay_seconds=value)


def test_staging_allowlist_is_exact_and_never_authorizes_metadata(monkeypatch):
    answers = {"stage.example.test": "10.1.2.3", "other.example.test": "10.1.2.3"}
    _dns(monkeypatch, answers)
    policy = RemoteEgressPolicy("project-a", frozenset({"STAGE.EXAMPLE.TEST."}))
    policy.authorize_submission("project-a", "https://stage.example.test/", _config())
    with pytest.raises(RemoteTargetError):
        policy.authorize_submission("project-a", "https://other.example.test/", _config())
    answers["stage.example.test"] = "169.254.169.254"
    with pytest.raises(RemoteTargetError):
        policy.authorize_submission("project-a", "https://stage.example.test/", _config())
    with pytest.raises(ValueError):
        RemoteEgressPolicy("project-a", frozenset({"*.example.test"}))
    with pytest.raises(ValueError, match="DNS hostnames"):
        RemoteEgressPolicy("project-a", frozenset({"10.1.2.3"}))


@pytest.mark.parametrize("alias", [3232235777, "3232235777", "0xc0a80101", "0300.0250.1.1"])
def test_private_host_allowlist_rejects_numeric_aliases(alias):
    with pytest.raises(ValueError, match="DNS hostname"):
        RemoteEgressPolicy("project-a", frozenset({alias}))


def test_mixed_dns_answers_are_rejected_for_remote_jobs(monkeypatch):
    _dns(monkeypatch, {"public.example.test": ["93.184.216.34", "10.1.2.3"]})
    policy = RemoteEgressPolicy("project-a")
    with pytest.raises(RemoteTargetError) as rejected:
        policy.authorize_submission("project-a", "https://public.example.test/", _config())
    assert rejected.value.code == "unsafe_target"


def test_allowlisted_staging_name_rejects_mixed_public_private_answers(monkeypatch):
    _dns(monkeypatch, {"stage.example.test": ["93.184.216.34", "10.1.2.3"]})
    policy = RemoteEgressPolicy("project-a", frozenset({"stage.example.test"}))
    with pytest.raises(RemoteTargetError) as rejected:
        policy.authorize_submission("project-a", "https://stage.example.test/", _config())
    assert rejected.value.code == "unsafe_target"


def test_remote_transport_pins_after_dns_rebinding_even_with_local_private_opt_in(monkeypatch):
    monkeypatch.setenv(net.PRIVATE_NETWORK_ENV, "1")
    _dns(
        monkeypatch,
        lambda _host, call: "93.184.216.34" if call == 1 else "127.0.0.1",
    )
    dispatched = _transport(monkeypatch)
    policy = RemoteEgressPolicy("project-a")
    with policy.active():
        client, _ = net.http_client(5)
        with client, pytest.raises(ValueError, match="private or non-public"):
            client.get("https://public.example.test/path?token=secret")
    assert dispatched == []
    assert current_remote_policy() is None


def test_remote_transport_pins_public_destination_and_caps_each_origin(monkeypatch):
    _dns(monkeypatch, {"public.example.test": "93.184.216.34"})
    dispatched = _transport(monkeypatch)
    policy = RemoteEgressPolicy("project-a", max_requests_per_origin=2)
    with policy.active():
        client, _ = net.http_client(5)
        with client:
            assert client.get("https://public.example.test/one").status_code == 200
            assert client.get("https://public.example.test/two").status_code == 200
            with pytest.raises(RemoteTargetError) as exhausted:
                client.get("https://public.example.test/three")
    assert exhausted.value.code == "origin_budget"
    assert [request.url.host for request in dispatched] == ["93.184.216.34"] * 2
    assert all(
        request.extensions["sni_hostname"] == "public.example.test" for request in dispatched
    )
    assert all(request.headers["host"] == "public.example.test" for request in dispatched)


def test_remote_redirect_private_escape_is_rejected_before_next_dispatch(monkeypatch):
    _dns(monkeypatch, {"public.example.test": "93.184.216.34"})

    def redirect(request):
        return httpx.Response(
            302,
            headers={"location": "http://10.1.2.3/?token=secret"},
            request=request,
        )

    dispatched = _transport(monkeypatch, redirect)
    policy = RemoteEgressPolicy("project-a")
    with policy.active():
        client, _ = net.http_client(5)
        with client, pytest.raises(net.BlockedRedirectError) as blocked:
            client.get("https://public.example.test/")
    assert len(dispatched) == 1
    assert blocked.value.location == ""
    assert "secret" not in str(blocked.value)


def test_prepinned_worker_request_cannot_bypass_remote_policy(monkeypatch):
    dispatched = _transport(monkeypatch)
    policy = RemoteEgressPolicy("project-a")
    with policy.active():
        client, _ = net.http_client(5)
        request = httpx.Request(
            "GET",
            "http://127.0.0.1/",
            headers={"Host": "public.example.test"},
            extensions={"sni_hostname": "public.example.test"},
        )
        with client, pytest.raises(RemoteTargetError) as blocked:
            client.send(request)
    assert blocked.value.code == "unsafe_target"
    assert dispatched == []


def test_prepinned_approved_staging_address_keeps_host_binding(monkeypatch):
    dispatched = _transport(monkeypatch)
    policy = RemoteEgressPolicy("project-a", frozenset({"stage.example.test"}))
    with policy.active():
        client, _ = net.http_client(5)
        request = httpx.Request(
            "GET",
            "https://10.1.2.3/",
            headers={"Host": "stage.example.test"},
            extensions={"sni_hostname": "stage.example.test"},
        )
        with client:
            assert client.send(request).status_code == 200
    assert len(dispatched) == 1


@pytest.mark.parametrize(
    "extensions,headers",
    [
        ({"sni_hostname": ""}, {"Host": "stage.example.test"}),
        ({"sni_hostname": "stage.example.test"}, {"Host": "metadata.internal"}),
    ],
)
def test_prepinned_request_refuses_missing_identity_or_host_spoof(monkeypatch, extensions, headers):
    dispatched = _transport(monkeypatch)
    policy = RemoteEgressPolicy("project-a", frozenset({"stage.example.test"}))
    with policy.active():
        client, _ = net.http_client(5)
        request = httpx.Request("GET", "http://10.1.2.3/", headers=headers, extensions=extensions)
        with client, pytest.raises(RemoteTargetError) as denied:
            client.send(request)
    assert denied.value.code == "unsafe_target"
    assert dispatched == []


class _BrowserRequest:
    method = "GET"

    def __init__(self, url):
        self.url = url

    def all_headers(self):
        return {"accept": "text/html"}


class _BrowserRoute:
    def __init__(self, url):
        self.request = _BrowserRequest(url)
        self.aborted = []
        self.fulfilled = []

    def abort(self, reason):
        self.aborted.append(reason)

    def fulfill(self, **kwargs):
        self.fulfilled.append(kwargs)


def test_browser_subresource_uses_same_guarded_remote_client(monkeypatch):
    _dns(monkeypatch, {"public.example.test": "93.184.216.34"})
    monkeypatch.setenv(net.PRIVATE_NETWORK_ENV, "1")
    dispatched = _transport(monkeypatch)
    policy = RemoteEgressPolicy("project-a")
    with policy.active():
        client, _ = net.http_client(5, follow_redirects=False)
        route_handler, limitations = render._pinned_browser_route(client)
    # Playwright may call this route from a thread without the context variable.
    allowed = _BrowserRoute("https://public.example.test/script.js")
    route_handler(allowed)
    refused = _BrowserRoute("http://169.254.169.254/?token=secret")
    route_handler(refused)
    client.close()
    assert len(allowed.fulfilled) == 1
    assert refused.aborted == ["blockedbyclient"]
    assert len(dispatched) == 1
    assert "secret" not in str(limitations)


def test_browser_header_exception_is_redacted_under_remote_policy(monkeypatch):
    _dns(monkeypatch, {"public.example.test": "93.184.216.34"})
    dispatched = _transport(monkeypatch)
    policy = RemoteEgressPolicy("project-a")
    with policy.active():
        client, _ = net.http_client(5)
        handler, limitations = render._pinned_browser_route(client)
    route = _BrowserRoute("https://public.example.test/script.js")

    def secret_headers():
        raise RuntimeError("https://user:secret@public.example.test/?token=secret")

    route.request.all_headers = secret_headers
    handler(route)
    client.close()
    assert route.aborted == ["blockedbyclient"]
    assert len(dispatched) == 0
    assert limitations == ["pinned browser request failed: remote browser request failed"]
    assert "secret" not in str(limitations)


def test_remote_render_error_summary_discards_playwright_exception_text():
    policy = RemoteEgressPolicy("project-a")
    with policy.active():
        reason = render._error_summary(
            RuntimeError("https://user:secret@public.example.test/?token=secret")
        )
    assert reason == "remote browser request failed"


def test_approved_staging_fetch_and_browser_route_survive_thread_context_loss(monkeypatch):
    from seohead.crawl.collect import fetch_one

    _dns(monkeypatch, {"stage.example.test": "10.1.2.3"})
    dispatched = _transport(monkeypatch)
    policy = RemoteEgressPolicy("project-a", frozenset({"stage.example.test"}))
    with policy.active():
        client, _ = net.http_client(5, follow_redirects=False)
        route_handler, _limitations = render._pinned_browser_route(client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        record, parsed = pool.submit(
            fetch_one, "https://stage.example.test/", client=client
        ).result()
    route = _BrowserRoute("https://stage.example.test/script.js")
    route_handler(route)
    client.close()
    assert record.status_code == 200 and parsed is not None
    assert len(route.fulfilled) == 1
    assert len(dispatched) == 2
    assert all(request.url.host == "10.1.2.3" for request in dispatched)


def test_declared_resource_rechecks_dns_without_context_in_worker(monkeypatch):
    from seohead.crawl.resource_fetch import fetch_resource

    answers = {"stage.example.test": "10.1.2.3"}
    _dns(monkeypatch, answers)
    dispatched = _transport(monkeypatch)
    policy = RemoteEgressPolicy("project-a", frozenset({"stage.example.test"}))
    with policy.active():
        client, _ = net.http_client(5, follow_redirects=False)
    kwargs = {
        "settings": _config(**{"resources.fetch": True}),
        "client": client,
        "throttle": None,
        "origin_url": "https://stage.example.test/",
        "robots_allowed": lambda _url: True,
        "remaining_requests": 1,
    }
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(fetch_resource, "https://stage.example.test/a.js", "script", **kwargs).result()
    assert len(dispatched) == 1
    answers["stage.example.test"] = "169.254.169.254"
    with ThreadPoolExecutor(max_workers=1) as pool:
        blocked = pool.submit(
            fetch_resource, "https://stage.example.test/b.js", "script", **kwargs
        ).result()
    client.close()
    assert blocked.capture_state == "excluded_scope"
    assert len(dispatched) == 1


def test_transport_failure_never_exposes_exception_url_or_cause(monkeypatch):
    _dns(monkeypatch, {"public.example.test": "93.184.216.34"})

    def fail(_request):
        raise RuntimeError("https://user:secret@public.example.test/?token=secret")

    _transport(monkeypatch, fail)
    policy = RemoteEgressPolicy("project-a")
    with policy.active():
        client, _ = net.http_client(5)
        with client, pytest.raises(RemoteTargetError) as failed:
            client.get("https://public.example.test/?token=secret")
    assert failed.value.code == "transport_failure"
    assert failed.value.__cause__ is None
    assert "secret" not in str(failed.value)


def test_remote_timeout_keeps_retry_classification_without_leaking_url(monkeypatch):
    _dns(monkeypatch, {"public.example.test": "93.184.216.34"})

    def fail(_request):
        raise httpx.ConnectTimeout("https://public.example.test/?token=secret")

    _transport(monkeypatch, fail)
    policy = RemoteEgressPolicy("project-a")
    with policy.active():
        client, _ = net.http_client(5)
        with client, pytest.raises(httpx.TimeoutException) as failed:
            client.get("https://public.example.test/?token=secret")
    assert failed.value.__cause__ is None
    assert "secret" not in str(failed.value)


def test_remote_client_ignores_ambient_proxy_and_refuses_explicit_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.example.test:8080")
    options = []
    original_init = httpx.HTTPTransport.__init__

    def init(self, *args, **kwargs):
        options.append(kwargs)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.HTTPTransport, "__init__", init)
    policy = RemoteEgressPolicy("project-a")
    with policy.active():
        client, _ = net.http_client(5)
        client.close()
        with pytest.raises(RemoteTargetError) as denied:
            net.http_client(5, proxy_route="http://proxy.example.test:8080")
    assert options and options[0]["trust_env"] is False
    assert denied.value.code == "unsupported_proxy"


def test_checked_job_revalidates_at_dispatch(monkeypatch):
    addresses = {"public.example.test": "93.184.216.34"}
    _dns(monkeypatch, addresses)
    policy = RemoteEgressPolicy("project-a")
    policy.authorize_submission("project-a", "https://public.example.test/", _config())
    addresses["public.example.test"] = "10.1.2.3"
    with (
        pytest.raises(RemoteTargetError) as refused,
        policy.checked_job("project-a", "https://public.example.test/", _config()),
    ):
        pytest.fail("unsafe job body executed")
    assert refused.value.code == "unsafe_target"
    assert current_remote_policy() is None


def test_legacy_crawl_handler_uses_bound_policy_without_a_socket(monkeypatch, tmp_path):
    from seohead.servers import handlers

    _dns(monkeypatch, {"public.example.test": "93.184.216.34"})
    dispatched = _transport(monkeypatch)
    overrides = {
        "limits.max_requests": 20,
        "limits.max_urls": 1,
        "robots.policy": "ignore",
        "speed.min_delay_seconds": 0.01,
    }
    policy = RemoteEgressPolicy("project-a", max_total_requests=20, min_delay_seconds=0.01)
    effective = settings.load(overrides={**overrides, "output.dir": str(tmp_path / "scan")})
    with policy.checked_job("project-a", "https://public.example.test/", effective):
        result = handlers.crawl_site(
            url="https://public.example.test/", out_dir=str(tmp_path / "scan"), overrides=overrides
        )
    assert result["urls_collected"] == 1
    assert dispatched
    assert all(request.url.host == "93.184.216.34" for request in dispatched)
    assert current_remote_policy() is None


def test_sqlite_scan_uses_bound_policy_without_a_socket(monkeypatch, tmp_path):
    from seohead.crawl.sqlite_adapter import crawl_to_scan

    _dns(monkeypatch, {"public.example.test": "93.184.216.34"})
    dispatched = _transport(monkeypatch)
    effective = _config(
        **{
            "limits.max_urls": 1,
            "robots.policy": "ignore",
            "speed.min_delay_seconds": 0.01,
        }
    )
    policy = RemoteEgressPolicy("project-a", max_total_requests=20, min_delay_seconds=0.01)
    scan_path = tmp_path / "scan.sqlite"
    with policy.checked_job("project-a", "https://public.example.test/", effective):
        crawl_to_scan(
            "https://public.example.test/",
            scan_out=str(scan_path),
            settings=effective,
            producer_version="3.0.0",
            producer_revision="a" * 40,
            runtime_versions={
                "python": "test",
                "sqlite": "test",
                "httpx": "test",
                "lxml": "test",
                "beautifulsoup4": "test",
            },
            sleeper=lambda _seconds: None,
        )
    assert scan_path.is_file()
    assert dispatched
    assert all(request.url.host == "93.184.216.34" for request in dispatched)
