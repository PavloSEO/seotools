"""Generate a connected Schema.org ``@graph`` from observable page facts.

For each classified page type, the builder proposes an ``@id``-connected graph
such as ``Organization <- WebSite <- WebPage <- Article``. Many validators inspect
isolated blocks; this module assembles the complete graph so its connectivity and
entity reuse can be evaluated.

One rule is strict: **mark up only facts visible on the page**. No observed fact
means no generated field. The builder does not invent ``logo`` when no logo is
present or add ``offers`` without a price. A short truthful graph is preferable
to a complete-looking graph that describes nonexistent content.

The resulting graph is intended to be passed immediately to
``schema.check_schema``. Generator and validator form a pair: build the graph,
validate it, and report what remains missing.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from seohead.tools import page_facts, page_type

# Stable IDs for entities the builder can support from reliably extracted facts.
# Unsupported fields are omitted rather than encouraging unverifiable markup.
_ORG_ID = "#organization"
_SITE_ID = "#website"
_PAGE_ID = "#webpage"
_CRUMBS_ID = "#breadcrumb"


def _first(*vals: Any) -> Any:
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def _site_origin(url: str) -> str:
    parts = urlparse(url)
    if not parts.scheme or not parts.netloc:
        return url
    return f"{parts.scheme}://{parts.netloc}"


def _image(facts: dict[str, Any]) -> str | None:
    og = facts.get("og") or {}
    return _first(og.get("og:image"), og.get("og:image:url"), og.get("og:image:secure_url"))


def build_organization(facts: dict[str, Any], base_url: str) -> dict[str, Any] | None:
    """Build Organization only when a name or logo proves that it exists."""
    org = facts.get("organization") or {}
    name = _first(org.get("name"))
    logo = _first(org.get("logo"))
    if not name and not logo:
        return None
    node: dict[str, Any] = {"@type": "Organization", "@id": _ORG_ID, "url": _site_origin(base_url)}
    if name:
        node["name"] = name
    if logo:
        node["logo"] = logo
    same_as = facts.get("same_as") or []
    if same_as:
        node["sameAs"] = same_as[:10]
    tel = _first(org.get("telephone"))
    if tel:
        node["telephone"] = tel
    addr = _first(org.get("address"))
    if addr:
        node["address"] = addr
    return node


def build_website(facts: dict[str, Any], base_url: str, org_id: str | None) -> dict[str, Any]:
    og = facts.get("og") or {}
    name = _first(og.get("og:site_name"), facts.get("title"))
    site: dict[str, Any] = {
        "@type": "WebSite",
        "@id": _SITE_ID,
        "url": _site_origin(base_url),
    }
    if name:
        site["name"] = name
    if org_id:
        site["publisher"] = {"@id": org_id}
    return site


def build_breadcrumb(facts: dict[str, Any]) -> dict[str, Any] | None:
    crumbs = facts.get("breadcrumbs") or []
    if not crumbs:
        return None
    elements = []
    for i, item in enumerate(crumbs, 1):
        entry: dict[str, Any] = {"@type": "ListItem", "position": i, "name": item["name"]}
        if item.get("url"):
            entry["item"] = item["url"]
        elements.append(entry)
    return {"@type": "BreadcrumbList", "@id": _CRUMBS_ID, "itemListElement": elements}


def _offer(facts: dict[str, Any]) -> dict[str, Any] | None:
    price = facts.get("price")
    if price is None or price.get("value") is None:
        return None
    # A whole amount belongs in the markup as 19900, not 19900.0.
    value = price["value"]
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    offer: dict[str, Any] = {"@type": "Offer", "price": value}
    if price.get("currency"):
        offer["priceCurrency"] = price["currency"]
    return offer


def _aggregate_rating(facts: dict[str, Any]) -> dict[str, Any] | None:
    rating = facts.get("rating")
    if rating is None or rating.get("value") is None:
        return None
    node: dict[str, Any] = {"@type": "AggregateRating", "ratingValue": rating["value"]}
    if rating.get("count"):
        node[("reviewCount" if rating.get("source") == "microdata" else "ratingCount")] = rating[
            "count"
        ]
    return node


def _common_page_fields(facts: dict[str, Any], page_id: str, site_id: str | None) -> dict[str, Any]:
    """Build common page fields using only facts that are actually present."""
    node: dict[str, Any] = {"@id": page_id, "url": facts.get("canonical") or facts.get("url")}
    name = _first(facts.get("h1"), facts.get("title"))
    if name:
        node["name"] = name
    desc = facts.get("description")
    if desc:
        node["description"] = desc
    img = _image(facts)
    if img:
        node["image"] = img
    if site_id:
        node["isPartOf"] = {"@id": site_id}
    return node


def build_entity(
    facts: dict[str, Any], ptype: dict[str, Any], site_id: str | None, org_id: str | None
) -> dict[str, Any]:
    """Build the content-page entity with type-specific properties."""
    t = ptype["inferred_type"]
    node = _common_page_fields(facts, _PAGE_ID, site_id)
    org = facts.get("organization") or {}
    offer = _offer(facts)
    rating = _aggregate_rating(facts)
    pub = facts.get("published_time")
    mod = facts.get("modified_time")
    author = facts.get("author_rel")

    if t in ("Article", "NewsArticle", "BlogPosting"):
        node["@type"] = "Article"
        # Article uses headline instead of name; dates and author support rich results.
        if "name" in node:
            node["headline"] = node.pop("name")
        if pub:
            node["datePublished"] = pub
        if mod:
            node["dateModified"] = mod
        # Map rel=author to Person; use Organization separately as publisher.
        if author:
            node["author"] = {"@type": "Person", "url": author}
        if org_id:
            node["publisher"] = {"@id": org_id}
    elif t == "Product":
        node["@type"] = "Product"
        brand = _first(org.get("name"))
        if brand:
            node["brand"] = {"@type": "Brand", "name": brand}
        if offer:
            node["offers"] = offer
        if rating:
            node["aggregateRating"] = rating
    elif t == "Service":
        node["@type"] = "Service"
        if org_id:
            node["provider"] = {"@id": org_id}
        if facts.get("h1"):
            node["serviceType"] = facts["h1"]
        if offer:
            node["offers"] = offer
    elif t == "LocalBusiness":
        node["@type"] = "LocalBusiness"
        if _first(org.get("address")):
            node["address"] = org["address"]
        if _first(org.get("telephone")):
            node["telephone"] = org["telephone"]
        if rating:
            node["aggregateRating"] = rating
    elif t == "FAQPage":
        # mainEntity requires real questions and answers from page content. This
        # extractor does not recover them reliably, so emit an honest shell and
        # let the validator report missing rich-result fields rather than inventing Q&A.
        node["@type"] = "FAQPage"
    elif t == "Recipe":
        node["@type"] = "Recipe"
    elif t == "Event":
        node["@type"] = "Event"
    elif t == "VideoObject":
        node["@type"] = "VideoObject"
    elif t == "Course":
        node["@type"] = "Course"
    elif t == "JobPosting":
        node["@type"] = "JobPosting"
    else:
        node["@type"] = "WebPage"

    return node


def build_graph(
    url: str, facts: dict[str, Any], ptype: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build ``Organization <- WebSite <- WebPage <- <content type>``.

    ``ptype`` may be omitted, in which case ``page_type.classify`` infers it from
    the supplied facts. A property is added only when corresponding evidence exists.
    """
    if ptype is None:
        ptype = page_type.classify(url, facts)
    base = facts.get("canonical") or url

    org = build_organization(facts, base)
    org_id = org["@id"] if org else None
    site = build_website(facts, base, org_id)
    entity = build_entity(facts, ptype, site["@id"], org_id)

    graph: list[dict[str, Any]] = []
    if org:
        graph.append(org)
    graph.append(site)
    graph.append(entity)

    crumbs = build_breadcrumb(facts)
    if crumbs:
        # Link breadcrumbs to the page so they participate in the same graph.
        entity["breadcrumb"] = {"@id": _CRUMBS_ID}
        graph.append(crumbs)

    return {"@context": "https://schema.org", "@graph": graph}


# -- Property-level diff against existing markup ------------------------------

# Recommended fields worth highlighting when absent from existing JSON-LD. They
# reflect practical rich-result guidance and coherent graph relationships.
_RECOMMENDED = {
    "Article": ["headline", "datePublished", "author", "image"],
    "Product": ["name", "image", "offers", "aggregateRating", "description"],
    "Service": ["name", "provider", "offers"],
    "LocalBusiness": ["name", "address", "telephone", "aggregateRating"],
    "WebPage": ["name", "isPartOf"],
}


def _existing_keys(facts: dict[str, Any], target_type: str) -> set[str]:
    """Return properties already declared for the target type in existing JSON-LD."""

    from seohead.tools.schema_org import _flatten, _types_of  # type: ignore[attr-defined]

    keys: set[str] = set()
    nodes: list[dict[str, Any]] = []
    for block in facts.get("existing_jsonld") or []:
        _flatten(block, nodes)
    for n in nodes:
        if target_type in _types_of(n):
            # "_"-prefixed keys are ``_flatten``'s own bookkeeping (source path,
            # containment, vocabulary), never a declared Schema.org property.
            keys |= {k for k in n if not k.startswith("@") and not k.startswith("_")}
    return keys


# -- Value-level diff: observed HTML/graph versus existing JSON-LD -------------
_MISSING = object()  # Missing key is distinct from an explicit null value.


def _flatten_typed_entities(blocks: list[Any]) -> list[dict[str, Any]]:
    """Return typed entities from existing JSON-LD.

    Reuses ``schema._flatten`` but removes supporting objects without ``@type``;
    treating those objects as entities would create noisy diffs.
    """
    from seohead.tools.schema_org import _flatten, _types_of  # type: ignore[attr-defined]

    nodes: list[dict[str, Any]] = []
    for block in blocks:
        _flatten(block, nodes)
    return [n for n in nodes if _types_of(n)]


def _is_reference(v: Any) -> bool:
    return isinstance(v, dict) and isinstance(v.get("@id"), str)


def _stringify_diff(v: Any) -> str:
    if _is_reference(v):
        return v["@id"]
    if isinstance(v, dict):
        import json

        return json.dumps(v, sort_keys=True, ensure_ascii=False)
    return str(v)


def _values_match(a: Any, b: Any) -> bool:
    """Compare values using conservative, ordered rules.

    Accept exact equality, trimmed string equality, ``@id`` reference equality,
    and equal-length arrays as multisets. Nested objects without ``@id`` are
    deliberately not compared because a first-pass deep diff would be too noisy.
    """
    if a == b:
        return True
    if isinstance(a, str) and isinstance(b, str):
        return a.strip() == b.strip()
    if _is_reference(a) and _is_reference(b):
        return a["@id"] == b["@id"]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return sorted(_stringify_diff(x) for x in a) == sorted(_stringify_diff(x) for x in b)
    return False


def diff_values(suggested: dict[str, Any], existing_jsonld: list[Any]) -> dict[str, Any]:
    """Compare proposed and existing JSON-LD values, not merely property presence.

    Returns three explicit categories: missing entity, missing property, and
    property mismatch. If existing JSON-LD is empty, ``no_jsonld`` is returned and
    value-level findings remain empty because no live entity can be matched.
    """
    live = _flatten_typed_entities(existing_jsonld or [])
    if not live:
        return {
            "no_jsonld": True,
            "missing_entities": [],
            "missing_properties": [],
            "property_mismatches": [],
        }

    live_by_id: dict[str, dict[str, Any]] = {}
    live_by_type: dict[str, list[dict[str, Any]]] = {}
    for n in live:
        if isinstance(n.get("@id"), str):
            live_by_id[n["@id"]] = n
        for t in n.get("__types__") or _node_types(n):
            live_by_type.setdefault(t, []).append(n)

    matched: set[int] = set()
    missing_entities: list[dict[str, Any]] = []
    missing_properties: list[dict[str, Any]] = []
    property_mismatches: list[dict[str, Any]] = []

    for rec in suggested.get("@graph", []):
        rec_types = _node_types(rec)
        if not rec_types:
            continue
        primary = rec_types[0]
        rec_id = rec.get("@id") if isinstance(rec.get("@id"), str) else None

        # Match by @id first, then by the first unmatched entity of a shared type.
        match = live_by_id.get(rec_id) if rec_id else None
        if match is None:
            for t in rec_types:
                for cand in live_by_type.get(t, []):
                    if id(cand) not in matched:
                        match = cand
                        break
                if match is not None:
                    break
        if match is None:
            missing_entities.append({"type": primary, "id": rec_id})
            continue
        matched.add(id(match))

        for key, rec_val in rec.items():
            if key.startswith("@") or key in ("_path", "isPartOf") or rec_val is None:
                continue
            live_val = match.get(key, _MISSING)
            if live_val is _MISSING:
                missing_properties.append({"type": primary, "id": rec_id, "property": key})
            elif not _values_match(rec_val, live_val):
                property_mismatches.append(
                    {
                        "type": primary,
                        "id": rec_id,
                        "property": key,
                        "suggested": _short(rec_val),
                        "actual": _short(live_val),
                    }
                )

    return {
        "no_jsonld": False,
        "missing_entities": missing_entities,
        "missing_properties": missing_properties,
        "property_mismatches": property_mismatches,
    }


def _node_types(node: dict[str, Any]) -> list[str]:
    """Return node types through ``schema._types_of`` without internal caches."""
    from seohead.tools.schema_org import _types_of  # type: ignore[attr-defined]

    return _types_of(node)


def _short(v: Any) -> str:
    s = _stringify_diff(v)
    return s if len(s) <= 80 else s[:77] + "..."


def diff_vs_existing(
    facts: dict[str, Any], suggested: dict[str, Any], ptype: dict[str, Any]
) -> dict[str, Any]:
    """Report what the proposed graph adds to existing page markup.

    The first layer compares property presence, identifying missing recommended
    fields and fields that observed facts can fill immediately. ``entity_diff``
    then compares values to distinguish an absent entity, an absent property, and
    a value that contradicts the page's observable facts.
    """
    t = ptype["inferred_type"]
    existing = _existing_keys(facts, t)
    # Properties the proposed graph can populate from an observed fact.
    graph_entity = {}
    for node in suggested.get("@graph", []):
        if node.get("@id") == _PAGE_ID:
            graph_entity = node
            break
    can_fill = {k for k in graph_entity if not k.startswith("@") and k != "isPartOf"}
    recommended = set(_RECOMMENDED.get(t, []))

    missing_recommended = sorted(recommended - existing)
    addable_now = sorted((recommended & can_fill) - existing)
    already_ok = sorted(recommended & existing)

    return {
        "type": t,
        "missing_recommended": missing_recommended,
        "addable_now": addable_now,  # Missing fields that can be populated now.
        "already_present": already_ok,
        "entity_diff": diff_values(suggested, facts.get("existing_jsonld") or []),
    }


def build_schema(
    url: str | None = None,
    html: str | None = None,
    timeout: float = 25.0,
    override_type: str | None = None,
) -> dict[str, Any]:
    """Run URL/HTML -> facts -> type -> proposed graph -> existing-markup diff.

    ``override_type`` lets a caller choose the type when classification confidence
    is low. The result includes the inferred type and its evidence, the connected
    graph proposal, and additions or corrections for existing markup.
    """
    target = _resolve_target(url, html)
    if target is None:
        return {"ok": False, "error": f"No valid URL or HTML was provided: {url!r}"}

    page_html = target["html"]
    if page_html is None:
        # The cause is already known here. A read timeout means retry or slow
        # down, a reset means you are being throttled, a DNS failure means the
        # host is wrong — collapsing all three into one sentence throws away
        # the only part an operator can act on.
        cause = target.get("error")
        return {
            "ok": False,
            "url": target.get("url"),
            "error": f"Page HTML could not be retrieved: {cause}"
            if cause
            else ("Page HTML could not be retrieved"),
            **({"cause": cause} if cause else {}),
        }

    status = target.get("status_code")
    if status is not None and not 200 <= status < 300:
        # The body of a 404 is an error page, and proposing a graph for it
        # describes something other than the URL that was asked about.
        return {
            "ok": False,
            "url": target.get("url"),
            "final_url": target.get("final_url"),
            "status_code": status,
            "error": (
                f"The page returned HTTP {status}, so its markup describes an error page "
                "rather than the requested URL. Pass the HTML directly to analyse it anyway."
            ),
        }

    facts = page_facts.extract(page_html, target["url"])
    ptype = page_type.classify(target["url"], facts)
    if override_type:
        ptype = {
            **ptype,
            "inferred_type": override_type,
            "confidence": "high",
            "signals": [
                *ptype.get("signals", []),
                {"type": override_type, "weight": 5.0, "reason": "Type set explicitly"},
            ],
            "alternatives": [],
            "note": None,
        }

    graph = build_graph(target["url"], facts, ptype)
    diff = diff_vs_existing(facts, graph, ptype)

    return {
        "ok": True,
        "url": target["url"],
        **(
            {"final_url": target["final_url"], "status_code": target["status_code"]}
            if target.get("final_url")
            else {}
        ),
        "inferred_type": ptype["inferred_type"],
        "confidence": ptype["confidence"],
        "signals": ptype.get("signals", []),
        "alternatives": ptype.get("alternatives", []),
        **({"note": ptype["note"]} if ptype.get("note") else {}),
        "suggested_graph": graph,
        "diff_vs_existing": diff,
        "facts": facts,
    }


def _resolve_target(url: str | None, html: str | None) -> dict[str, Any] | None:
    """Use supplied HTML offline, otherwise fetch through the shared HTTP layer."""
    if html is not None:
        if not url:
            return None
        return {"url": url, "html": html}
    if not url:
        return None
    from seohead.recon.net import normalize_url

    target = normalize_url(url)
    if not target:
        return None
    try:
        from seohead.recon.net import http_client

        client, _ = http_client(25.0)
    except ImportError:
        return {"url": target, "html": None}
    try:
        with client:
            resp = client.get(target)
        return {
            "url": target,
            "html": resp.text[:5_000_000],
            "final_url": str(resp.url),
            "status_code": resp.status_code,
        }
    except Exception as exc:
        return {"url": target, "html": None, "error": str(exc)}
