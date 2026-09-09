"""Bridge captured parser output to versioned corpus/structured/i18n contexts.

This module belongs at the collector boundary: it receives one concrete
representation and returns closed context items for the existing storage writer.
It neither opens a URL nor admits anything to the crawl frontier.
"""

from __future__ import annotations

import json
from typing import Any


def capture(
    *,
    page_url_id: int,
    source_document_id: int,
    representation: str,
    html: str | None,
    parsed: dict[str, Any] | None,
    settings: dict[str, Any],
    indexable: bool | None = None,
    canonical_target: str = "",
    unavailable_reason: str = "",
    extraction_rules: Any = None,
) -> list[dict[str, Any]]:
    """Return atomic context items for corpus, structured, language and rules evidence."""
    from seohead.storage import content_evidence, structured_evidence

    content = content_evidence.capture_document(
        page_url_id=page_url_id,
        source_document_id=source_document_id,
        representation=representation,
        html=html,
        parsed=parsed,
        content_area=settings.get("content_area"),
        indexable=indexable,
        canonical_target=canonical_target,
        unavailable_reason=unavailable_reason,
    )
    structured = structured_evidence.structured_payload(
        page_url_id=page_url_id,
        source_document_id=source_document_id,
        representation=representation,
        parsed=parsed,
    )
    language = structured_evidence.language_payload(
        page_url_id=page_url_id,
        source_document_id=source_document_id,
        representation=representation,
        parsed=parsed,
        html=html,
    )
    items = [content_evidence.context_item(content), *structured_evidence.context_items(structured, language)]
    if extraction_rules is not None:
        from seohead.tools.extraction_rules import evaluate

        rules = evaluate(html=html, parsed=parsed, rules=extraction_rules, representation=representation)
        items.append(
            {
                "kind": "extraction_rule_evidence",
                "item_key": f"page:{page_url_id}:document:{source_document_id}:representation:{representation}",
                "payload_version": "scan_context.v1",
                "payload_json": json.dumps(rules, sort_keys=True, separators=(",", ":")),
                "completeness": "complete" if rules["state"] == "complete" else "unavailable",
                "reason": rules["reason"],
            }
        )
    return items
