"""Network-independent tests for the Schema.org @graph generator."""

import json

from seohead.tools import schema_build
from seohead.tools.parser import parse_html
from seohead.tools.schema_org import check_schema

PRODUCT_HTML = """
<html><head>
  <title>Widget — Shop</title>
  <meta property="og:site_name" content="Shop">
  <meta property="og:image" content="https://cdn.example.com/shop.jpg">
  <link rel="canonical" href="https://shop.example.com/p/widget">
</head><body>
  <h1>Widget</h1>
  <div itemscope itemtype="https://schema.org/Product">
    <span itemprop="price" content="100">100 USD</span>
    <meta itemprop="priceCurrency" content="USD">
    <div itemprop="aggregateRating" itemscope itemtype="https://schema.org/AggregateRating">
      <span itemprop="ratingValue">4.5</span><span itemprop="reviewCount">10</span>
    </div>
  </div>
  <nav class="breadcrumb"><a href="/">Home</a><a href="/p">Products</a></nav>
</body></html>
"""

ARTICLE_HTML = (
    """
<html><head>
  <title>How to SEO — Blog</title>
  <meta property="og:site_name" content="Blog">
  <meta property="og:type" content="article">
  <meta property="article:published_time" content="2025-02-01T08:00:00Z">
  <link rel="canonical" href="https://blog.example.com/how-to-seo">
  <link rel="author" href="https://blog.example.com/author/joe">
</head><body>
  <h1>How to do SEO in 2025</h1>
  <p>"""
    + "Word " * 600
    + """</p>
</body></html>
"""
)

BARE_HTML = "<html><head><title>Just a page</title></head><body><h1>Hi</h1></body></html>"


def _entity(graph: dict) -> dict:
    for n in graph["@graph"]:
        if n.get("@id") == "#webpage":
            return n
    raise AssertionError("The graph does not contain a #webpage node")


def test_product_graph_has_offers_rating_and_brand():
    r = schema_build.build_schema(url="https://shop.example.com/p/widget", html=PRODUCT_HTML)
    assert r["ok"] is True
    assert r["inferred_type"] == "Product"
    ent = _entity(r["suggested_graph"])
    assert ent["@type"] == "Product"
    assert ent["offers"]["price"] == 100
    assert ent["aggregateRating"]["ratingValue"] == "4.5"
    assert ent["brand"]["name"] == "Shop"


def test_offer_keeps_a_genuine_zero_price():
    # A price of exactly 0 (free product) is a measured fact and must survive.
    assert schema_build._offer({"price": {"value": 0.0, "currency": "USD"}}) == {
        "@type": "Offer",
        "price": 0,
        "priceCurrency": "USD",
    }


def test_offer_stays_silent_with_no_price_fact():
    # Absence of evidence must not become a fabricated Offer.
    assert schema_build._offer({"price": None}) is None
    assert schema_build._offer({}) is None


def test_offer_unaffected_for_a_normal_price():
    assert schema_build._offer({"price": {"value": 19900.0, "currency": "USD"}}) == {
        "@type": "Offer",
        "price": 19900,
        "priceCurrency": "USD",
    }


def test_free_product_page_still_gets_an_offer_in_the_graph():
    html = """
<html><head><title>Free Widget</title></head>
<body>
<div itemscope itemtype="https://schema.org/Product">
<h1 itemprop="name">Free Widget</h1>
<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
<span itemprop="price">0</span>
<span itemprop="priceCurrency">USD</span>
</div>
</div>
</body></html>
"""
    from seohead.tools import page_facts

    facts = page_facts.extract(html, "https://example.com/free-widget")
    graph = schema_build.build_graph(
        "https://example.com/free-widget",
        facts,
        {"inferred_type": "Product", "confidence": "high", "signals": []},
    )
    ent = _entity(graph)
    assert ent["@type"] == "Product"
    assert ent["offers"] == {"@type": "Offer", "price": 0, "priceCurrency": "USD"}


def test_aggregate_rating_keeps_a_genuine_zero_rating():
    assert schema_build._aggregate_rating({"rating": {"value": 0, "count": 5}}) == {
        "@type": "AggregateRating",
        "ratingValue": 0,
        "ratingCount": 5,
    }


def test_aggregate_rating_stays_silent_with_no_rating_fact():
    assert schema_build._aggregate_rating({"rating": None}) is None
    assert schema_build._aggregate_rating({}) is None


def test_graph_is_linked_via_ids():
    r = schema_build.build_schema(url="https://shop.example.com/p/widget", html=PRODUCT_HTML)
    ids = {n.get("@id") for n in r["suggested_graph"]["@graph"]}
    assert "#organization" in ids
    assert "#website" in ids
    ent = _entity(r["suggested_graph"])
    site = next(n for n in r["suggested_graph"]["@graph"] if n.get("@id") == "#website")
    assert ent["isPartOf"]["@id"] == "#website"
    assert site["publisher"]["@id"] == "#organization"


def test_article_uses_headline_and_dates_not_name():
    r = schema_build.build_schema(url="https://blog.example.com/how-to-seo", html=ARTICLE_HTML)
    assert r["inferred_type"] == "Article"
    ent = _entity(r["suggested_graph"])
    assert ent["@type"] == "Article"
    assert "headline" in ent and "name" not in ent
    assert ent["datePublished"] == "2025-02-01T08:00:00Z"
    assert ent["author"]["@type"] == "Person"
    assert ent["publisher"]["@id"] == "#organization"


def test_breadcrumb_added_only_when_present_and_links_to_page():
    r = schema_build.build_schema(url="https://shop.example.com/p/widget", html=PRODUCT_HTML)
    crumbs = [n for n in r["suggested_graph"]["@graph"] if n.get("@id") == "#breadcrumb"]
    assert crumbs, "BreadcrumbList must be present in the graph"
    assert crumbs[0]["@type"] == "BreadcrumbList"
    assert _entity(r["suggested_graph"])["breadcrumb"]["@id"] == "#breadcrumb"


def test_template_jsonld_is_absent_from_parser_validator_and_generator():
    html = """<html><body>
    <template>
      <script type="application/ld+json">{bad JSON}</script>
      <script type="application/ld+json">{
        "@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [{"@type": "ListItem", "position": 1, "name": "Draft"}]
      }</script>
    </template>
    <main><h1>Published page</h1></main>
    <script type="application/ld+json">{
      "@context": "https://schema.org", "@type": "WebPage", "name": "Published page"
    }</script>
    </body></html>"""

    parsed = parse_html(html, "https://example.test/page")
    checked = check_schema(html=html)
    built = schema_build.build_schema(
        html=html, url="https://example.test/page", override_type="WebPage"
    )

    assert parsed["jsonld"] == [
        {"@context": "https://schema.org", "@type": "WebPage", "name": "Published page"}
    ]
    assert parsed["jsonld_invalid"] == []
    assert checked["blocks"] == checked["blocks_found"] == 1
    assert checked["parse_errors"] == []
    assert built["diff_vs_existing"]["entity_diff"]["no_jsonld"] is False
    assert "breadcrumb" not in _entity(built["suggested_graph"])


def test_no_organization_node_when_no_org_signals():
    r = schema_build.build_schema(url="https://example.com/page", html=BARE_HTML)
    ids = {n.get("@id") for n in r["suggested_graph"]["@graph"]}
    # No og:site_name or itemscope Organization signal exists.
    assert "#organization" not in ids


def test_diff_reports_missing_recommended_for_product_without_jsonld():
    r = schema_build.build_schema(url="https://shop.example.com/p/widget", html=PRODUCT_HTML)
    diff = r["diff_vs_existing"]
    assert diff["type"] == "Product"
    # With no existing JSON-LD, every recommended field is missing.
    for field in ("name", "image", "offers", "aggregateRating", "description"):
        assert field in diff["missing_recommended"], (
            f"{field} must be included in missing_recommended"
        )
    # offers and aggregateRating are available from extracted facts.
    assert "offers" in diff["addable_now"]
    assert "aggregateRating" in diff["addable_now"]


def test_diff_marks_already_present_when_jsonld_has_field():
    html = (
        PRODUCT_HTML
        + '<script type="application/ld+json">'
        + json.dumps({"@context": "https://schema.org", "@type": "Product", "name": "Widget"})
        + "</script>"
    )
    r = schema_build.build_schema(url="https://shop.example.com/p/widget", html=html)
    assert "name" in r["diff_vs_existing"]["already_present"]


def test_override_type_forces_service():
    r = schema_build.build_schema(
        url="https://example.com/anything", html=BARE_HTML, override_type="Service"
    )
    assert r["inferred_type"] == "Service"
    assert r["confidence"] == "high"
    assert _entity(r["suggested_graph"])["@type"] == "Service"


def test_service_graph_has_provider_and_service_type():
    # Russian visible copy intentionally verifies localized Service classification
    # and confirms that the page's H1 is preserved in serviceType.
    html = """
    <html><head><title>SEO услуги — Agency</title>
    <meta property="og:site_name" content="Agency"></head><body>
    <h1>Услуги по SEO</h1>
    <span itemprop="price" content="500">500 USD</span>
    <meta itemprop="priceCurrency" content="USD">
    </body></html>
    """
    r = schema_build.build_schema(url="https://agency.example.com/services/seo", html=html)
    assert r["inferred_type"] == "Service"
    ent = _entity(r["suggested_graph"])
    assert ent["@type"] == "Service"
    assert ent["provider"]["@id"] == "#organization"
    assert ent["serviceType"] == "Услуги по SEO"
    assert ent["offers"]["price"] == 500


def test_suggested_graph_validates_with_no_dangling_ids():
    # Every @id in the generated graph must resolve to a linked entity.
    from seohead.tools import schema_org as validator

    r = schema_build.build_schema(url="https://shop.example.com/p/widget", html=PRODUCT_HTML)
    rendered = (
        "<script type='application/ld+json'>" + json.dumps(r["suggested_graph"]) + "</script>"
    )
    check = validator.check_schema(html=rendered)
    flat = [e for ent in check["entities"] for e in ent["errors"]]
    assert not any("not found in the graph" in e for e in flat), f"Dangling @id references: {flat}"


# ── Value-level diff (entity_diff) ──────────────────────────────────────────

ARTICLE_WITH_STALE_JSONLD = ARTICLE_HTML.replace(
    "</head>",
    '<script type="application/ld+json">{"@context":"https://schema.org",'
    '"@type":"Article","headline":"OLD TITLE","datePublished":"2025-02-01"}</script>'
    "</head>",
)


def test_entity_diff_flags_stale_headline_mismatch():
    r = schema_build.build_schema(
        url="https://blog.example.com/how-to-seo", html=ARTICLE_WITH_STALE_JSONLD
    )
    ed = r["diff_vs_existing"]["entity_diff"]
    assert ed["no_jsonld"] is False
    headlines = [m for m in ed["property_mismatches"] if m["property"] == "headline"]
    assert headlines, "A stale headline must produce a property mismatch"
    assert headlines[0]["suggested"] == "How to do SEO in 2025"
    assert headlines[0]["actual"] == "OLD TITLE"


def test_entity_diff_reports_missing_entities():
    r = schema_build.build_schema(
        url="https://blog.example.com/how-to-seo", html=ARTICLE_WITH_STALE_JSONLD
    )
    ed = r["diff_vs_existing"]["entity_diff"]
    missing_types = {m["type"] for m in ed["missing_entities"]}
    # The suggested graph includes Organization and WebSite, but existing JSON-LD does not.
    assert "Organization" in missing_types
    assert "WebSite" in missing_types


def test_entity_diff_no_jsonld_when_page_has_none():
    # BARE_HTML has no JSON-LD, so no_jsonld is true without spurious findings.
    r = schema_build.build_schema(url="https://example.com/page", html=BARE_HTML)
    ed = r["diff_vs_existing"]["entity_diff"]
    assert ed["no_jsonld"] is True
    assert ed["property_mismatches"] == []


def test_entity_diff_clean_when_jsonld_matches_facts():
    # Matching Article JSON-LD must not produce a headline mismatch.
    html = (
        "<html><head><title>How to do SEO in 2025</title>"
        '<meta property="og:site_name" content="Blog">'
        '<meta property="article:published_time" content="2025-02-01T08:00:00Z">'
        '<link rel="canonical" href="https://blog.example.com/how-to-seo"></head><body>'
        "<h1>How to do SEO in 2025</h1><p>" + "Word " * 600 + "</p>"
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@type":"Article","headline":"How to do SEO in 2025",'
        '"datePublished":"2025-02-01T08:00:00Z"}</script></body></html>'
    )
    r = schema_build.build_schema(url="https://blog.example.com/how-to-seo", html=html)
    ed = r["diff_vs_existing"]["entity_diff"]
    assert not any(m["property"] == "headline" for m in ed["property_mismatches"])
