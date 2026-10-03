"""Structural, size, text, and model-conservation checks for temporary audit PDFs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from seohead.reports.pdf_validation import validate_pdf_output

pypdf = pytest.importorskip("pypdf")


def _write_text_pdf(path: Path, pages: list[str]) -> None:
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 36 720 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)


def _model() -> dict[str, Any]:
    return {
        "schema": "seohead.technical-audit-pdf/1",
        "run": {"state": "partial", "reasons": ["Synthetic run reason"]},
        "summary": {
            "counts": {
                "findings": {"source_count": 1, "projected_count": 1},
                "pages": {"source_count": 1, "projected_count": 1},
                "checks": {
                    "ran": {"source_count": 1, "projected_count": 1},
                    "failed": {"source_count": 0, "projected_count": 0},
                    "skipped": {"source_count": 0, "projected_count": 0},
                    "disabled": {"source_count": 0, "projected_count": 0},
                    "capabilities": {"source_count": 0, "projected_count": 0},
                    "silent": {"source_count": 0, "projected_count": 0},
                },
                "backlog": {
                    "source_count": 1,
                    "projected_count": 1,
                    "state": "recorded",
                },
            }
        },
        "findings": [
            {
                "record": {"url": "https://example.invalid/finding-0001/"},
                "display": {
                    "title": "Finding sentinel 0001",
                    "observation": "Synthetic observation sentinel 0001",
                    "reproduction": "Synthetic reproduction sentinel 0001",
                },
            }
        ],
        "pages": [
            {
                "record": {
                    "url": "https://example.invalid/page-0001/",
                    "title": "Page sentinel 0001",
                }
            }
        ],
        "coverage": {"checks": [{"id": "CHECK_SENTINEL_0001", "reason": "Check reason sentinel"}]},
        "backlog": {"items": [{"record": {"title": "Backlog sentinel 0001"}}]},
        "omissions": [],
    }


def _text_pdf(path: Path) -> Path:
    _write_text_pdf(
        path,
        [
            "Finding sentinel 0001 Synthetic observation sentinel 0001 "
            "Synthetic reproduction sentinel 0001 https://example.invalid/finding-0001/ "
            "https://example.invalid/page-0001/ Page sentinel 0001 Backlog sentinel 0001 "
            "Synthetic run reason CHECK_SENTINEL_0001 Check reason sentinel"
        ],
    )
    return path


def test_valid_pdf_checks_signature_text_page_and_model_conservation(tmp_path):
    result = validate_pdf_output(_text_pdf(tmp_path / "audit.pdf"), _model())

    assert result == {
        "status": "ok",
        "page_count": 1,
        "size_bytes": (tmp_path / "audit.pdf").stat().st_size,
        "errors": [],
    }


def test_pdf_page_and_byte_limits_accept_values_at_the_boundary(tmp_path):
    path = _text_pdf(tmp_path / "audit.pdf")
    size = path.stat().st_size

    at_boundary = validate_pdf_output(path, _model(), page_limit=1, byte_limit=size)
    assert at_boundary["status"] == "ok"
    assert at_boundary["page_count"] == 1
    assert at_boundary["size_bytes"] == size


@pytest.mark.parametrize(
    ("page_limit", "byte_limit", "message"),
    [
        (0, 1, "page_limit"),
        (True, 1, "page_limit"),
        (1, 0, "byte_limit"),
        (1, True, "byte_limit"),
    ],
)
def test_nonpositive_or_boolean_limits_are_rejected(page_limit, byte_limit, message):
    with pytest.raises(ValueError, match=message):
        validate_pdf_output("unused.pdf", _model(), page_limit=page_limit, byte_limit=byte_limit)


def test_pdf_over_the_byte_limit_fails_without_claiming_success(tmp_path):
    path = _text_pdf(tmp_path / "audit.pdf")

    result = validate_pdf_output(path, _model(), byte_limit=path.stat().st_size - 1)

    assert result["status"] == "failed"
    assert result["page_count"] is None
    assert any("byte limit exceeded" in error for error in result["errors"])


def test_pdf_over_the_page_limit_fails_without_extracting_a_complete_result(tmp_path):
    path = tmp_path / "two-pages.pdf"
    _write_text_pdf(path, ["first synthetic page", "second synthetic page"])

    result = validate_pdf_output(path, _model(), page_limit=1)

    assert result["status"] == "failed"
    assert result["page_count"] == 2
    assert result["errors"] == ["PDF page limit exceeded: 2 > 1"]


def test_invalid_signature_and_text_blank_pages_fail(tmp_path):
    invalid = tmp_path / "not-pdf.pdf"
    invalid.write_bytes(b"not a pdf")
    invalid_result = validate_pdf_output(invalid, _model())
    assert invalid_result["status"] == "failed"
    assert "temporary output does not start with a PDF signature" in invalid_result["errors"]

    blank = tmp_path / "blank.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with blank.open("wb") as output:
        writer.write(output)
    blank_result = validate_pdf_output(blank, _model())
    assert blank_result["status"] == "failed"
    assert "PDF page 1 has no extractable text" in blank_result["errors"]


def test_pdf_validation_fails_when_projected_count_disagrees_with_model_rows(tmp_path):
    model = _model()
    model["summary"]["counts"]["findings"]["projected_count"] = 2

    result = validate_pdf_output(_text_pdf(tmp_path / "audit.pdf"), model)

    assert result["status"] == "failed"
    assert any("findings count mismatch" in error for error in result["errors"])


def test_pdf_model_check_conservation_includes_page_tool_failures(tmp_path):
    model = _model()
    model["summary"]["counts"]["checks"]["page_tools_failed"] = {
        "source_count": 1,
        "projected_count": 1,
    }
    model["coverage"]["checks"].append(
        {"id": "PAGE_TOOL_FAILED", "reason": "Synthetic page tool failure"}
    )
    path = tmp_path / "page-tool-failure.pdf"
    text = " ".join([*_pdf_sentinels(), "PAGE_TOOL_FAILED", "Synthetic page tool failure"])
    _write_text_pdf(path, [text])

    result = validate_pdf_output(path, model)

    assert result["status"] == "ok"
    assert result["errors"] == []


def test_pdf_validation_fails_when_a_source_row_is_unaccounted_for(tmp_path):
    model = _model()
    model["summary"]["counts"]["findings"]["source_count"] = 2

    result = validate_pdf_output(_text_pdf(tmp_path / "audit.pdf"), model)

    assert result["status"] == "failed"
    assert any("omits source rows" in error for error in result["errors"])


def test_pdf_text_must_retain_each_expected_model_sentinel(tmp_path):
    path = tmp_path / "missing-row.pdf"
    _write_text_pdf(path, ["Finding sentinel 0001 Page sentinel 0001"])

    result = validate_pdf_output(path, _model())

    assert result["status"] == "failed"
    assert any("Backlog sentinel 0001" in error for error in result["errors"])


def test_pdf_text_accepts_localized_generated_finding_title_and_reproduction(tmp_path):
    model = _model()
    finding = model["findings"][0]
    finding["display"]["title"] = "Title element is missing"
    finding["display"]["reproduction"] = (
        "At https://example.invalid/finding-0001/: Synthetic observation sentinel 0001"
    )
    finding["record"]["check"] = "TITLE_MISSING"
    source_sentinels = [
        value
        for value in _pdf_sentinels()
        if value not in {"Finding sentinel 0001", "Synthetic reproduction sentinel 0001"}
    ]
    source_sentinels.append("TITLE_MISSING")
    path = tmp_path / "localized.pdf"
    _write_text_pdf(path, [" ".join(source_sentinels)])

    result = validate_pdf_output(path, model)

    assert result["status"] == "ok"
    assert result["errors"] == []


def test_pdf_model_omissions_must_be_recorded_for_source_count_shortfalls(tmp_path):
    model = _model()
    model["summary"]["counts"]["findings"]["source_count"] = 2
    model["omissions"] = [{"reason": "Synthetic omitted source row 0002"}]
    path = tmp_path / "omission.pdf"
    _write_text_pdf(path, ["Synthetic omitted source row 0002 " + " ".join(_pdf_sentinels())])

    result = validate_pdf_output(path, model)

    assert result["status"] == "ok"


def _pdf_sentinels() -> list[str]:
    return [
        "Finding sentinel 0001",
        "Synthetic observation sentinel 0001",
        "Synthetic reproduction sentinel 0001",
        "https://example.invalid/finding-0001/",
        "https://example.invalid/page-0001/",
        "Page sentinel 0001",
        "Backlog sentinel 0001",
        "Synthetic run reason",
        "CHECK_SENTINEL_0001",
        "Check reason sentinel",
    ]


def test_missing_pypdf_is_skipped_with_an_install_command(tmp_path, monkeypatch):
    path = _text_pdf(tmp_path / "audit.pdf")
    original_import = __import__

    def import_without_pypdf(name, *args, **kwargs):
        if name == "pypdf":
            raise ImportError("synthetic missing dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", import_without_pypdf)
    result = validate_pdf_output(path, _model())

    assert result["status"] == "skipped"
    assert result["page_count"] is None
    assert result["install"] == "pip install 'seohead-seotools[pdf]'"
