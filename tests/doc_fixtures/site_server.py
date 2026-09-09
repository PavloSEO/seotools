"""A tiny loopback HTTP server standing in for the live internet in doc-command tests.

``tests/test_docs_commands_execute.py`` needs every documented ``seohead ...`` command
that names a URL to actually run, without opening a real socket to the outside world.
This server answers on 127.0.0.1 with the static fixtures in ``tests/doc_fixtures/site/``:
``robots.txt``, ``sitemap.xml`` and ``llms.txt`` are served as themselves; every other
path falls back to ``index.html`` so a doc example can point at ``/page``, ``/about``,
``/product/example`` or anything else without a matching file on disk.
"""

from __future__ import annotations

import contextlib
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

SITE_DIR = Path(__file__).with_name("site")
EXACT_FILES = {
    "/robots.txt": SITE_DIR / "robots.txt",
    "/sitemap.xml": SITE_DIR / "sitemap.xml",
    "/llms.txt": SITE_DIR / "llms.txt",
    "/image.png": SITE_DIR / "image.png",
}
_ORIGIN_REWRITES = {"/robots.txt", "/sitemap.xml"}


class _FixtureHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def do_GET(self) -> None:
        self._serve_fixture()

    def do_HEAD(self) -> None:
        self._serve_fixture()

    def _serve_fixture(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in _ORIGIN_REWRITES:
            body = (
                EXACT_FILES[path].read_bytes().replace(b"http://127.0.0.1", self._origin().encode())
            )
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(body)
            return
        if path not in EXACT_FILES:
            self.path = "/index.html"
        if self.command == "GET":
            super().do_GET()
        else:
            super().do_HEAD()

    def _origin(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def log_message(self, format: str, *args) -> None:
        pass  # keep pytest output clean; failures still surface through assertions


@contextlib.contextmanager
def run_fixture_site() -> Iterator[str]:
    """Serve the fixture site on an OS-assigned loopback port for the duration of the block."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
