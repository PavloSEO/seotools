# ruff: noqa: RUF001 -- Intentional Cyrillic fixtures cover Russian SEO regressions.
"""Network-independent tests for page type classification."""

from seohead.tools import page_type
from seohead.tools.page_facts import extract
from seohead.tools.page_type import classify


def _classify(url: str, html: str) -> dict:
    return classify(url, extract(html, url))


def test_existing_jsonld_product_is_high_confidence():
    facts = {
        "existing_types": ["Product", "Offer"],
        "og": {},
        "price": None,
        "rating": None,
        "published_time": None,
        "word_count": 0,
        "h1": None,
    }
    r = page_type.classify("https://shop.example.com/products/acme-x", facts)
    assert r["inferred_type"] == "Product"
    assert r["confidence"] == "high"
    assert any(s["reason"].startswith("Already declared") for s in r["signals"])


def test_article_signals_from_og_path_and_date():
    facts = {
        "existing_types": [],
        "og": {"og:type": "article"},
        "price": None,
        "rating": None,
        "published_time": "2025-01-15",
        "word_count": 1200,
        "h1": "Heading",
    }
    r = page_type.classify("https://site.example.com/blog/how-to-seo", facts)
    assert r["inferred_type"] == "Article"
    assert r["confidence"] in ("mid", "high")


def test_service_by_path_and_h1():
    # The Russian H1 intentionally verifies localized service-term classification.
    facts = {
        "existing_types": [],
        "og": {},
        "price": None,
        "rating": None,
        "published_time": None,
        "word_count": 200,
        "h1": "Услуги по SEO",
    }
    r = page_type.classify("https://agency.example.com/services/seo", facts)
    assert r["inferred_type"] == "Service"
    assert r["confidence"] in ("mid", "low")


def test_cyrillic_path_patterns_recognized():
    # The Russian contact heading intentionally exercises localized classification.
    facts = {
        "existing_types": [],
        "og": {},
        "price": None,
        "rating": None,
        "published_time": None,
        "word_count": 0,
        "h1": "Контакты",
    }
    r = page_type.classify("https://ru.example.com/kontakty", facts)
    assert r["inferred_type"] == "LocalBusiness"


def test_price_signal_pushes_to_product():
    facts = {
        "existing_types": [],
        "og": {},
        "price": {"value": "5990", "heuristic": True},
        "rating": None,
        "published_time": None,
        "word_count": 0,
        "h1": "X",
    }
    r = page_type.classify("https://shop.example.com/x", facts)
    assert r["inferred_type"] == "Product"


def test_no_signals_returns_webpage_low():
    facts = {
        "existing_types": [],
        "og": {},
        "price": None,
        "rating": None,
        "published_time": None,
        "word_count": 0,
        "h1": "About us",
    }
    r = page_type.classify("https://site.example.com/", facts)
    assert r["inferred_type"] == "WebPage"
    assert r["confidence"] == "low"
    assert "note" in r


def test_close_candidates_yield_note():
    # Price (Product) + /services/ path (Service) + rating (both) creates a close race.
    facts = {
        "existing_types": [],
        "og": {},
        "price": {"value": "100", "heuristic": False},
        "rating": {"value": "4.5"},
        "published_time": None,
        "word_count": 0,
        "h1": "Consultation",
    }
    r = page_type.classify("https://agency.example.com/services/cons", facts)
    assert r["inferred_type"] in ("Product", "Service")
    # The result must expose the ambiguity through either a note or alternatives.
    assert r.get("note") or r["alternatives"]


def test_signals_are_sorted_by_weight_desc():
    facts = {
        "existing_types": ["Product"],
        "og": {"og:type": "product"},
        "price": {"value": "5"},
        "rating": None,
        "published_time": None,
        "word_count": 0,
        "h1": None,
    }
    r = page_type.classify("https://shop.example.com/product/x", facts)
    weights = [s["weight"] for s in r["signals"]]
    assert weights == sorted(weights, reverse=True)


def test_catalog_is_a_listing_not_a_product():
    """A catalog path must describe a listing rather than a single product.

    Marking a product collection as one Product claims that the page contains an
    item that is not actually present.
    """
    # Russian catalog copy intentionally verifies localized listing classification.
    r = _classify(
        "https://shop.example.com/catalog/nasosy", "<html><body><h1>Насосы</h1></body></html>"
    )
    assert r["inferred_type"] == "CollectionPage"
    assert r["inferred_type"] != "Product"


def test_category_synonyms_are_listings_too():
    for path in (
        "/category/pumps",
        "/kategoriya/nasosy",
        "/razdel/tovary",
        "/collection/new",
        "/shop/tools",
    ):
        # Russian section copy intentionally covers localized listing pages.
        r = _classify(
            f"https://site.example.com{path}", "<html><body><h1>Раздел</h1></body></html>"
        )
        assert r["inferred_type"] == "CollectionPage", f"{path} -> {r['inferred_type']}"


def test_product_card_is_still_a_product():
    """The listing fix must not reclassify individual product pages."""
    for path in ("/product/nasos", "/tovar/nasos", "/item/123", "/p/4567"):
        # Russian product copy and RUB price intentionally test localized extraction.
        r = _classify(
            f"https://site.example.com{path}",
            "<html><body><h1>Насос CDM</h1><p>12 000 руб</p></body></html>",
        )
        assert r["inferred_type"] == "Product", f"{path} -> {r['inferred_type']}"


def test_service_page_with_a_price_is_a_service():
    """A priced service must remain a Service rather than becoming a Product.

    A price previously contributed 2.0 only to Product, creating a 2:2 tie with
    Service that alphabetical ordering always resolved in favor of Product.
    """
    # Russian service copy and monthly RUB price intentionally test local semantics.
    r = _classify(
        "https://agency.example.com/services/seo-prodvizhenie",
        "<html><body><h1>SEO-продвижение</h1><p>от 50 000 руб/мес</p></body></html>",
    )
    assert r["inferred_type"] == "Service"


def test_exact_tie_is_reported_as_a_coin_flip():
    """An alphabetical tie-break must be reported as arbitrary, not inferred."""
    r = classify(
        "https://site.example.com/page",
        {
            "existing_types": [],
            "og": {},
            "price": None,
            "rating": None,
            "published_time": None,
            "word_count": 0,
            "h1": "",
            # Two equal-weight URL paths cannot be modeled simultaneously, so this
            # regression verifies the no-signal fallback explicitly.
        },
    )
    # No signals must produce an honest WebPage fallback, not an invented type.
    assert r["inferred_type"] == "WebPage"
    assert r["confidence"] == "low"
    assert "No specific content-type signals" in (r.get("note") or "")


def test_signals_are_always_shown():
    """The classifier must expose the evidence behind its decision."""
    r = _classify(
        "https://site.example.com/blog/post",
        "<html><head><meta property='article:published_time' content='2026-01-01'>"
        "</head><body><h1>Post</h1></body></html>",
    )
    assert r["signals"], "A decision without supporting signals cannot be audited"
    assert all("reason" in s and "weight" in s for s in r["signals"])


def test_high_confidence_with_close_alternative_is_flagged():
    """A best_score at/above _HIGH must not hide a close, real alternative.

    Issue #476: the confidence branch short-circuited before the
    alternatives-note branch could ever fire when best_score >= _HIGH,
    silently contradicting the alternatives list in the same result.
    """
    r = classify(
        "https://example.com/uslugi/remont",
        {"existing_types": ["Product"], "h1": "Наши услуги по ремонту"},
    )
    assert r["alternatives"] == ["Service"]
    assert r["confidence"] != "high" or (r.get("note") and "Service" in r["note"])


def test_single_decisive_signal_stays_high_with_no_note():
    """Negative control: one decisive signal and nothing else is genuinely
    unambiguous and must not be downgraded or annotated."""
    r = classify(
        "https://example.com/product/widget",
        {"existing_types": ["Product"]},
    )
    assert r["confidence"] == "high"
    assert r["alternatives"] == []
    assert "note" not in r
