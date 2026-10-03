"""Build a validated PDF report without exposing intermediate files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

_INSTALL = "pip install 'seohead-seotools[pdf]' and install Chrome, Edge or Chromium"


def _sanitize_metadata(path: Path, *, lang: str) -> None:
    """Rewrite with only fixed, non-source metadata values."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(path)
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    title = "Технический SEO-аудит" if lang == "ru" else "Technical SEO audit"
    writer.add_metadata(
        {
            "/Title": title,
            "/Author": "SEOHEAD Tools",
            "/Subject": "Technical SEO audit report",
            "/Creator": "SEOHEAD Tools",
            "/Producer": "SEOHEAD Tools",
        }
    )
    cleaned = path.with_name(f".{path.stem}.metadata.pdf")
    try:
        with cleaned.open("wb") as stream:
            writer.write(stream)
        os.replace(cleaned, path)
    finally:
        cleaned.unlink(missing_ok=True)


def write_pdf_report(
    model: dict[str, Any], path: str | Path, *, lang: str = "en"
) -> dict[str, Any]:
    """Render, sanitize, validate and atomically publish one technical-audit PDF."""
    target = Path(path)
    try:
        import pypdf  # noqa: F401

        from seohead.reports.audit_pdf import render_audit_pdf_html
        from seohead.reports.chromium_pdf import print_to_pdf

        html = render_audit_pdf_html(model, lang=lang)
        with tempfile.TemporaryDirectory(prefix=".seohead-pdf-", dir=target.parent) as temp_dir:
            temp_root = Path(temp_dir)
            html_path = temp_root / "report.html"
            pdf_path = temp_root / "report.pdf"
            html_path.write_text(html, encoding="utf-8")
            rendered = print_to_pdf(html_path, pdf_path)
            if rendered.get("status") != "ok":
                status = rendered.get("status", "failed")
                error = (
                    "no local Chrome, Edge, or Chromium executable was found"
                    if status == "skipped"
                    else rendered.get("reason", "Chromium did not produce a PDF")
                )
                return {
                    "ok": False,
                    "format": "pdf",
                    "status": status,
                    "error": error,
                    "install": _INSTALL,
                }

            _sanitize_metadata(pdf_path, lang=lang)
            from seohead.reports.pdf_validation import validate_pdf_output

            validation = validate_pdf_output(pdf_path, model)
            if validation.get("status") != "ok":
                return {
                    "ok": False,
                    "format": "pdf",
                    "status": validation.get("status", "failed"),
                    "error": "PDF validation failed",
                    "validation": validation,
                    **({"install": validation["install"]} if validation.get("install") else {}),
                }

            os.replace(pdf_path, target)

        return {
            "ok": True,
            "format": "pdf",
            "path": str(target),
            "bytes": target.stat().st_size,
            "findings": model["summary"]["counts"]["findings"]["projected_count"],
            "pages": model["summary"]["counts"]["pages"]["projected_count"],
            "lang": lang,
            "validation": validation,
        }
    except ImportError as exc:
        return {
            "ok": False,
            "format": "pdf",
            "status": "skipped",
            "error": f"required PDF dependency is missing: {exc}",
            "install": _INSTALL,
        }
    except Exception as exc:
        return {
            "ok": False,
            "format": "pdf",
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
