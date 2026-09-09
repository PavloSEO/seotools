"""Offline derivations over persisted corpus, structured and language evidence.

This is intentionally an audit-core consumer rather than a collector.  It
does not re-open page bodies or contact targets: an unfetched URL row is not a
target observation, and every unavailable relationship names that fact.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urlsplit

from seohead.sf.core.normalize import norm_url
from seohead.tools.hreflang import code_error
from seohead.tools.parser import robots_directives


def _page_index(con: Any) -> tuple[dict[int, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Read only captured pages; ``urls`` alone never establishes an observation."""
    by_id: dict[int, dict[str, Any]] = {}
    by_normalized: dict[str, list[dict[str, Any]]] = {}
    query = (
        "SELECT p.url_id,u.url,p.document_id,p.status_code,p.canonical,p.meta_robots,p.x_robots,"
        "p.representation,p.hreflang_outside_head FROM pages p JOIN urls u USING(url_id)"
    )
    for row in con.execute(query):
        item = (
            dict(row)
            if hasattr(row, "keys")
            else dict(
                zip(
                    (
                        "url_id",
                        "url",
                        "document_id",
                        "status_code",
                        "canonical",
                        "meta_robots",
                        "x_robots",
                        "representation",
                        "hreflang_outside_head",
                    ),
                    row,
                    strict=True,
                )
            )
        )
        by_id[item["url_id"]] = item
        by_normalized.setdefault(norm_url(item["url"]), []).append(item)
    return by_id, by_normalized


def _indexability(page: dict[str, Any]) -> dict[str, str]:
    """Retain only the indexability facts the stored page row can support."""
    status = page.get("status_code")
    if type(status) is not int:
        return {"state": "unmeasured", "reason": "captured page has no HTTP status"}
    if not 200 <= status < 300:
        return {"state": "non_indexable", "reason": f"HTTP {status}"}
    if "noindex" in robots_directives(page.get("meta_robots"), page.get("x_robots")):
        return {"state": "non_indexable", "reason": "noindex directive"}
    canonical = page.get("canonical")
    if isinstance(canonical, str) and canonical and norm_url(canonical) != norm_url(page["url"]):
        return {"state": "non_indexable", "reason": "canonicalized to another URL"}
    return {
        "state": "indexable_candidate",
        "reason": "stored status, directives and canonical do not exclude indexing",
    }


def _target(target_url: str, by_normalized: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Resolve only one unambiguous captured target page, never a queued URL."""
    if not target_url:
        return {"state": "unmeasured", "reason": "declaration has no resolved target"}
    candidates = by_normalized.get(norm_url(target_url), [])
    if not candidates:
        return {
            "state": "unmeasured",
            "reason": "target has no captured page observation in this scan",
        }
    if len(candidates) != 1:
        return {
            "state": "unmeasured",
            "reason": "target has multiple captured URL variants in this scan",
        }
    page = candidates[0]
    return {
        "state": "observed",
        "page_url_id": page["url_id"],
        "document_id": page.get("document_id"),
        "representation": page.get("representation"),
        "status_code": page.get("status_code"),
        "canonical": page.get("canonical") or "",
        "indexability": _indexability(page),
    }


def _relative(raw_href: str) -> dict[str, str]:
    if not raw_href:
        return {"state": "malformed", "reason": "declaration has no href"}
    split = urlsplit(raw_href)
    if split.scheme or raw_href.startswith("//"):
        return {"state": "absolute", "reason": ""}
    return {"state": "relative", "reason": "raw hreflang href is relative"}


def _placement(source: dict[str, Any] | None) -> dict[str, Any]:
    if source is None:
        return {"state": "unmeasured", "reason": "declaring page was not captured"}
    outside = source.get("hreflang_outside_head")
    if outside not in (0, 1, False, True):
        return {
            "state": "unmeasured",
            "reason": "captured page has no hreflang placement observation",
        }
    return {
        "state": "measured",
        "all_outside_head": bool(outside),
        "reason": (
            "all declarations are outside head"
            if outside
            else "at least one declaration is in head"
        ),
    }


def _source_id(item: dict[str, Any]) -> str:
    return (
        f"page:{item['source_page_url_id']}:document:{item['source_document_id']}:"
        f"representation:{item['representation']}:ordinal:{item['ordinal']}"
    )


def _language_derivations(
    language: list[dict[str, Any]],
    by_id: dict[int, dict[str, Any]],
    by_normalized: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Validate saved declaration-to-target relations within retained representations."""
    rows: list[dict[str, Any]] = []
    active_by_source: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for evidence in language:
        source = by_id.get(evidence["page_url_id"])
        source_active = bool(
            source
            and source.get("document_id") == evidence["source_document_id"]
            and evidence.get("state") != "unavailable"
        )
        declarations = (
            evidence.get("declarations") if isinstance(evidence.get("declarations"), list) else []
        )
        for declaration in declarations:
            if not isinstance(declaration, dict):
                continue
            item = {
                "source_page_url_id": evidence["page_url_id"],
                "source_url": source.get("url") if source else None,
                "source_document_id": evidence["source_document_id"],
                "representation": evidence["representation"],
                "ordinal": declaration.get("ordinal"),
                "source_declaration_id": "",
                "lang": str(declaration.get("lang") or ""),
                "raw_href": str(declaration.get("raw_href") or ""),
                "target": str(declaration.get("target") or ""),
                "declaration_state": declaration.get("state"),
                "source_representation_state": (
                    "active" if source_active else "superseded_or_unmeasured"
                ),
                "source_placement": _placement(source),
            }
            if type(item["ordinal"]) is not int or item["ordinal"] < 0:
                item["declaration_state"] = "malformed"
                item["source_declaration_id"] = "unavailable"
            else:
                item["source_declaration_id"] = _source_id(item)
            item["target_observation"] = _target(item["target"], by_normalized)
            item["code"] = {
                "state": "valid" if not code_error(item["lang"]) else "invalid",
                "reason": code_error(item["lang"]),
            }
            item["href"] = _relative(item["raw_href"])
            rows.append(item)
            if source_active and type(item["ordinal"]) is int:
                active_by_source.setdefault(
                    (item["source_page_url_id"], item["source_document_id"]), []
                ).append(item)

    active_edges = {
        (norm_url(item["source_url"]), norm_url(item["target"]))
        for group in active_by_source.values()
        for item in group
        if item["source_url"] and item["target"] and item["declaration_state"] == "declared"
    }
    active_sources = {
        norm_url(group[0]["source_url"])
        for group in active_by_source.values()
        if group
        and group[0]["source_url"]
        and any(item["declaration_state"] == "declared" for item in group)
    }
    for group in active_by_source.values():
        source_url = group[0]["source_url"]
        source_key = norm_url(source_url)
        declared = {
            norm_url(item["target"])
            for item in group
            if item["target"] and item["declaration_state"] == "declared"
        }
        fallback = any(
            item["lang"].casefold() == "x-default" and item["declaration_state"] == "declared"
            for item in group
        )
        for item in group:
            target_key = norm_url(item["target"]) if item["target"] else ""
            item["self_reference"] = {
                "state": "present" if source_key in declared else "missing",
                "reason": "" if source_key in declared else "no saved self-reference declaration",
            }
            item["fallback"] = {
                "state": "present" if fallback else "missing",
                "reason": "" if fallback else "no x-default declaration in this representation",
            }
            if item["target_observation"]["state"] != "observed":
                item["reciprocity"] = {
                    "state": "unmeasured",
                    "reason": "target page was not captured for a reciprocity check",
                }
            elif target_key not in active_sources:
                item["reciprocity"] = {
                    "state": "unmeasured",
                    "reason": "target has no active saved language declaration evidence",
                }
            elif (target_key, source_key) in active_edges:
                item["reciprocity"] = {"state": "present", "reason": ""}
            else:
                item["reciprocity"] = {
                    "state": "missing",
                    "reason": "target representation has no saved return declaration",
                }
    for item in rows:
        if "self_reference" not in item:
            unavailable = {
                "state": "unmeasured",
                "reason": "source representation is not the active captured page",
            }
            item["self_reference"] = unavailable
            item["fallback"] = dict(unavailable)
            item["reciprocity"] = dict(unavailable)
    return rows


def derive(con: Any, *, duplicate_threshold: float = 0.92) -> dict[str, Any]:
    """Return reproducible content/structured/i18n derivations from one scan connection."""
    from seohead.storage.content_evidence import derive_duplicates
    from seohead.storage.content_evidence import read as read_content
    from seohead.storage.structured_evidence import read as read_structured

    content = read_content(con)
    structured = read_structured(con)
    by_id, by_normalized = _page_index(con)
    duplicates = derive_duplicates(
        content["items"],
        threshold=duplicate_threshold,
        page_urls={page_id: page["url"] for page_id, page in by_id.items()},
    )
    declarations = _language_derivations(structured["language"], by_id, by_normalized)
    structured_states = dict(Counter(item.get("state") for item in structured["structured"]))
    return {
        "schema_version": "saved_corpus_derivations.v2",
        "duplicates": duplicates,
        "structured": {"states": structured_states, "items": structured["structured"]},
        "internationalization": {
            "declarations": declarations,
            "items": structured["language"],
            "target_population": "captured pages only; queued or linked URLs are unmeasured",
        },
    }
