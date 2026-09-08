"""Reader-facing finding labels and reproductions for human report formats.

The audit documents remain the machine-evidence source.  This module only makes
an immutable, display-oriented projection for Markdown, CSV, XLSX, and DOCX;
it neither fetches a URL nor derives a new audit verdict.
"""

from __future__ import annotations

import re
from typing import Any

from seohead.sf.core.registry import CHECKS, check_meta

_CHECK_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+\b")
_INTERNAL_PRODUCER = re.compile(r"\b(?:seohead|screaming frog)\b", re.IGNORECASE)


def check_title(check: Any) -> str:
    """Return a reader-facing title without exposing a registry identifier."""
    check_id = str(check or "")
    if check_id in CHECKS:
        return str(check_meta(check_id).get("message") or "Audit finding")
    return "Audit finding"


def _detail_rows(details: Any) -> list[str]:
    """Keep primitive recorded details, with readable labels and bounded shape."""
    if not isinstance(details, dict):
        return []
    rows: list[str] = []
    for key, value in sorted(details.items()):
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            label = str(key).replace("_", " ").capitalize()
            rows.append(f"{label}: {value}")
    return rows


def _observation(value: Any) -> str:
    """Keep recorded prose unless it is only an internal identifier or producer claim."""
    text = str(value or "").strip()
    if not text:
        return ""
    if _CHECK_IDENTIFIER.search(text) or _INTERNAL_PRODUCER.search(text):
        return "The saved audit recorded no reader-facing observation."
    return text


def reproduction(finding: dict[str, Any]) -> str:
    """State only the primitive observation the saved audit can support."""
    url = finding.get("url")
    status = finding.get("status_code")
    if isinstance(url, str) and url:
        if isinstance(status, int):
            return f"{url} returned HTTP {status}."
        return f"Observed at {url}."
    locations = finding.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict) and isinstance(location.get("source_url"), str):
                return f"Observed from {location['source_url']}."
    return "Reproduction unavailable from the saved audit."


def project_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Copy one finding with client-only display fields.

    ``text`` remains the audit's recorded observation.  Check IDs, producer
    names, and tool names are intentionally absent from the display fields;
    those identifiers remain in the original machine audit and JSON output.
    """
    projected = dict(finding)
    projected["client_title"] = check_title(finding.get("check"))
    projected["client_observation"] = _observation(finding.get("text"))
    projected["client_reproduction"] = reproduction(finding)
    projected["client_details"] = _detail_rows(finding.get("details"))
    return projected


def project_document(document: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow document copy whose findings have display fields."""
    projected = dict(document)
    projected["findings"] = [
        project_finding(finding)
        for finding in document.get("findings") or []
        if isinstance(finding, dict)
    ]
    return projected
