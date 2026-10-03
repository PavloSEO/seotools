"""Validate a temporary technical-audit PDF before publishing its final path."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

MODEL_SCHEMA = "seohead.technical-audit-pdf/1"
DEFAULT_PAGE_LIMIT = 200
DEFAULT_BYTE_LIMIT = 25 * 1024 * 1024


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _projected_count_errors(model: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if model.get("schema") != MODEL_SCHEMA:
        return [f"unsupported or missing PDF model schema {model.get('schema')!r}"]

    summary = model.get("summary")
    counts = summary.get("counts") if isinstance(summary, Mapping) else None
    if not isinstance(counts, Mapping):
        return ["PDF model has no summary.counts object"]

    collections = {
        "findings": model.get("findings"),
        "pages": model.get("pages"),
    }
    backlog = model.get("backlog")
    collections["backlog"] = backlog.get("items") if isinstance(backlog, Mapping) else None
    for name, rows in collections.items():
        count_block = counts.get(name)
        if not isinstance(rows, list):
            errors.append(f"PDF model {name} must be a list")
            continue
        if not isinstance(count_block, Mapping):
            errors.append(f"PDF model has no summary.counts.{name} object")
            continue
        projected = count_block.get("projected_count")
        if type(projected) is not int or projected != len(rows):
            errors.append(
                f"PDF model {name} count mismatch: projected {projected!r}, rows {len(rows)}"
            )
        source_count = count_block.get("source_count")
        if source_count is not None and (type(source_count) is not int or source_count < len(rows)):
            errors.append(
                f"PDF model {name} source count {source_count!r} is below its projected rows"
            )
        if type(source_count) is int and source_count > len(rows) and not model.get("omissions"):
            errors.append(f"PDF model {name} omits source rows without an omission record")

    coverage = model.get("coverage")
    checks = coverage.get("checks") if isinstance(coverage, Mapping) else None
    check_counts = counts.get("checks")
    if not isinstance(checks, list) or not isinstance(check_counts, Mapping):
        errors.append("PDF model has no check coverage rows or counts")
    else:
        projected_total = 0
        for name in (
            "ran",
            "failed",
            "page_tools_failed",
            "skipped",
            "disabled",
            "capabilities",
            "silent",
        ):
            group = check_counts.get(name)
            if group is None:
                continue
            projected = group.get("projected_count") if isinstance(group, Mapping) else None
            if type(projected) is not int or projected < 0:
                errors.append(f"PDF model checks.{name}.projected_count is invalid")
            else:
                projected_total += projected
        if projected_total != len(checks):
            errors.append(
                f"PDF model check count mismatch: projected {projected_total}, rows {len(checks)}"
            )
    return errors


def _expected_text(model: Mapping[str, Any]) -> Counter[str]:
    expected: Counter[str] = Counter()

    def add(value: Any) -> None:
        if isinstance(value, str):
            token = _normalized(value)
            if len(token) > 1:
                expected[token] += 1

    for finding in model.get("findings", []):
        if not isinstance(finding, Mapping):
            continue
        display = finding.get("display")
        record = finding.get("record")
        # Titles and reproduction labels are presentation strings and may be
        # localized. The saved observation, URL and check identifier remain
        # invariant evidence and are checked individually below.
        add(display.get("observation") if isinstance(display, Mapping) else None)
        if isinstance(record, Mapping):
            add(record.get("url"))
            add(record.get("check"))

    for page in model.get("pages", []):
        record = page.get("record") if isinstance(page, Mapping) else None
        if isinstance(record, Mapping):
            add(record.get("url"))
            add(record.get("title"))

    backlog = model.get("backlog")
    items = backlog.get("items", []) if isinstance(backlog, Mapping) else []
    for item in items:
        record = item.get("record") if isinstance(item, Mapping) else None
        if isinstance(record, Mapping):
            add(record.get("title"))

    run = model.get("run")
    reasons = run.get("reasons", []) if isinstance(run, Mapping) else []
    for reason in reasons:
        add(reason)

    coverage = model.get("coverage")
    checks = coverage.get("checks", []) if isinstance(coverage, Mapping) else []
    for check in checks:
        if isinstance(check, Mapping):
            add(check.get("id"))
            add(check.get("reason"))

    for omission in model.get("omissions", []):
        if isinstance(omission, Mapping):
            for field in ("reason", "message", "notice"):
                add(omission.get(field))
        else:
            add(omission)

    return expected


def validate_pdf_output(
    pdf_path: str | Path,
    model: Mapping[str, Any],
    *,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    byte_limit: int = DEFAULT_BYTE_LIMIT,
) -> dict[str, Any]:
    """Check output structure, model conservation, extracted text, and hard limits.

    Call this on a temporary PDF. A caller must publish the destination only when
    ``status == "ok"``; it should delete the temporary file for ``failed`` or
    ``skipped`` results so an incomplete document never becomes the final output.
    The ``pdf`` optional extra supplies pypdf without making other report formats
    depend on a PDF parser.
    """
    if type(page_limit) is not int or page_limit < 1:
        raise ValueError("page_limit must be a positive integer")
    if type(byte_limit) is not int or byte_limit < 1:
        raise ValueError("byte_limit must be a positive integer")
    if not isinstance(model, Mapping):
        return {
            "status": "failed",
            "page_count": None,
            "size_bytes": None,
            "errors": ["PDF model must be an object"],
        }

    errors = _projected_count_errors(model)
    path = Path(pdf_path)
    if not path.is_file():
        errors.append("temporary PDF is missing")
        return {"status": "failed", "page_count": None, "size_bytes": None, "errors": errors}
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        errors.append(f"temporary PDF cannot be read: {exc}")
        return {"status": "failed", "page_count": None, "size_bytes": None, "errors": errors}
    if size_bytes == 0:
        errors.append("temporary PDF is empty")
    if size_bytes > byte_limit:
        errors.append(f"PDF byte limit exceeded: {size_bytes} > {byte_limit}")
    if not errors:
        try:
            with path.open("rb") as stream:
                signature = stream.read(5)
        except OSError as exc:
            errors.append(f"temporary PDF cannot be read: {exc}")
            signature = b""
        if signature != b"%PDF-":
            errors.append("temporary output does not start with a PDF signature")
    if errors:
        return {"status": "failed", "page_count": None, "size_bytes": size_bytes, "errors": errors}

    try:
        from pypdf import PdfReader
    except ImportError:
        reason = "pypdf is not installed; install the PDF extra"
        return {
            "status": "skipped",
            "page_count": None,
            "size_bytes": size_bytes,
            "errors": [reason],
            "install": "pip install 'seohead-seotools[pdf]'",
        }

    try:
        reader = PdfReader(str(path), strict=True)
        page_count = len(reader.pages)
        if page_count == 0:
            errors.append("PDF contains no pages")
        if page_count > page_limit:
            errors.append(f"PDF page limit exceeded: {page_count} > {page_limit}")
            return {
                "status": "failed",
                "page_count": page_count,
                "size_bytes": size_bytes,
                "errors": errors,
            }
        extracted = []
        for index, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if not text.strip():
                errors.append(f"PDF page {index} has no extractable text")
            extracted.append(text)
    except Exception as exc:
        return {
            "status": "failed",
            "page_count": None,
            "size_bytes": size_bytes,
            "errors": [f"PDF could not be parsed or text extracted: {exc}"],
        }

    if not errors:
        searchable = _normalized(" ".join(extracted))
        for token, expected_count in _expected_text(model).items():
            actual_count = len(re.findall(re.escape(token), searchable))
            if actual_count < expected_count:
                errors.append(
                    f"PDF text is missing model content: expected {expected_count} occurrence(s) "
                    f"of {token!r}, found {actual_count}"
                )
                if len(errors) >= 25:
                    errors.append("additional missing model text was omitted from diagnostics")
                    break

    return {
        "status": "ok" if not errors else "failed",
        "page_count": page_count,
        "size_bytes": size_bytes,
        "errors": errors,
    }
