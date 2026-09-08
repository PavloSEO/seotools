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
_ATTRIBUTION = re.compile(
    r"^(?:seohead|screaming frog)\s+(?:found|reported|detected)\s+(.+?)\.?$", re.IGNORECASE
)
_MAX_EVIDENCE_ITEMS = 10


def check_title(check: Any) -> str:
    """Return a reader-facing title without exposing a registry identifier."""
    check_id = str(check or "")
    if check_id in CHECKS:
        return str(check_meta(check_id).get("message") or "Audit finding")
    if re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+", check_id):
        return check_id.replace("_", " ").capitalize()
    return "Audit finding"


def _detail_rows(details: Any) -> list[str]:
    """Keep primitive recorded details, with readable labels and bounded shape."""
    if not isinstance(details, dict):
        return []
    rows: list[str] = []
    for key, value in sorted(details.items()):
        label = str(key).replace("_", " ").capitalize()
        if isinstance(value, (str, int, float, bool)) and value not in ("", None):
            rows.append(f"{label}: {value}")
        elif isinstance(value, list):
            values = [str(item) for item in value if isinstance(item, (str, int, float, bool))]
            if values:
                shown = values[:_MAX_EVIDENCE_ITEMS]
                suffix = (
                    f"; {len(values) - len(shown)} more values omitted"
                    if len(values) > len(shown)
                    else ""
                )
                rows.append(f"{label}: {', '.join(shown)}{suffix}")
    return rows


def _observation(value: Any) -> str:
    """Translate only known internal wrappers while preserving recorded content."""
    text = str(value or "").strip()
    if not text:
        return ""
    match = _ATTRIBUTION.fullmatch(text)
    if match:
        text = match.group(1)
    if _CHECK_IDENTIFIER.fullmatch(text) and text in CHECKS:
        return "The saved audit recorded no reader-facing observation."
    return _CHECK_IDENTIFIER.sub(
        lambda matched: (
            check_title(matched.group(0)) if matched.group(0) in CHECKS else matched.group(0)
        ),
        text,
    )


def _location_rows(locations: Any) -> list[str]:
    """Render saved source/anchor/XPath facts without turning them into new findings."""
    if not isinstance(locations, list):
        return []
    rows: list[str] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        bits = [
            f"Source: {location['source_url']}"
            if isinstance(location.get("source_url"), str) and location["source_url"]
            else "",
            f"Anchor: {location['anchor']}"
            if isinstance(location.get("anchor"), str) and location["anchor"]
            else "",
            f"Position: {location['link_position']}"
            if isinstance(location.get("link_position"), str) and location["link_position"]
            else "",
            f"XPath: {location['link_path']}"
            if isinstance(location.get("link_path"), str) and location["link_path"]
            else "",
        ]
        if any(bits):
            rows.append("; ".join(bit for bit in bits if bit))
    shown = rows[:_MAX_EVIDENCE_ITEMS]
    if len(rows) > len(shown):
        shown.append(f"{len(rows) - len(shown)} more locations omitted")
    return shown


def reproduction(finding: dict[str, Any], observation: str = "") -> str:
    """State only the primitive observation the saved audit can support."""
    url = finding.get("url")
    status = finding.get("status_code")
    if isinstance(url, str) and url:
        if isinstance(status, int):
            return f"{url} returned HTTP {status}."
        details = _detail_rows(finding.get("details"))
        if details:
            return f"At {url}: {details[0]}."
        if observation:
            return f"At {url}: {observation}"
    locations = finding.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict) and isinstance(location.get("source_url"), str):
                target = f" to {url}" if isinstance(url, str) and url else ""
                path = (
                    f" at {location['link_path']}"
                    if isinstance(location.get("link_path"), str) and location["link_path"]
                    else ""
                )
                return f"Link from {location['source_url']}{path}{target}."
    target = f" Target URL: {url}." if isinstance(url, str) and url else ""
    return "Reproduction unavailable from the saved audit." + target


def project_finding(finding: dict[str, Any]) -> dict[str, Any]:
    """Copy one finding with client-only display fields.

    ``text`` remains the audit's recorded observation.  Check IDs, producer
    names, and tool names are intentionally absent from the display fields;
    those identifiers remain in the original machine audit and JSON output.
    """
    projected = dict(finding)
    observation = _observation(finding.get("text"))
    projected["client_title"] = check_title(finding.get("check"))
    projected["client_observation"] = observation
    projected["client_reproduction"] = reproduction(finding, observation)
    projected["client_details"] = _detail_rows(finding.get("details"))
    projected["client_locations"] = _location_rows(finding.get("locations"))
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
