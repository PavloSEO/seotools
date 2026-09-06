# ruff: noqa: RUF001 -- Intentional Cyrillic fixtures verify Russian SEO extraction.
"""Network-independent tests for page fact extraction."""

from seohead.tools import page_facts

# This fixture intentionally remains in Russian to verify extraction from a
# localized product page, including visible text, breadcrumbs, and RUB prices.
HTML = """
<html lang="ru"><head>
  <title>Кроссовки Acme X — купить за 5990 ₽ | Магазин</title>
  <meta name="description" content="Кроссовки для бега">
  <link rel="canonical" href="https://shop.example.com/products/acme-x">
  <meta property="og:site_name" content="Acme Shop">
  <meta property="og:title" content="Кроссовки Acme X">
  <meta property="og:type" content="product">
  <meta property="article:published_time" content="2025-01-15T09:00:00Z">
</head><body>
  <nav class="breadcrumb"><a href="/">Главная</a> · <a href="/products">Товары</a></nav>
  <h1>Кроссовки Acme X</h1>
  <div itemscope itemtype="https://schema.org/Product">
    <span itemprop="name">Кроссовки Acme X</span>
    <span itemprop="price" content="5990">5990 ₽</span>
    <meta itemprop="priceCurrency" content="RUB">
    <div itemprop="aggregateRating" itemscope itemtype="https://schema.org/AggregateRating">
      <span itemprop="ratingValue">4.7</span>
      <span itemprop="reviewCount">42</span>
    </div>
  </div>
  <div itemscope itemtype="https://schema.org/Organization">
    <span itemprop="name">Acme Shop</span>
    <span itemprop="telephone">+7 800 123-45-67</span>
    <link itemprop="sameAs" href="https://t.me/acmeshop">
  </div>
  <footer>
    <a href="https://t.me/acmeshop">Telegram</a>
    <a href="https://vk.com/acmeshop">VK</a>
  </footer>
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"Product","name":"Кроссовки Acme X",
   "offers":{"@type":"Offer","price":"5990","priceCurrency":"RUB"}}
  </script>
</body></html>
"""


def test_base_facts_from_parser():
    f = page_facts.extract(HTML, "https://shop.example.com/products/acme-x")
    assert f["title"] == "Кроссовки Acme X — купить за 5990 ₽ | Магазин"
    assert f["canonical"] == "https://shop.example.com/products/acme-x"
    assert f["og"]["og:site_name"] == "Acme Shop"
    assert f["og"]["og:type"] == "product"
    assert f["h1"] == "Кроссовки Acme X"


def test_published_time_from_meta():
    f = page_facts.extract(HTML, "https://shop.example.com/products/acme-x")
    assert f["published_time"] == "2025-01-15T09:00:00Z"


def test_existing_types_extracted_from_jsonld():
    f = page_facts.extract(HTML, "https://shop.example.com/products/acme-x")
    assert "Product" in f["existing_types"]
    assert "Offer" in f["existing_types"]


def test_price_from_microdata_is_fact_not_heuristic():
    f = page_facts.extract(HTML, "https://shop.example.com/products/acme-x")
    assert f["price"] is not None
    assert f["price"]["value"] == 5990.0
    assert f["price"]["currency"] == "RUB"
    assert f["price"]["heuristic"] is False
    assert f["price"]["source"] == "microdata"


def test_rating_from_microdata():
    f = page_facts.extract(HTML, "https://shop.example.com/products/acme-x")
    assert f["rating"]["value"] == "4.7"
    assert f["rating"]["count"] == "42"
    assert f["rating"]["heuristic"] is False


def test_same_as_collects_social_links_and_itemprop():
    f = page_facts.extract(HTML, "https://shop.example.com/products/acme-x")
    same = set(f["same_as"])
    assert "https://t.me/acmeshop" in same
    assert "https://vk.com/acmeshop" in same


def test_organization_from_microdata():
    f = page_facts.extract(HTML, "https://shop.example.com/products/acme-x")
    org = f["organization"]
    assert org["name"] == "Acme Shop"
    assert org["telephone"] == "+7 800 123-45-67"


def test_breadcrumbs_from_nav_when_no_jsonld_breadcrumbs():
    f = page_facts.extract(HTML, "https://shop.example.com/products/acme-x")
    names = [b["name"] for b in f["breadcrumbs"]]
    assert "Главная" in names
    assert "Товары" in names
    assert all(b["url"].startswith("https://shop.example.com") for b in f["breadcrumbs"])


def test_price_heuristic_from_text_when_no_microdata():
    # Russian price copy is intentional: the heuristic must recognize RUB text.
    html = "<html><body><h1>X</h1><p>Цена: 1290 руб.</p></body></html>"
    f = page_facts.extract(html, "https://example.com/p")
    assert f["price"] is not None
    assert f["price"]["heuristic"] is True
    assert f["price"]["source"] == "text"


def test_missing_signals_return_none_not_fake_values():
    html = "<html><body><h1>Plain page</h1><p>Copy without a price or date.</p></body></html>"
    f = page_facts.extract(html, "https://example.com/about")
    assert f["price"] is None
    assert f["rating"] is None
    assert f["published_time"] is None
    assert f["organization"]["name"] is None


# Issue #325: a related-product card elsewhere on the page must never lend its
# price or rating to the page's own primary Product, and reordering the
# unrelated markup must not change what gets attributed to the target.


def _related_and_target(related_first: bool) -> str:
    target = """<main><div itemscope itemtype="https://schema.org/Product">
      <h1 itemprop="name">Target</h1><meta itemprop="price" content="20">
      <meta itemprop="priceCurrency" content="USD">
      <div itemprop="aggregateRating" itemscope itemtype="https://schema.org/AggregateRating">
        <meta itemprop="ratingValue" content="4.0"><meta itemprop="reviewCount" content="2">
      </div></div></main>"""
    related = """<aside><div itemscope itemtype="https://schema.org/Product">
      <span itemprop="name">Other</span><meta itemprop="price" content="99">
      <meta itemprop="priceCurrency" content="USD">
      <div itemprop="aggregateRating" itemscope itemtype="https://schema.org/AggregateRating">
        <meta itemprop="ratingValue" content="4.9"><meta itemprop="reviewCount" content="100">
      </div></div></aside>"""
    body = related + target if related_first else target + related
    return '<meta property="og:type" content="product">' + body


def test_target_first_price_and_rating_are_the_targets_own():
    f = page_facts.extract(
        _related_and_target(related_first=False), "https://shop.example.test/products/target"
    )
    assert f["price"]["value"] == 20.0
    assert f["rating"]["value"] == "4.0"
    assert f["rating"]["count"] == "2"


def test_related_first_still_reads_the_targets_own_price_and_rating():
    # Reordering the unrelated scope must not change what is attributed to
    # the target: the related card's 99/4.9 must never surface here.
    f = page_facts.extract(
        _related_and_target(related_first=True), "https://shop.example.test/products/target"
    )
    assert f["price"]["value"] == 20.0
    assert f["rating"]["value"] == "4.0"
    assert f["rating"]["count"] == "2"


def test_ambiguous_product_scopes_are_omitted_not_guessed():
    # Two competing Product scopes with no h1 to single one out: no fact can
    # be attributed with confidence. The visible-text values make this the
    # regression control: neither a page-wide Microdata nor text fallback may
    # lend the first card's values to the target.
    html = """<div itemscope itemtype="https://schema.org/Product">
      <span itemprop="name">A</span><meta itemprop="price" content="20">
      <meta itemprop="ratingValue" content="4.0"><span>20 USD, 4.0/5</span></div>
    <div itemscope itemtype="https://schema.org/Product">
      <span itemprop="name">B</span><meta itemprop="price" content="99">
      <meta itemprop="ratingValue" content="4.9"><span>99 USD, 4.9/5</span></div>"""
    f = page_facts.extract(html, "https://example.com/p")
    assert f["price"] is None
    assert f["rating"] is None


def test_single_product_visible_text_keeps_heuristics():
    """The ambiguity guard must not suppress ordinary one-Product text evidence."""
    html = """<div itemscope itemtype="https://schema.org/Product">
      <span>20 USD, 4.0/5</span></div>"""
    f = page_facts.extract(html, "https://example.com/p")
    assert f["price"]["value"] == 20.0
    assert f["price"]["source"] == "text"
    assert f["rating"] == {"value": "4.0", "count": None, "heuristic": True, "source": "text"}


def test_svg_label_price_is_not_the_pages_price():
    """Issue #544: text inside <svg> is a drawing label, never page copy.

    A parent-only exclusion test missed it -- the number sits in a text node
    under <text>, not directly under <svg> -- so a chart axis was returned as
    a confident price for a page that states none.
    """
    html = (
        "<html><body><h1>Product</h1>"
        '<svg width="200" height="50"><text x="0" y="20">19 900 rub.</text></svg>'
        "<p>Call for pricing.</p></body></html>"
    )
    f = page_facts.extract(html, "https://example.com/p")
    assert f["price"] is None
