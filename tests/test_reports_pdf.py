"""Shared PDF report-build integration tests with synthetic, offline fixtures."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

from seohead.audit.site import SCHEMA
from seohead.reports import build_report, pdf_output


def _audit():
    return {
        "ok": True,
        "schema": SCHEMA,
        "domain": "fixture.example.invalid",
        "url": "https://fixture.example.invalid/",
        "generated_at": "2026-10-03T00:00:00Z",
        "findings": [
            {
                "id": "fixture-finding-1",
                "check": "TITLE_MISSING",
                "severity": "warning",
                "url": "https://fixture.example.invalid/",
                "text": "Synthetic title finding",
            }
        ],
        "pages": [{"url": "https://fixture.example.invalid/", "status": 200}],
        "summary": {
            "pages_checked": 1,
            "findings_total": 1,
            "findings_by_severity": {"warning": 1},
        },
    }


def _model():
    return {
        "schema": "seohead.technical-audit-pdf/1",
        "summary": {
            "counts": {
                "findings": {"projected_count": 1},
                "pages": {"projected_count": 1},
            }
        },
    }


def _validation_module(result):
    return SimpleNamespace(validate_pdf_output=lambda path, model: result)


def test_write_pdf_report_validates_before_atomic_replace_and_cleans_html(tmp_path, monkeypatch):
    target = tmp_path / "audit.pdf"
    target.write_bytes(b"previous complete report")
    seen = {}

    def render_html(_model, *, lang):
        seen["lang"] = lang
        return "<html></html>"

    monkeypatch.setattr("seohead.reports.audit_pdf.render_audit_pdf_html", render_html)

    def render(html_path, pdf_path):
        seen["html"] = html_path.read_text(encoding="utf-8")
        seen["temporary_pdf"] = pdf_path
        pdf_path.write_bytes(b"%PDF-1.7\nsynthetic-pdf")
        return {"status": "ok", "path": str(pdf_path)}

    monkeypatch.setattr("seohead.reports.chromium_pdf.print_to_pdf", render)
    monkeypatch.setattr(
        pdf_output, "_sanitize_metadata", lambda path, *, lang: seen.update(sanitized=True)
    )
    validation = {"status": "ok", "page_count": 2, "size_bytes": 20, "errors": []}

    def validate(path, model):
        seen["validated_path"] = path
        seen["validated_model"] = model
        assert path.read_bytes().startswith(b"%PDF-")
        assert target.read_bytes() == b"previous complete report"
        return validation

    monkeypatch.setitem(
        sys.modules,
        "seohead.reports.pdf_validation",
        SimpleNamespace(validate_pdf_output=validate),
    )

    result = pdf_output.write_pdf_report(_model(), target, lang="ru")

    assert result["ok"] is True
    assert result["format"] == "pdf"
    assert result["lang"] == "ru"
    assert result["validation"] == validation
    assert result["findings"] == result["pages"] == 1
    assert target.read_bytes() == b"%PDF-1.7\nsynthetic-pdf"
    assert seen["lang"] == "ru"
    assert seen["sanitized"] is True
    assert seen["validated_model"] == _model()
    assert not list(tmp_path.glob(".seohead-pdf-*"))


def test_failed_validation_preserves_existing_pdf_and_removes_temp_files(tmp_path, monkeypatch):
    target = tmp_path / "audit.pdf"
    target.write_bytes(b"previous complete report")
    monkeypatch.setattr(
        "seohead.reports.audit_pdf.render_audit_pdf_html",
        lambda model, *, lang: "<html></html>",
    )

    def render(_html_path, pdf_path):
        pdf_path.write_bytes(b"%PDF-1.7\nincomplete")
        return {"status": "ok"}

    monkeypatch.setattr("seohead.reports.chromium_pdf.print_to_pdf", render)
    monkeypatch.setattr(pdf_output, "_sanitize_metadata", lambda path, *, lang: None)
    monkeypatch.setitem(
        sys.modules,
        "seohead.reports.pdf_validation",
        _validation_module(
            {"status": "failed", "page_count": 0, "size_bytes": 0, "errors": ["empty page"]}
        ),
    )

    result = pdf_output.write_pdf_report(_model(), target)

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert target.read_bytes() == b"previous complete report"
    assert not list(tmp_path.glob(".seohead-pdf-*"))


def test_missing_browser_is_a_skipped_result_and_does_not_replace_target(tmp_path, monkeypatch):
    target = tmp_path / "audit.pdf"
    target.write_bytes(b"previous complete report")
    monkeypatch.setattr(
        "seohead.reports.audit_pdf.render_audit_pdf_html",
        lambda model, *, lang: "<html></html>",
    )
    monkeypatch.setattr(
        "seohead.reports.chromium_pdf.print_to_pdf",
        lambda *_args: {"status": "skipped", "reason": "no browser found"},
    )

    result = pdf_output.write_pdf_report(_model(), target)

    assert result["ok"] is False
    assert result["status"] == "skipped"
    assert "seohead-seotools[pdf]" in result["install"]
    assert target.read_bytes() == b"previous complete report"
    assert not list(tmp_path.glob(".seohead-pdf-*"))


def test_report_build_routes_pdf_through_semantic_model_and_renderer(tmp_path, monkeypatch):
    target = tmp_path / "audit.pdf"
    model = _model()
    seen = {}
    monkeypatch.setattr(
        "seohead.reports.pdf_model.build_pdf_model",
        lambda document, *, project: seen.update(document=document, project=project) or model,
    )
    monkeypatch.setattr(
        "seohead.reports.pdf_output.write_pdf_report",
        lambda actual_model, path, *, lang: (
            seen.update(model=actual_model, path=path, lang=lang)
            or {"ok": True, "format": "pdf", "path": str(path), "bytes": 128}
        ),
    )

    result = build_report(_audit(), fmt="pdf", path=str(target), lang="ru")

    assert result["ok"] is True
    assert seen["document"]["schema"] == SCHEMA
    assert seen["model"] is model
    assert seen["path"] == target
    assert seen["lang"] == "ru"


def test_report_build_rejects_unknown_pdf_language_before_writing(tmp_path):
    result = build_report(_audit(), fmt="pdf", path=str(tmp_path / "audit.pdf"), lang="fr")

    assert result == {"ok": False, "error": "report language must be 'en' or 'ru'"}


def test_pdf_default_output_name_cannot_escape_from_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    document = _audit()
    document["domain"] = "../../outside"
    seen = {}
    monkeypatch.setattr(
        "seohead.reports.pdf_model.build_pdf_model",
        lambda _document, *, project: _model(),
    )
    monkeypatch.setattr(
        "seohead.reports.pdf_output.write_pdf_report",
        lambda _model_arg, path, *, lang: seen.update(path=path) or {"ok": True},
    )

    result = build_report(document, fmt="pdf")

    assert result["ok"] is True
    assert seen["path"].resolve().parent == tmp_path
    assert seen["path"].name == "audit-.._.._outside.pdf"


def test_cli_report_build_exposes_pdf_and_language_arguments():
    from seohead.cli import _build_kwargs, build_parser

    args = build_parser().parse_args(
        [
            "report-build",
            "--audit",
            "fixture.json",
            "--format",
            "pdf",
            "--lang",
            "ru",
        ]
    )

    handler, kwargs = _build_kwargs("report-build", args)

    assert handler == "report_build"
    assert kwargs == {"audit": "fixture.json", "fmt": "pdf", "lang": "ru"}


def test_mcp_report_build_exposes_pdf_language_argument(monkeypatch):
    from seohead.servers.mcp_server import build_server

    received = []
    monkeypatch.setattr(
        "seohead.servers.handlers.report_build",
        lambda **kwargs: received.append(kwargs) or {"ok": True},
    )
    tool = next(
        item
        for item in build_server()._tool_manager.list_tools()
        if item.name == "seo_report_build"
    )

    result = asyncio.run(
        tool.run({"audit": _audit(), "fmt": "pdf", "lang": "ru", "out": "audit.pdf"})
    )

    assert result == {"ok": True}
    assert len(received) == 1
    assert received[0]["audit"] == _audit()
    assert received[0]["fmt"] == "pdf"
    assert received[0]["out"] == "audit.pdf"
    assert received[0]["lang"] == "ru"
