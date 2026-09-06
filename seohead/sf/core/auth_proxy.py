"""Local proxy for crawling sites protected by HTTP Basic Authentication.

Screaming Frog accepts credentials neither in the URL nor through a command-line
option. It only supports authentication profiles saved manually from the GUI.
To make a protected staging site crawlable from the CLI, including in CI and by
agents, this module starts a local proxy that adds the ``Authorization`` header
and forwards each request to the target origin.

SF crawls ``http://127.0.0.1:<port>/...``. Afterward, :func:`rewrite_exports`
restores those proxy URLs to the original host in the generated exports.
"""

from __future__ import annotations

import base64
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from seohead.recon.net import http_client, validate_url

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}


class AuthProxy:
    """Forward requests to a target origin while injecting Basic Auth."""

    def __init__(self, target: str, user: str, password: str, host: str = "127.0.0.1"):
        target = validate_url(target)
        parts = urlsplit(target)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.target_host = parts.netloc
        self._credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._host = host
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._client = None

    @property
    def base_url(self) -> str:
        if not self._server:
            raise RuntimeError("proxy is not running")
        return f"http://{self._host}:{self._server.server_port}"

    def start(self) -> str:
        proxy = self
        self._client, _http2_capable = http_client(
            60,
            follow_redirects=False,
            headers={"Authorization": f"Basic {self._credentials}"},
        )

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # Keep the caller's console output clean.
                pass

            def do_GET(self):
                self._proxy(method="GET")

            def do_HEAD(self):
                self._proxy(method="HEAD")

            def _proxy(self, *, method: str):
                url = proxy.origin + self.path
                headers = {}
                for name, value in self.headers.items():
                    if name.lower() in _HOP_BY_HOP or name.lower() in {"host", "authorization"}:
                        continue
                    headers[name] = value

                try:
                    if proxy._client is None:
                        raise RuntimeError("proxy HTTP client is not initialized")
                    response = proxy._client.request(method, url, headers=headers)
                    body = response.content if method == "GET" else b""
                    self._respond(response.status_code, response.headers.multi_items(), body)
                except Exception as error:
                    message = str(error).encode()
                    self._respond(502, [("Content-Type", "text/plain")], message)

            def _respond(self, status: int, headers, body: bytes):
                self.send_response(status)
                for name, value in headers:
                    if name.lower() in _HOP_BY_HOP:
                        continue
                    # Keep redirects inside the proxy; otherwise SF would leave the
                    # authenticated path and request the protected origin directly.
                    if name.lower() == "location":
                        if value.startswith(f"//{proxy.target_host}"):
                            value = (
                                f"//{proxy.base_url.split('://', 1)[1]}"
                                + value[len(f"//{proxy.target_host}") :]
                            )
                        else:
                            value = value.replace(proxy.origin, proxy.base_url)
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

        self._server = ThreadingHTTPServer((self._host, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.stop()


def rewrite_exports(exports_dir: str, proxy_base: str, origin: str) -> int:
    """Replace proxy URLs in CSV exports with the original host.

    Without this rewrite, reports would contain URLs such as
    ``http://127.0.0.1:51234/en`` and checks for sitemaps, canonicals, and other
    host-sensitive signals would compare two different origins.
    """
    changed = 0
    for root, _dirs, files in os.walk(exports_dir):
        for name in files:
            if not name.lower().endswith(".csv"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8-sig", newline="") as handle:
                content = handle.read()
            if proxy_base not in content:
                continue
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write(content.replace(proxy_base, origin))
            changed += 1
    return changed


def parse_auth(value: str) -> tuple[str, str]:
    """Parse ``USERNAME:PASSWORD``; the password may contain colons."""
    user, separator, password = value.partition(":")
    if not separator or not user or not password:
        raise ValueError("--auth expects USERNAME:PASSWORD")
    return user, password


__all__ = ["AuthProxy", "parse_auth", "rewrite_exports"]
