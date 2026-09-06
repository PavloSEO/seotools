"""Basic Auth proxy lifecycle and export rewriting (issues #263, #217, #461).

Both bugs live in the same code path: ``sf run --auth`` starts a local
credentialed proxy so Screaming Frog can crawl a site behind HTTP Basic Auth.

- #263: the proxy must be stopped even when the audit raises, not only on the
  happy path.
- #217: ``rewrite_exports`` only rewrites CSV. An authenticated crawl asking
  for a non-CSV export format must be refused before the crawl starts rather
  than shipping a report full of dead ``127.0.0.1`` URLs.
- #461: a protocol-relative (``//host/path``) redirect ``Location`` must be
  rewritten back into the proxy just like an absolute one is, or SF leaves
  the authenticated path and hits the protected origin directly.
"""

from __future__ import annotations

import http.server
import json
import threading
from typing import ClassVar

import httpx
import pytest

from seohead.sf import cli
from seohead.sf.core import auth_proxy
from seohead.sf.core.auth_proxy import AuthProxy


class FakeProxy:
    """Stands in for ``AuthProxy`` without opening a real socket."""

    instances: ClassVar[list[FakeProxy]] = []

    def __init__(self, target, user, password, host="127.0.0.1"):
        self.target = target
        self.origin = "https://protected.example"
        self.base_url = "http://127.0.0.1:45678"
        self.stopped = 0
        FakeProxy.instances.append(self)

    def start(self):
        return self.base_url

    def stop(self):
        self.stopped += 1


@pytest.fixture(autouse=True)
def _reset_fake_proxy():
    FakeProxy.instances.clear()
    yield
    FakeProxy.instances.clear()


# ── #263: the proxy outlives a failed audit ─────────────────────────────────


def test_proxy_is_stopped_when_the_audit_raises(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth_proxy, "AuthProxy", FakeProxy)

    def boom(**_kwargs):
        raise RuntimeError("synthetic SF failure")

    monkeypatch.setattr(cli, "run_audit", boom)

    code = cli.main(
        ["run", "--crawl", "https://protected.example/", "--auth", "user:password", "--quiet"]
    )

    assert code == 1
    assert len(FakeProxy.instances) == 1
    assert FakeProxy.instances[0].stopped == 1, "proxy must be stopped even after a failed audit"


class _FakeResult:
    """The minimal shape ``main()`` touches on a --quiet, no-format run."""

    run: ClassVar[dict] = {"crawl_valid": True}


def test_proxy_is_stopped_on_the_ordinary_success_path(monkeypatch, tmp_path):
    """Positive control's mirror: the happy path must keep stopping the proxy too."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(auth_proxy, "AuthProxy", FakeProxy)
    monkeypatch.setattr(cli, "run_audit", lambda **kwargs: _FakeResult())
    monkeypatch.setattr(cli, "write_json", lambda *_a, **_k: "")
    monkeypatch.setattr(cli, "write_markdown", lambda *_a, **_k: "")

    code = cli.main(
        ["run", "--crawl", "https://protected.example/", "--auth", "user:password", "--quiet"]
    )

    assert code == 0
    assert len(FakeProxy.instances) == 1
    assert FakeProxy.instances[0].stopped == 1


# ── #217: XLSX exports must not keep loopback proxy URLs ───────────────────


def test_auth_with_xlsx_export_format_is_refused_before_crawl_starts(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"sf_cli": {"export_format": "xlsx"}}), encoding="utf-8"
    )
    monkeypatch.setattr(auth_proxy, "AuthProxy", FakeProxy)

    called = []
    monkeypatch.setattr(cli, "run_audit", lambda **kw: called.append(kw))

    with pytest.raises(SystemExit):
        cli.main(["run", "--crawl", "https://protected.example/", "--auth", "user:password"])

    assert not called, "the crawl must never start for a format the rewrite cannot cover"
    assert not FakeProxy.instances, "no credentialed proxy should be opened for a refused run"


def test_auth_with_csv_export_format_is_not_refused(monkeypatch, tmp_path):
    """Negative control: the default (CSV) export format must keep working unchanged."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.json").write_text(
        json.dumps({"sf_cli": {"export_format": "csv"}}), encoding="utf-8"
    )
    monkeypatch.setattr(auth_proxy, "AuthProxy", FakeProxy)

    monkeypatch.setattr(cli, "run_audit", lambda **kwargs: _FakeResult())
    monkeypatch.setattr(cli, "write_json", lambda *_a, **_k: "")
    monkeypatch.setattr(cli, "write_markdown", lambda *_a, **_k: "")

    code = cli.main(
        ["run", "--crawl", "https://protected.example/", "--auth", "user:password", "--quiet"]
    )

    assert code == 0
    assert len(FakeProxy.instances) == 1


def test_rewrite_exports_covers_csv(tmp_path):
    """Baseline: CSV rewrite behaviour is preserved."""
    export = tmp_path / "internal_all.csv"
    export.write_text("Address\nhttp://127.0.0.1:45678/page\n", encoding="utf-8")

    changed = auth_proxy.rewrite_exports(
        str(tmp_path), "http://127.0.0.1:45678", "https://protected.example"
    )

    assert changed == 1
    assert "https://protected.example/page" in export.read_text(encoding="utf-8")
    assert "127.0.0.1" not in export.read_text(encoding="utf-8")


# ── #461: protocol-relative redirects must stay inside the proxy ───────────


class _RedirectingOriginHandler(http.server.BaseHTTPRequestHandler):
    """Origin server issuing protocol-relative and absolute-third-party redirects."""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/same-host":
            self.send_response(302)
            self.send_header("Location", "//" + self.headers.get("Host", "") + "/after")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/third-party":
            self.send_response(302)
            self.send_header("Location", "//other.example.com/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/third-party-absolute":
            self.send_response(302)
            self.send_header("Location", "https://other.example.com/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")


@pytest.fixture()
def _live_proxy(monkeypatch):
    """A real AuthProxy in front of a real origin, both on loopback."""
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    origin = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectingOriginHandler)
    thread = threading.Thread(target=origin.serve_forever, daemon=True)
    thread.start()

    proxy = AuthProxy(f"http://127.0.0.1:{origin.server_port}", "user", "pass")
    base = proxy.start()
    try:
        yield proxy, base
    finally:
        proxy.stop()
        origin.shutdown()
        origin.server_close()


def test_protocol_relative_redirect_to_target_host_is_rewritten_into_the_proxy(_live_proxy):
    proxy, base = _live_proxy
    response = httpx.get(base + "/same-host", follow_redirects=False)

    location = response.headers.get("location")
    assert location is not None
    # Must point back at the proxy's own host:port, never the real target host.
    assert proxy.target_host not in location
    assert location.startswith("//" + base.split("://", 1)[1])


def test_protocol_relative_redirect_to_third_party_passes_through_unchanged(_live_proxy):
    """Negative control: an unrelated host must not be touched."""
    _proxy, base = _live_proxy
    response = httpx.get(base + "/third-party", follow_redirects=False)

    assert response.headers.get("location") == "//other.example.com/x"


def test_absolute_redirect_to_third_party_passes_through_unchanged(_live_proxy):
    """Negative control for the pre-existing absolute-URL path."""
    _proxy, base = _live_proxy
    response = httpx.get(base + "/third-party-absolute", follow_redirects=False)

    assert response.headers.get("location") == "https://other.example.com/x"
