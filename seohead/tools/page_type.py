"""Classify the Schema.org content type of a page before building its graph.

Given a URL and extracted page facts, the classifier proposes the Schema.org type
that best describes the page, such as ``Article``, ``Product``, ``Service``, or
``LocalBusiness``. This is harder than graph generation because a URL and content
rarely prove whether an entity is a service or product. The result therefore
exposes every weighted signal and returns alternatives when evidence is weak,
leaving the final decision to a human or orchestrator.

Signals are weighted in descending order of reliability:
  1. An existing content ``@type`` in JSON-LD is almost conclusive.
  2. ``og:type`` values such as ``article`` or ``product``.
  3. Latin and Cyrillic path patterns such as ``/blog/``, ``/product/``, or
     ``/uslugi/``.
  4. Content evidence: prices suggest Product and Service; ratings suggest Product
     or LocalBusiness; a publication date plus long copy suggests Article; and
     service terminology in the H1 suggests Service.

Content types are kept separate from utility entities such as ``WebPage``,
``Organization``, and ``BreadcrumbList``, which do not explain what the page is
principally about.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

# Types that describe page content rather than supporting graph entities.
# NewsArticle and BlogPosting map to Article; Offer maps to Product.
CONTENT_TYPES: dict[str, str] = {
    "Article": "Article",
    "NewsArticle": "Article",
    "BlogPosting": "Article",
    "Product": "Product",
    "Offer": "Product",
    # A listing is not a product. Catalogs, categories, and collections describe
    # sets; marking them Product promises an entity that the page does not contain.
    "CollectionPage": "CollectionPage",
    "ItemList": "CollectionPage",
    "Service": "Service",
    "LocalBusiness": "LocalBusiness",
    "Store": "LocalBusiness",
    "Restaurant": "LocalBusiness",
    "Event": "Event",
    "Recipe": "Recipe",
    "FAQPage": "FAQPage",
    "HowTo": "HowTo",
    "VideoObject": "VideoObject",
    "Course": "Course",
    "JobPosting": "JobPosting",
    "Review": "Review",
    "Question": "FAQPage",  # A standalone question usually belongs to FAQPage context.
}

# Map Open Graph types to Schema.org content types.
OG_TYPE_MAP: dict[str, str] = {
    "article": "Article",
    "product": "Product",
    "music.song": "VideoObject",
    "video.movie": "VideoObject",
    "video.episode": "VideoObject",
    "book": "Article",
    "profile": "Article",
}

# Latin and transliterated/Cyrillic-market path patterns, including common RU slugs.
PATH_PATTERNS: dict[str, list[str]] = {
    "Article": [r"/blog/", r"/article", r"/news/", r"/post/", r"/stat(yi|i)?/", r"/novost"],
    # A product detail page represents one entity.
    "Product": [r"/product", r"/item/", r"/tovar", r"/p/\d", r"/goods"],
    # Catalog and category routes represent sets. Treating /catalog/ and /shop/
    # as Product previously mislabeled every category page as a single product.
    "CollectionPage": [
        r"/catalog",
        r"/kategor",
        r"/category",
        r"/collection",
        r"/razdel",
        r"/shop/",
        r"/podbork",
    ],
    "Service": [r"/service", r"/uslug", r"/reshen", r"/solutions"],
    "LocalBusiness": [r"/contact", r"/kontakt", r"/about", r"/o-kompanii", r"/address"],
    "Event": [r"/event", r"/meropriyat", r"/afisha"],
    "Recipe": [r"/recipe", r"/recept"],
    "FAQPage": [r"/faq", r"/vopros"],
    "Course": [r"/course", r"/kurs"],
    "JobPosting": [r"/job", r"/vacanc", r"/career", r"/rabota"],
}

# Confidence thresholds.
_HIGH = 5.0  # A decisive signal, such as an existing JSON-LD content type.
_MID = 3.0  # A meaningful combination of weaker signals.
_CLOSE_RATIO = 0.6  # An alternative is close at >= 60% of the leading score.


def classify(url: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Infer a page's content type from its URL and extracted facts.

    Returns ``{inferred_type, confidence (high|mid|low), signals[],
    alternatives[], note?}``. ``inferred_type`` is always present and defaults to
    ``WebPage``; this explicitly means that no specific content type was found.
    """
    scores: dict[str, float] = defaultdict(float)
    signals: list[dict[str, Any]] = []

    def add(t: str, weight: float, reason: str) -> None:
        # Collapse supporting aliases into the canonical content type.
        canonical = CONTENT_TYPES.get(t, t)
        scores[canonical] += weight
        signals.append({"type": canonical, "weight": weight, "reason": reason})

    # 1. Existing JSON-LD provides the strongest signal.
    existing = {t for t in facts.get("existing_types", []) if t in CONTENT_TYPES}
    for t in existing:
        add(t, 5.0, f"Already declared in JSON-LD ({t})")

    # 2. Open Graph type.
    og_type = (facts.get("og") or {}).get("og:type")
    if og_type and og_type in OG_TYPE_MAP:
        add(OG_TYPE_MAP[og_type], 3.0, f"og:type={og_type}")

    # 3. URL path patterns.
    path = (urlparse(url).path or "").lower()
    for typ, patterns in PATH_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, path):
                add(typ, 2.0, f"URL path matches {pat}")
                break  # Count at most one path pattern per type.

    # 4. Content signals.
    price = facts.get("price")
    if price and price.get("value"):
        # A price alone does not prove Product: services, courses, and subscriptions
        # also have prices. Product-only weighting previously made priced service
        # pages lose an arbitrary alphabetical tie to Product.
        how = ", heuristic" if price.get("heuristic") else ", microdata"
        add("Product", 2.0, f"Page contains a price ({price['value']}{how})")
        add(
            "Service",
            2.0,
            f"Page contains a price ({price['value']}{how}); services can also have prices",
        )

    rating = facts.get("rating")
    if rating and rating.get("value"):
        # Ratings apply to products and businesses, so context is required.
        add("Product", 1.0, "aggregateRating is present (valid for products and businesses)")
        add("LocalBusiness", 1.0, "aggregateRating is present (valid for businesses and products)")

    pub = facts.get("published_time")
    wc = facts.get("word_count") or 0
    if pub and wc > 500:
        add("Article", 2.0, f"Publication date and long-form copy are present ({wc} words)")
    elif pub:
        add("Article", 1.0, "Publication date is present")

    # 5. Shape signals. These carry no language, which is the point: a catalogue
    # with Cyrillic slugs and no markup had nothing else to offer.
    structure = facts.get("structure") or {}
    items = structure.get("priced_items") or 0
    if items >= 6:
        add("CollectionPage", 4.0, f"{items} linked, priced items — a listing, not one thing")
    elif items >= 3:
        add("CollectionPage", 3.0, f"{items} linked, priced items — a listing, not one thing")

    actions = structure.get("commerce_actions") or []
    if actions and items < 3:
        # A buy button on a page that is not a grid is a buy button for this page.
        add("Product", 2.0, f"The page offers to sell what it is about ({actions[0]})")

    if (structure.get("images") or 0) >= 4 and (structure.get("spec_rows") or 0) >= 3:
        add("Product", 2.0, "A gallery beside a specification table")

    depth = structure.get("breadcrumb_depth") or 0
    if depth >= 3:
        add("Product", 1.0, f"Breadcrumb depth {depth} — a leaf, not a section")
        add("Article", 1.0, f"Breadcrumb depth {depth} — a leaf, not a section")
    elif depth == 2:
        add("CollectionPage", 1.0, f"Breadcrumb depth {depth} — a section, not a leaf")

    h1 = (facts.get("h1") or "").lower()
    # Preserve Russian service inflections; a trailing word boundary is unreliable
    # after variants ending in either a Cyrillic letter or inflection suffix.
    if re.search(
        r"(?:услуг[а-яё]*|сервис[а-яё]*|services?|solutions)",  # noqa: RUF001 - Russian morphology
        h1,
    ):
        add("Service", 2.0, f'Russian service terminology in H1 ("{facts.get("h1")}")')

    # Resolve the weighted decision.
    if not scores:
        return {
            "inferred_type": "WebPage",
            "confidence": "low",
            "signals": [],
            "alternatives": [],
            "note": "No specific content-type signals were found; using WebPage",
        }

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best_type, best_score = ranked[0]
    alternatives = [t for t, s in ranked[1:] if s >= best_score * _CLOSE_RATIO]
    # Sorting resolves an exact tie alphabetically, which is arbitrary. Report it
    # explicitly so the selected type is not mistaken for an evidence-based result.
    tied = [t for t, s in ranked[1:] if s == best_score]

    if best_score >= _HIGH:
        confidence = "high"
    elif best_score >= _MID and not alternatives:
        confidence = "mid"
    elif best_score >= _MID and alternatives:
        # There is a lead, but a close alternative limits confidence.
        confidence = "mid"
    else:
        confidence = "low"

    note = None
    if tied:
        note = (
            f"Equal scores for {', '.join([best_type, *tied])}; {best_type} was "
            "selected alphabetically, not inferred. Set the type explicitly."
        )
        confidence = "low"
    elif confidence == "low":
        note = (
            "Weak evidence with close candidates: "
            + ", ".join([best_type, *alternatives])
            + ". Review manually or pass --type."
        )
    elif alternatives:
        note = (
            f"Ambiguous between {best_type} and {', '.join(alternatives)}; review the page context"
        )

    return {
        "inferred_type": best_type,
        "confidence": confidence,
        "signals": sorted(signals, key=lambda s: -s["weight"]),
        "alternatives": alternatives,
        **({"note": note} if note else {}),
    }
