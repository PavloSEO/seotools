"""The pinned render route must hand Chromium a decoded document (#650).

``route.fulfill`` writes the body it is given straight into the renderer and
never applies a declared ``Content-Encoding``. Reading the origin with httpx's
undecoded stream and forwarding the origin's ``content-encoding`` alongside it
therefore built a DOM out of the compressed bytes: ``render-check`` answered
``incomplete_render`` -- no title, no h1, no canonical, no links -- on every
site that compresses its HTML, which is nearly all of them, and blamed the
site for it.

These tests drive the real route handler against a real loopback origin over a
real httpx client, so the compression is real and the assertions are about the
bytes the browser would actually receive. No Chromium is involved: what the
bug produced was a wrong body, and the body is observable at the route.
"""

from __future__ import annotations

import contextlib
import gzip
import http.server
import threading
import zlib

import httpx
import pytest
from bs4 import BeautifulSoup

from seohead.tools import render

# A page with every landmark ``render-check`` compares, and enough words that a
# compressed transfer is markedly smaller than the document itself.
PAGE = (
    "<html><head><title>Fixture</title>"
    '<link rel="canonical" href="http://127.0.0.1/"></head>'
    "<body><h1>Fixture</h1><p>" + "word " * 400 + "</p>"
    '<a href="/a">a</a><a href="/b">b</a></body></html>'
).encode()


def _brotli(body: bytes) -> bytes:
    import brotli

    return brotli.compress(body)


def _zstd(body: bytes) -> bytes:
    import zstandard

    return zstandard.ZstdCompressor().compress(body)


# Every coding this route may legitimately meet. ``br`` and ``zstd`` are only
# decodable when the matching optional library is installed, so their cases skip
# where it is not -- and run, unchanged, where it is.
ENCODERS = {
    "identity": lambda body: body,
    "gzip": gzip.compress,
    "deflate": zlib.compress,
    "br": _brotli,
    "zstd": _zstd,
}


@contextlib.contextmanager
def _origin(encoding: str, *, body: bytes = PAGE, encoder=None):
    """Serve one page on loopback under ``encoding``, recording what was asked for."""
    payload = (encoder or ENCODERS[encoding])(body)
    asked: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def do_GET(self):
            asked.append(self.headers.get("accept-encoding", ""))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if encoding != "identity":
                self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", payload, asked
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _Request:
    """Chromium's view of the document request, including the codings it invites."""

    def __init__(self, url, headers=None):
        self.url = url
        self.method = "GET"
        self._headers = headers or {
            "accept": "text/html",
            "accept-encoding": "gzip, deflate, br, zstd",
        }

    def all_headers(self):
        return self._headers


class _Route:
    def __init__(self, request):
        self.request = request
        self.aborted: list[str] = []
        self.fulfilled: list[dict] = []

    def continue_(self):  # pragma: no cover - the route never continues
        raise AssertionError("the pinned route must never continue a request")

    def abort(self, reason):
        self.aborted.append(reason)

    def fulfill(self, **kwargs):
        self.fulfilled.append(kwargs)


def _decodable() -> set[str]:
    """The codings httpx can decode, read back from the header it sends itself."""
    with httpx.Client() as client:
        return {item.strip() for item in client.headers.get("accept-encoding", "").split(",")}


def _run(url, *, max_response_bytes=None, request_headers=None):
    client = httpx.Client()
    kwargs = {} if max_response_bytes is None else {"max_response_bytes": max_response_bytes}
    handler, limitations = render._pinned_browser_route(client, **kwargs)
    route = _Route(_Request(url, headers=request_headers))
    try:
        handler(route)
    finally:
        client.close()
    return route, limitations


@pytest.fixture(autouse=True)
def _allow_loopback(monkeypatch):
    """The URL guard is not what is under test here; the response body is."""
    monkeypatch.setattr(render, "validate_url", lambda url: url)


@pytest.mark.parametrize("encoding", sorted(ENCODERS))
def test_a_compressed_page_reaches_the_browser_fully_decoded(encoding):
    # httpx advertises exactly the codings it has a decoder for, so its own
    # Accept-Encoding decides which cases can run here. br and zstd skip without
    # brotli/zstandard installed and run, unchanged, once they are.
    if encoding != "identity" and encoding not in _decodable():
        pytest.skip(f"httpx does not decode {encoding} in this environment")

    with _origin(encoding) as (url, payload, _asked):
        route, limitations = _run(url)

    assert limitations == []
    assert not route.aborted
    body = route.fulfilled[0]["body"]

    # The whole document, not the transfer. On every coding but identity the
    # transferred payload is far smaller, which is exactly the size the broken
    # route reported as the rendered page.
    assert body == PAGE
    assert len(body) == len(PAGE)
    if encoding != "identity":
        assert len(payload) < len(PAGE)

    # The landmarks render-check compares are all present in what Chromium gets.
    soup = BeautifulSoup(body, "html.parser")
    assert soup.title.get_text() == "Fixture"
    assert soup.h1.get_text() == "Fixture"
    assert soup.find("link", rel="canonical")["href"] == "http://127.0.0.1/"
    assert [a["href"] for a in soup.find_all("a")] == ["/a", "/b"]


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_the_fulfilled_headers_never_contradict_the_fulfilled_body(encoding):
    with _origin(encoding) as (url, _payload, _asked):
        route, _limitations = _run(url)

    names = {name.lower() for name in route.fulfilled[0]["headers"]}
    # A coding header would describe a body this route already decoded, and a
    # transferred length would describe bytes Chromium never sees.
    assert "content-encoding" not in names
    assert "content-length" not in names
    assert "content-type" in names


def test_the_route_asks_the_origin_only_for_codings_it_can_decode():
    with _origin("gzip") as (url, _payload, asked):
        _route, _limitations = _run(url)

    # Chromium's own list (which includes br and zstd) is replaced, because the
    # decoding happens here and not in the browser.
    assert {item.strip() for item in asked[0].split(",")} == _decodable()


def test_a_coding_the_client_cannot_decode_is_refused_rather_than_rendered():
    # A non-compliant origin can still answer in a coding nobody asked for.
    # Passing those bytes through would resurrect the bug silently, so the route
    # fails closed and names the coding instead.
    with _origin("x-seohead-unknown", encoder=lambda body: body) as (url, _payload, _asked):
        route, limitations = _run(url)

    assert route.aborted == ["blockedbyclient"]
    assert not route.fulfilled
    assert limitations == [
        "browser response content coding x-seohead-unknown is undecodable by pinned rendering"
    ]


def test_the_byte_cap_measures_the_decoded_body_not_the_transfer():
    # The cap protects the renderer, which only ever sees decoded bytes: a
    # highly compressible page that fits the cap on the wire must not pass it.
    bomb = b"<html><body>" + b"a" * 400_000 + b"</body></html>"
    with _origin("gzip", body=bomb) as (url, payload, _asked):
        assert len(payload) < 5_000
        route, limitations = _run(url, max_response_bytes=5_000)

    assert route.aborted == ["blockedbyclient"]
    assert not route.fulfilled
    assert limitations == ["browser response exceeds pinned rendering byte limit"]
