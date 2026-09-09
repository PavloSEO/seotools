"""Offline derivations over persisted corpus, structured and language evidence.

This is intentionally an audit-core consumer rather than a collector.  It
does not re-open page bodies or contact targets: absent target observations stay
``unmeasured`` rather than being converted to an error or a clean relation.
"""

from __future__ import annotations

from typing import Any


def derive(con: Any, *, duplicate_threshold: float = 0.92) -> dict[str, Any]:
    """Return reproducible content/structured/i18n derivations from one scan connection."""
    from seohead.storage.content_evidence import derive_duplicates, read as read_content
    from seohead.storage.structured_evidence import read as read_structured

    content = read_content(con)
    structured = read_structured(con)
    duplicates = derive_duplicates(content["items"], threshold=duplicate_threshold)
    page_urls = {row[0]: row[1] for row in con.execute("SELECT url_id,url FROM urls")}
    observed_urls = set(page_urls.values())
    declarations = []
    for language in structured["language"]:
        source = page_urls.get(language["page_url_id"])
        for declaration in language["declarations"]:
            target = declaration["target"]
            declarations.append(
                {
                    "source_page_url_id": language["page_url_id"],
                    "source_url": source,
                    "source_document_id": language["source_document_id"],
                    "representation": language["representation"],
                    "ordinal": declaration["ordinal"],
                    "lang": declaration["lang"],
                    "raw_href": declaration["raw_href"],
                    "target": target,
                    "declaration_state": declaration["state"],
                    "target_observation": "observed" if target in observed_urls else "unmeasured",
                }
            )
    structured_states = {
        state: sum(item["state"] == state for item in structured["structured"])
        for state in ("eligible", "malformed", "absent", "unavailable")
    }
    return {
        "schema_version": "saved_corpus_derivations.v1",
        "duplicates": duplicates,
        "structured": {"states": structured_states, "items": structured["structured"]},
        "internationalization": {"declarations": declarations, "items": structured["language"]},
    }
