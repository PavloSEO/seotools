"""Pinned browser-route security contracts, entirely without Chromium or a socket."""

from __future__ import annotations

from seohead.tools import render


class _Request:
    def __init__(self, url="https://public.example.test/app.js", method="GET", headers=None):
        self.url = url
        self.method = method
        self._headers = headers or {"accept": "*/*", "host": "public.example.test"}
        self.post_data_buffer = None

    def all_headers(self):
        return self._headers


class _Route:
    def __init__(self, request=None):
        self.request = request or _Request()
        self.continued = False
        self.aborted = []
        self.fulfilled = []

    def continue_(self):
        self.continued = True

    def abort(self, reason):
        self.aborted.append(reason)

    def fulfill(self, **kwargs):
        self.fulfilled.append(kwargs)


def test_browser_route_does_not_continue_after_validation(monkeypatch):
    route = _Route()
    monkeypatch.setattr(render, "validate_url", lambda url: url)

    render._guard_browser_route(route)

    assert route.continued is False


class _Response:
    status_code = 200

    class _Headers:
        def multi_items(self):
            return [("content-encoding", "gzip"), ("content-type", "application/javascript")]

    headers = _Headers()

    def iter_raw(self):
        yield b"raw-compressed-bytes"


class _Stream:
    def __enter__(self):
        return _Response()

    def __exit__(self, *_args):
        return False


class _Client:
    def __init__(self):
        self.calls = []

    def stream(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Stream()


def test_pinned_fulfiller_uses_raw_bytes_and_never_continues(monkeypatch):
    route = _Route()
    client = _Client()
    monkeypatch.setattr(render, "validate_url", lambda url: url)
    handler, limitations = render._pinned_browser_route(client, request_gate=lambda: None)

    handler(route)

    assert route.continued is False
    assert not route.aborted
    assert client.calls[0][0:2] == ("GET", "https://public.example.test/app.js")
    assert "host" not in client.calls[0][2]["headers"]
    fulfilled = route.fulfilled[0]
    assert fulfilled["body"] == b"raw-compressed-bytes"
    assert fulfilled["headers"]["content-encoding"] == "gzip"
    assert limitations == []


def test_pinned_fulfiller_aborts_before_a_response_exceeds_its_finite_cap(monkeypatch):
    route = _Route()
    client = _Client()
    monkeypatch.setattr(render, "validate_url", lambda url: url)
    handler, limitations = render._pinned_browser_route(client, max_response_bytes=3)

    handler(route)

    assert route.aborted == ["blockedbyclient"]
    assert not route.fulfilled
    assert limitations == ["browser response exceeds pinned rendering byte limit"]


def test_pinned_fulfiller_fails_closed_for_private_url_method_and_cookie_response(monkeypatch):
    client = _Client()
    handler, limitations = render._pinned_browser_route(client)
    private = _Route(_Request("http://169.254.169.254/"))
    monkeypatch.setattr(
        render, "validate_url", lambda _url: (_ for _ in ()).throw(ValueError("private"))
    )
    handler(private)
    assert private.aborted == ["blockedbyclient"]

    method = _Route(_Request(method="POST"))
    handler(method)
    assert method.aborted == ["blockedbyclient"]
    assert any("private" in item for item in limitations)
    assert any("POST" in item for item in limitations)


def test_websocket_routes_are_closed_without_connecting():
    calls = []

    class _WebSocket:
        def close(self):
            calls.append("close")

        def connect_to_server(self):
            calls.append("connect")

    render._guard_websocket_route(_WebSocket())
    assert calls == ["close"]
