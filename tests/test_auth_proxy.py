"""Basic Auth proxy lifecycle and export rewriting (issues #263, #217).

Both bugs live in the same code path: ``sf run --auth`` starts a local
credentialed proxy so Screaming Frog can crawl a site behind HTTP Basic Auth.

- #263: the proxy must be stopped even when the audit raises, not only on the
  happy path.
- #217: ``rewrite_exports`` only rewrites CSV. An authenticated crawl asking
  for a non-CSV export format must be refused before the crawl starts rather
  than shipping a report full of dead ``127.0.0.1`` URLs.
"""

from __future__ import annotations

import json
from typing import ClassVar

import pytest

from seohead.sf import cli
from seohead.sf.core import auth_proxy


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
