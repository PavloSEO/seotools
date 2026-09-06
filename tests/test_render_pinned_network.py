"""Pinned browser-route security contracts, entirely without Chromium or a socket."""

from __future__ import annotations

import httpx

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


class _RawStream(httpx.SyncByteStream):
    def __iter__(self):
        yield b"ok"

    def close(self):
        pass


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


def test_pinned_fulfiller_does_not_replay_httpx_cookies_when_browser_omits_them(monkeypatch):
    seen_cookies = []

    def transport(request):
        seen_cookies.append(request.headers.get("cookie"))
        headers = {"set-cookie": "session=server-state; Path=/"} if len(seen_cookies) == 1 else {}
        return httpx.Response(200, headers=headers, stream=_RawStream())

    client = httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(render, "validate_url", lambda url: url)
    handler, _limitations = render._pinned_browser_route(client)

    first = _Route(
        _Request(headers={"accept": "*/*", "host": "public.example.test", "cookie": "browser=1"})
    )
    second = _Route()
    handler(first)
    handler(second)

    assert seen_cookies == ["browser=1", None]
    assert not first.aborted
    assert not second.aborted
    assert len(first.fulfilled) == len(second.fulfilled) == 1
    client.close()


def test_pinned_fulfiller_combines_duplicate_ordinary_headers(monkeypatch):
    def transport(_request):
        return httpx.Response(
            200,
            headers=[
                ("content-security-policy", "default-src 'self'"),
                ("content-security-policy", "img-src https://cdn.example.test"),
                ("access-control-allow-origin", "https://one.example.test"),
                ("access-control-allow-origin", "https://two.example.test"),
            ],
            stream=_RawStream(),
        )

    client = httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(render, "validate_url", lambda url: url)
    route = _Route()
    handler, _limitations = render._pinned_browser_route(client)

    handler(route)

    headers = route.fulfilled[0]["headers"]
    assert (
        headers["content-security-policy"] == "default-src 'self', img-src https://cdn.example.test"
    )
    assert (
        headers["access-control-allow-origin"]
        == "https://one.example.test, https://two.example.test"
    )
    client.close()


def test_pinned_fulfiller_keeps_multiple_set_cookie_headers_as_newline_values(monkeypatch):
    def transport(_request):
        return httpx.Response(
            200,
            headers=[
                ("set-cookie", "session=one; Path=/; HttpOnly"),
                ("set-cookie", "csrf=two; Path=/; SameSite=Lax"),
            ],
            stream=_RawStream(),
        )

    client = httpx.Client(transport=httpx.MockTransport(transport))
    monkeypatch.setattr(render, "validate_url", lambda url: url)
    route = _Route()
    handler, _limitations = render._pinned_browser_route(client)

    handler(route)

    assert route.fulfilled[0]["headers"]["set-cookie"] == (
        "session=one; Path=/; HttpOnly\ncsrf=two; Path=/; SameSite=Lax"
    )
    client.close()


def test_pinned_fulfiller_uses_empty_cors_sentinel_for_an_unapproved_cross_origin_request(
    monkeypatch,
):
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, stream=_RawStream()))
    )
    monkeypatch.setattr(render, "validate_url", lambda url: url)
    route = _Route(
        _Request(
            headers={
                "accept": "*/*",
                "host": "public.example.test",
                "origin": "https://other.example.test",
            }
        )
    )
    handler, _limitations = render._pinned_browser_route(client)

    handler(route)

    assert route.fulfilled[0]["headers"]["access-control-allow-origin"] == ""
    client.close()


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
