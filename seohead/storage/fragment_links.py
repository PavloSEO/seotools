"""Broken in-page fragment detection over retained documents (issue #827).

A broken bookmark is not a broken HTTP link: the fragment in ``href="page#target"``
is resolved inside the *destination document* the browser actually receives.
This module evaluates every fragment-bearing ``<a href>`` found in retained
complete HTML/DOM bodies and answers, per occurrence, whether the fragment
identifies a target -- without ever fetching the destination.

Evidence contract:

- One lane per ``(page, representation)``: ``static``, ``rendered`` and
  ``legacy_fragment`` documents are evaluated independently and a link seen in
  one representation is checked only against destination evidence from that
  same representation. Nothing about one lane may mask another.
- Only ``body_state='complete'`` documents are evidence. A missing,
  incomplete, truncated, unsupported, failed or budget-exhausted body produces
  a named ``skipped`` occurrence (destination) or a named unavailable-source
  record (source) -- never a missing-target finding, because an unreadable
  body cannot prove an element absent.
- HTML only: a document whose media type is not exactly ``text/html`` cannot
  carry element-id fragment semantics (``application/xhtml+xml`` follows XML
  fragment rules instead), so its lane is skipped by name.
- An ``href`` the URL resolver refuses stays a ``skipped`` occurrence
  (``href_unresolvable``): dropping it would erase an unverified anchor and
  let coverage claim ``complete`` over a document that was not fully read.
- ``<template>`` content is inert (see parser's ``_INERT_LINK_CONTAINERS``):
  anchors and ``id``/``name`` attributes inside it are never measured.
- Effective ``<base href>`` resolves relative hrefs, mirroring browser URL
  resolution; the destination is then compared fragment-free under the scan's
  own URL identity (scheme/host/path/query, path defaulting to ``/`` -- a
  different query is a different document). Stored ``final_url``/``redirect_url``
  hops are followed within the retained corpus, bounded, because the browser
  resolves the fragment in the document it finally lands on.

Fragment matching follows the WHATWG HTML "scroll to the fragment" algorithm:

1. An empty fragment (``href="#"``) is the valid top-of-document target.
2. An element ``id`` exactly equal to the *serialized* fragment wins first,
   then a legacy ``<a name>`` equal to it (case-sensitive, first match in
   tree order -- duplicates never multiply a match nor a finding).
3. The fragment is percent-decoded, then UTF-8 decoded without BOM
   (``errors='replace'``); an element ``id`` equal to the decoded value wins
   next, then a legacy ``<a name>`` equal to it. ``+`` is a literal plus,
   never a space (this is URL percent-decoding, not form decoding); malformed
   ``%`` sequences pass through unchanged.
4. A decoded value that ASCII-case-insensitively equals ``top`` resolves to
   the top of the document.
5. Otherwise the fragment is missing -- the only state that can become a
   finding.

Fragments carrying a ``:~:`` fragment directive (scroll-to-text and friends)
are named skips unless their element part already resolves: the directive
itself is searched as page text by the browser and cannot be verified from the
retained DOM inventory.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote_to_bytes, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from . import ScanError
from .bodies import read_document

EVALUATION_VERSION = "fragment_links.v1"

_REP_ORDER = ("static", "rendered", "legacy_fragment")
_REP_RANK = {name: index for index, name in enumerate(_REP_ORDER)}

# Per-document extraction bounds. Anchors and id/name targets are counted
# before they are stored, so a page that declares more is reported as
# truncated rather than silently trimmed.
MAX_ANCHORS_PER_DOCUMENT = 10_000
MAX_TARGETS_PER_DOCUMENT = 100_000
# Whole-evaluation bound on materialized occurrences, and on the named
# unavailable-source records kept for inspection.
MAX_OCCURRENCES = 100_000
MAX_UNAVAILABLE_SOURCES = 500
# Stored redirect hops followed while resolving a destination inside the
# retained corpus. A browser's own redirect limit is higher, but a retained
# chain this long is already unusable evidence.
MAX_REDIRECT_HOPS = 8
DEFAULT_MAX_DECODED_BYTES = 8 * 1024 * 1024

_SKIP = "skipped"
_RESOLVED = "resolved"
_MISSING = "missing"


def _canonical_key(url: str) -> str:
    """The scan's fragment-free URL identity; mirrors ``crawl.spider._canonical_key``.

    Returns ``""`` for input ``urlsplit`` rejects, so a malformed stored URL
    degrades to "does not match any page" instead of aborting the evaluation.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _scheme(url: str) -> str:
    try:
        return urlsplit(url).scheme
    except ValueError:
        return ""


def _decode_fragment(fragment: str) -> str:
    """Percent-decode, then UTF-8 decode without BOM, per the HTML algorithm.

    ``unquote_to_bytes`` is the URL percent-decoder, not the form decoder: a
    ``+`` stays a literal plus, and a malformed ``%`` sequence is carried
    through unchanged. Invalid UTF-8 becomes U+FFFD, matching the standard's
    replacement behaviour, so this never fails.
    """
    return unquote_to_bytes(fragment).decode("utf-8", errors="replace")


def _ascii_case_top(value: str) -> bool:
    """ASCII-case-insensitive ``top`` -- not Unicode ``lower()``, which folds more."""
    return len(value) == 3 and value.lower() == "top" and value.isascii()


def _indicated_element(
    fragment: str, ids: frozenset[str], names: frozenset[str]
) -> tuple[str, str] | None:
    """WHATWG "find a potential indicated element" for an id/name inventory.

    The caller handles the empty fragment. Returns ``(kind, value)`` for the
    winning candidate or ``None`` when no target exists.
    """
    decoded = _decode_fragment(fragment)
    # Select-the-indicated-part: the serialized fragment is tried against
    # both element ids and legacy <a name> values; only when that finds
    # nothing is the decoded fragment tried against both again.
    if fragment in ids:
        return "element_id", fragment
    if fragment in names:
        return "anchor_name", fragment
    if decoded in ids:
        return "element_id", decoded
    if decoded in names:
        return "anchor_name", decoded
    if _ascii_case_top(decoded):
        return "top", "top"
    return None


@dataclass
class _DocEvidence:
    """Parsed retained document: fragment anchors plus the id/name inventory."""

    anchors: list[dict[str, Any]] = field(default_factory=list)
    anchors_omitted: int = 0
    ids: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()
    targets_truncated: bool = False
    base_url: str = ""


def _is_html(content_type: str) -> bool:
    """Strict ``text/html`` media-type test -- parameters and case ignored.

    Element-id fragment semantics are defined for ``text/html`` documents;
    ``application/xhtml+xml`` follows XML fragment rules instead, and a
    substring check would also wave through arbitrary types such as
    ``application/nothtml``.
    """
    return content_type.split(";", 1)[0].strip().lower() == "text/html"


def _extract(html: str, final_url: str) -> _DocEvidence:
    """One parse pass: fragment anchors, element ids and ``<a name>`` targets."""
    from seohead.tools.parser import document_base_url, is_inert_template_content

    evidence = _DocEvidence()
    evidence.base_url = document_base_url(html, final_url)
    soup = BeautifulSoup(html, "lxml")
    ids: list[str] = []
    names: list[str] = []
    targets = 0
    for tag in soup.find_all(True):
        if is_inert_template_content(tag):
            continue
        element_id = tag.get("id")
        if element_id is not None:
            if targets < MAX_TARGETS_PER_DOCUMENT:
                ids.append(element_id)
                targets += 1
            else:
                evidence.targets_truncated = True
        if tag.name == "a":
            name = tag.get("name")
            if name is not None:
                if targets < MAX_TARGETS_PER_DOCUMENT:
                    names.append(name)
                    targets += 1
                else:
                    evidence.targets_truncated = True
            href = tag.get("href")
            if href is None:
                continue
            if len(evidence.anchors) >= MAX_ANCHORS_PER_DOCUMENT:
                evidence.anchors_omitted += 1
                continue
            unresolvable = False
            try:
                resolved = urljoin(evidence.base_url, href)
            except ValueError:
                resolved = ""
                unresolvable = True
            if "#" in resolved:
                fragment = resolved.split("#", 1)[1]
            elif unresolvable and "#" in href:
                # The resolver refused the href, but it still visibly carries
                # a fragment. Keep it as an unmeasurable occurrence: dropping
                # it would report the document as holding no unverified
                # anchors, which a complete coverage state would then imply.
                fragment = href.split("#", 1)[1]
            else:
                continue
            evidence.anchors.append(
                {
                    "ordinal": len(evidence.anchors),
                    "raw_href": href,
                    "resolved_url": resolved,
                    "fragment": fragment,
                    "unresolvable": unresolvable,
                }
            )
    evidence.ids = frozenset(ids)
    evidence.names = frozenset(names)
    return evidence


@dataclass
class _Lane:
    """One (page, representation) evaluation lane."""

    page_ordinal: int
    url: str
    url_id: int
    representation: str
    document: dict[str, Any] | None
    is_html: bool


def _document_rows(con: sqlite3.Connection) -> dict[tuple[int, str], dict[str, Any]]:
    """The active retained document per (url_id, representation).

    Selection order is the same one the body-diff reader uses: the page's own
    selected document, then the resource-inventory-marked static document,
    then the newest -- so every consumer of "which document would be read"
    agrees.
    """
    rows = con.execute(
        "SELECT d.*, r.content_type AS response_content_type, "
        "(p.document_id=d.document_id) AS selected, "
        "(i.item_key IS NOT NULL) AS inventory_marked "
        "FROM documents d "
        "LEFT JOIN responses r ON r.response_id=d.source_response_id "
        "LEFT JOIN pages p ON p.url_id=d.url_id "
        "LEFT JOIN context_items i ON i.kind='resource_inventory' "
        "AND i.item_key='document:'||d.document_id "
        "ORDER BY d.url_id,d.representation,selected DESC,inventory_marked DESC,"
        "d.document_id DESC"
    ).fetchall()
    active: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["url_id"], row["representation"])
        if key not in active:
            active[key] = dict(row)
    return active


def _document_content_type(document: dict[str, Any], page_content_type: str) -> str:
    return document.get("response_content_type") or page_content_type or ""


def _document_final_url(con: sqlite3.Connection, document: dict[str, Any], fallback: str) -> str:
    """The URL the retained document was served at; the page URL when unknown."""
    try:
        if document["representation"] == "static":
            response_id = document.get("source_response_id")
            if type(response_id) is not int:
                return fallback
            row = con.execute(
                "SELECT u.url FROM responses r JOIN urls u ON u.url_id=r.effective_url_id "
                "WHERE r.response_id=?",
                (response_id,),
            ).fetchone()
        else:
            try:
                renderer = json.loads(document.get("renderer_json") or "{}")
            except (TypeError, ValueError):
                renderer = {}
            final_url_id = renderer.get("final_url_id") if isinstance(renderer, dict) else None
            if type(final_url_id) is not int:
                navigation_url_id = (
                    renderer.get("navigation_url_id") if isinstance(renderer, dict) else None
                )
                final_url_id = navigation_url_id if type(navigation_url_id) is int else None
            if type(final_url_id) is not int:
                return fallback
            row = con.execute("SELECT url FROM urls WHERE url_id=?", (final_url_id,)).fetchone()
    except sqlite3.Error:
        return fallback
    return str(row[0]) if row is not None and row[0] else fallback


def evaluate(
    con: sqlite3.Connection,
    *,
    max_decoded_bytes: int = DEFAULT_MAX_DECODED_BYTES,
    max_occurrences: int = MAX_OCCURRENCES,
) -> dict[str, Any]:
    """Evaluate every fragment-bearing anchor in retained complete HTML/DOM bodies.

    Read-only, offline and deterministic: occurrences are ordered by source
    page ordinal, representation rank and in-document anchor order, so two
    runs over the same artifact return the same sequence. ``con`` must already
    be a validated scan connection (``open_scan`` or an open ``NativeScan``).
    """
    if not isinstance(con, sqlite3.Connection):
        raise TypeError("fragment evaluation requires a SQLite scan connection")
    pages = [
        dict(row)
        for row in con.execute(
            "SELECT p.url_id,p.page_ordinal,p.document_id,p.redirect_url,p.final_url,"
            "p.content_type,p.status_code,u.url FROM pages p JOIN urls u USING(url_id) "
            "ORDER BY p.page_ordinal"
        )
    ]
    active = _document_rows(con)
    page_by_key: dict[str, dict[str, Any]] = {}
    for page in pages:
        page_by_key.setdefault(_canonical_key(page["url"]), page)

    documents: dict[int, _DocEvidence] = {}
    unavailable_sources: list[dict[str, Any]] = []
    unavailable_omitted = 0
    lane_counts = {"evaluated": 0, "unavailable": 0, "non_html": 0}
    occurrences: list[dict[str, Any]] = []
    occurrences_omitted = 0

    def get_evidence(document: dict[str, Any], final_url: str) -> _DocEvidence | None:
        document_id = document["document_id"]
        if document_id in documents:
            return documents[document_id]
        try:
            html = read_document(con, int(document_id), max_decoded_bytes=max_decoded_bytes)
        except ScanError:
            return None
        evidence = _extract(html, final_url)
        documents[document_id] = evidence
        return evidence

    def record_unavailable(url: str, representation: str, reason: str) -> None:
        nonlocal unavailable_omitted
        lane_counts["unavailable"] += 1
        if len(unavailable_sources) < MAX_UNAVAILABLE_SOURCES:
            unavailable_sources.append(
                {"url": url, "representation": representation, "reason": reason}
            )
        else:
            unavailable_omitted += 1

    def destination(
        dest_url: str, representation: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str, str]:
        """Resolve a fragment-free URL to its retained document.

        Returns ``(page, document, final_url, skip_reason)``; on a skip
        ``page``/``document`` may be None, ``final_url`` names the last URL
        the retained chain reached, and ``skip_reason`` is a slug.
        """
        page = page_by_key.get(_canonical_key(dest_url))
        if page is None:
            return None, None, "", "destination_not_in_scan"
        seen = {page["url_id"]}
        hops = 0
        while True:
            # Walk the recorded hops like the browser does. ``redirect_url``
            # is the first hop (always set on a stored 3xx); ``final_url`` is
            # the probed chain end, kept as a fallback for records written
            # without hop detail.
            nxt = page["redirect_url"] or page["final_url"]
            if not nxt:
                break
            nxt_page = page_by_key.get(_canonical_key(nxt))
            if nxt_page is None:
                return page, None, nxt, "destination_redirect_target_absent"
            if nxt_page["url_id"] == page["url_id"]:
                # A redirect to itself never lands on a document.
                return page, None, "", "destination_redirect_unresolved"
            if nxt_page["url_id"] in seen or hops >= MAX_REDIRECT_HOPS:
                return page, None, "", "destination_redirect_unresolved"
            seen.add(nxt_page["url_id"])
            page = nxt_page
            hops += 1
        document = active.get((page["url_id"], representation))
        if document is None:
            return page, None, page["url"], "destination_document_absent"
        if not _is_html(_document_content_type(document, page["content_type"])):
            return page, document, page["url"], "destination_not_html"
        if document["body_state"] != "complete" or document.get("body_sha256") is None:
            reason = f"destination_body_{document['body_state']}"
            return page, document, page["url"], reason
        return page, document, page["url"], ""

    for page in pages:
        lanes = [
            (representation, active[(page["url_id"], representation)])
            for representation in _REP_ORDER
            if (page["url_id"], representation) in active
        ]
        if not lanes:
            # A page with no retained document at all is still named, so a
            # report can see that part of the crawl carried no HTML evidence.
            if _is_html(page["content_type"] or ""):
                record_unavailable(page["url"], "page", "document_absent")
            else:
                lane_counts["non_html"] += 1
            continue
        for representation, document in lanes:
            final_url = _document_final_url(con, document, page["url"])
            if not _is_html(_document_content_type(document, page["content_type"])):
                lane_counts["non_html"] += 1
                continue
            if document["body_state"] != "complete" or document.get("body_sha256") is None:
                record_unavailable(
                    page["url"],
                    representation,
                    f"body_{document['body_state']}/{document['body_reason']}",
                )
                continue
            evidence = get_evidence(document, final_url)
            if evidence is None:
                record_unavailable(page["url"], representation, "body_read_failed")
                continue
            lane_counts["evaluated"] += 1
            for anchor in evidence.anchors:
                fragment = anchor["fragment"]
                base_occurrence = {
                    "source_url": page["url"],
                    "source_representation": representation,
                    "ordinal": anchor["ordinal"],
                    "raw_href": anchor["raw_href"],
                    "resolved_url": anchor["resolved_url"],
                    "fragment": fragment,
                    "decoded_fragment": _decode_fragment(fragment),
                    "destination_url": anchor["resolved_url"].split("#", 1)[0],
                    "destination_document_url": None,
                    "destination_representation": representation,
                }
                if len(occurrences) >= max_occurrences:
                    occurrences_omitted += 1
                    continue
                if anchor["unresolvable"]:
                    occurrences.append(
                        base_occurrence
                        | {
                            "state": _SKIP,
                            "match": None,
                            "reason": "href_unresolvable",
                            "note": "",
                        }
                    )
                    continue
                dest_url = base_occurrence["destination_url"]
                if _scheme(dest_url) not in {"http", "https"}:
                    occurrences.append(
                        base_occurrence
                        | {
                            "state": _SKIP,
                            "match": None,
                            "reason": "destination_scheme_unsupported",
                            "note": "",
                        }
                    )
                    continue
                dest_page, dest_document, landing_url, skip_reason = destination(
                    dest_url, representation
                )
                if dest_document is None or skip_reason:
                    occurrences.append(
                        base_occurrence
                        | {
                            "state": _SKIP,
                            "match": None,
                            "reason": skip_reason,
                            "note": ""
                            if dest_document is None
                            else str(dest_document["body_reason"]),
                            "destination_document_url": landing_url or None,
                        }
                    )
                    continue
                assert dest_page is not None
                dest_evidence = get_evidence(
                    dest_document,
                    _document_final_url(con, dest_document, dest_page["url"]),
                )
                if dest_evidence is None:
                    occurrences.append(
                        base_occurrence
                        | {
                            "state": _SKIP,
                            "match": None,
                            "reason": "destination_body_read_failed",
                            "note": "",
                            "destination_document_url": landing_url,
                        }
                    )
                    continue
                base_occurrence["destination_document_url"] = landing_url
                # A fragment directive (#:~:text=...) is evaluated on its
                # element part; the directive itself is a text search the
                # retained DOM inventory cannot verify.
                if ":~:" in fragment:
                    element_part = fragment.split(":~:", 1)[0]
                    match = (
                        _indicated_element(element_part, dest_evidence.ids, dest_evidence.names)
                        if element_part
                        else None
                    )
                    if match is not None:
                        occurrences.append(
                            base_occurrence
                            | {
                                "state": _RESOLVED,
                                "match": {"kind": match[0], "value": match[1]},
                                "reason": "",
                                "note": "fragment_directive_unverified",
                            }
                        )
                    else:
                        occurrences.append(
                            base_occurrence
                            | {
                                "state": _SKIP,
                                "match": None,
                                "reason": "fragment_directive_unverified",
                                "note": "",
                            }
                        )
                    continue
                if not fragment:
                    match_result: tuple[str, str] | None = ("top", "")
                else:
                    match_result = _indicated_element(
                        fragment, dest_evidence.ids, dest_evidence.names
                    )
                if match_result is not None:
                    occurrences.append(
                        base_occurrence
                        | {
                            "state": _RESOLVED,
                            "match": {"kind": match_result[0], "value": match_result[1]},
                            "reason": "",
                            "note": "",
                        }
                    )
                elif dest_evidence.targets_truncated:
                    occurrences.append(
                        base_occurrence
                        | {
                            "state": _SKIP,
                            "match": None,
                            "reason": "destination_target_inventory_truncated",
                            "note": "",
                        }
                    )
                else:
                    occurrences.append(
                        base_occurrence
                        | {
                            "state": _MISSING,
                            "match": None,
                            "reason": "no_matching_fragment_target",
                            "note": "",
                        }
                    )

    ids_total = sum(len(doc.ids) for doc in documents.values())
    names_total = sum(len(doc.names) for doc in documents.values())
    anchors_total = sum(len(doc.anchors) + doc.anchors_omitted for doc in documents.values())
    anchors_omitted = sum(doc.anchors_omitted for doc in documents.values())
    state_counts = {
        state: sum(1 for item in occurrences if item["state"] == state)
        for state in (_RESOLVED, _MISSING, _SKIP)
    }
    skip_reasons: dict[str, int] = {}
    for item in occurrences:
        if item["state"] == _SKIP:
            skip_reasons[item["reason"]] = skip_reasons.get(item["reason"], 0) + 1
    # A skipped occurrence is an unanswered link: an absent, non-HTML,
    # incomplete or unreadable destination can never prove its target
    # inventory, so coverage stays partial rather than reading complete.
    partial = bool(
        anchors_omitted
        or occurrences_omitted
        or unavailable_omitted
        or any(doc.targets_truncated for doc in documents.values())
        or lane_counts["unavailable"]
        or skip_reasons
    )
    return {
        "analysis": EVALUATION_VERSION,
        "occurrences": occurrences,
        "states": state_counts,
        "coverage": {
            "state": "partial" if partial else "complete",
            "source_documents_evaluated": lane_counts["evaluated"],
            "source_documents_unavailable": lane_counts["unavailable"],
            "source_documents_non_html": lane_counts["non_html"],
            "unavailable_sources": unavailable_sources,
            "unavailable_sources_omitted": unavailable_omitted,
            "anchors_seen": anchors_total,
            "anchors_omitted": anchors_omitted,
            "occurrences_seen": len(occurrences) + occurrences_omitted,
            "occurrences_omitted": occurrences_omitted,
            "occurrences_skipped": state_counts[_SKIP],
            "skip_reasons": dict(sorted(skip_reasons.items())),
            "target_ids_recorded": ids_total,
            "target_names_recorded": names_total,
            "target_inventory_truncated_documents": sum(
                1 for doc in documents.values() if doc.targets_truncated
            ),
            "limits": {
                "max_decoded_bytes": max_decoded_bytes,
                "max_occurrences": max_occurrences,
                "max_anchors_per_document": MAX_ANCHORS_PER_DOCUMENT,
                "max_targets_per_document": MAX_TARGETS_PER_DOCUMENT,
            },
        },
    }


def findings(evaluation: dict[str, Any]) -> list[dict[str, Any]]:
    """Group ``missing`` occurrences into bounded audit-finding records.

    One record per ``(destination document, representation, fragment)``: the
    deterministic group a specialist would fix once. ``target_url`` stays
    fragment-free so the retained page/document evidence binding still
    resolves; source URLs land in ``locations`` and are counted in full.
    """
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in evaluation["occurrences"]:
        if item["state"] != _MISSING:
            continue
        destination = item["destination_document_url"] or item["destination_url"]
        key = (destination, item["destination_representation"], item["fragment"])
        group = groups.get(key)
        location = {
            "source_url": item["source_url"],
            "source_representation": item["source_representation"],
            "raw_href": item["raw_href"],
            "resolved_url": item["resolved_url"],
            "ordinal": item["ordinal"],
        }
        if group is None:
            group = {
                "target_url": destination,
                "fragment": item["fragment"],
                "decoded_fragment": item["decoded_fragment"],
                "destination_representation": item["destination_representation"],
                "occurrences_count": 0,
                "locations": [],
                "locations_omitted": 0,
            }
            groups[key] = group
        group["occurrences_count"] += 1
        if len(group["locations"]) < 100:
            group["locations"].append(location)
        else:
            group["locations_omitted"] += 1
    return list(groups.values())


def paginate(
    evaluation: dict[str, Any],
    *,
    offset: int = 0,
    limit: int = 100,
    state: str | None = None,
    representation: str | None = None,
) -> dict[str, Any]:
    """Deterministic offset/limit slice over the evaluated occurrences."""
    items = evaluation["occurrences"]
    if state is not None:
        items = [item for item in items if item["state"] == state]
    if representation is not None:
        items = [item for item in items if item["source_representation"] == representation]
    total = len(items)
    page = items[offset : offset + limit]
    next_offset = offset + len(page) if offset + len(page) < total else None
    return {
        "analysis": evaluation["analysis"],
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(page),
        "next_offset": next_offset,
        "states": evaluation["states"],
        "coverage": evaluation["coverage"],
        "occurrences": page,
    }
