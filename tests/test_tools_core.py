"""Unit tests for the pure (network-free) core functions."""

import json
from pathlib import Path

import httpx

from seohead.tools import (
    clusterer,
    downloader,
    hreflang,
    logscan,
    optimizer,
    parser,
    redirects,
    robots,
    sitemap,
)

# ── redirects ────────────────────────────────────────────────────────────────


def test_generate_rules_nginx():
    rules = redirects.generate_rules([{"from": "/old", "to": "/new"}], "nginx")
    assert rules == ["rewrite ^/old$ /new permanent;"]


def test_generate_rules_apache():
    rules = redirects.generate_rules([{"old_url": "/a", "new_url": "/b"}], "apache")
    assert any("/a" in r and "/b" in r for r in rules)


def test_generate_rules_alias_keys():
    a = redirects.generate_rules([{"from": "/x", "to": "/y"}], "nginx")
    b = redirects.generate_rules([{"oldUrl": "/x", "newUrl": "/y"}], "nginx")
    assert a == b


# ── parser (pure parse_html) ─────────────────────────────────────────────────

_HTML = """<html><head><title>Hi</title>
<meta name="description" content="Desc">
<link rel="canonical" href="/canon">
<meta property="og:title" content="OG">
</head><body><h1>Head1</h1><h2>Head2</h2>
<a href="/in">internal</a>
<a href="https://other.tld/out" rel="nofollow">ext</a>
<a href="mailto:x@y.z">mail</a>
<p>hello world foo</p></body></html>"""


def test_parse_html_basics():
    r = parser.parse_html(_HTML, "https://site.tld/page")
    assert r["title"] == "Hi"
    assert r["meta_description"] == "Desc"
    assert r["canonical"] == "https://site.tld/canon"
    assert r["headings"]["h1"] == ["Head1"]
    assert r["word_count"] >= 3


def test_parse_html_links_classified():
    r = parser.parse_html(_HTML, "https://site.tld/page")
    hrefs = {ln["href"] for ln in r["links"]}
    assert "https://site.tld/in" in hrefs
    assert any(ln.get("nofollow") for ln in r["links"])
    assert not any("mailto" in ln["href"] for ln in r["links"])


def test_parse_html_options_off():
    r = parser.parse_html(_HTML, "https://site.tld/page", {"links": False, "text": False})
    assert r["links"] == []


def test_template_link_is_not_extracted():
    """<template> content is never rendered or requested (issue #140)."""
    html = (
        '<html><body><template><a href="/template-link">gone</a></template>'
        "<p>Real content paragraph with enough words to be meaningful for a test.</p>"
        "</body></html>"
    )
    r = parser.parse_html(html, "https://site.tld/page")
    assert r["links"] == []


def test_noscript_link_is_still_extracted():
    """Unlike <template>, <noscript> is real fallback markup a browser or crawler can load."""
    html = '<html><body><noscript><a href="/fallback-link">fb</a></noscript></body></html>'
    r = parser.parse_html(html, "https://site.tld/page")
    hrefs = {ln["href"] for ln in r["links"]}
    assert "https://site.tld/fallback-link" in hrefs


def test_template_only_metadata_never_overrides_or_fabricates_live_values():
    """A <template> is a DocumentFragment: a script must clone it in before any of
    its canonical, robots, OG, JSON-LD, or form content is real (issue #236 -- the
    same exclusion #140 gave links/images). This fixture carries a live head value
    for every field *and* a conflicting template-only one, so a leak shows up as
    either a wrong value or a spurious extra entry, not just a missing one."""
    html = (
        "<html><head>"
        '<link rel="canonical" href="/real-canonical">'
        '<meta name="description" content="real description">'
        '<meta name="robots" content="index, follow">'
        '<meta property="og:title" content="real preview">'
        '<script type="application/ld+json">{"@type": "Article"}</script>'
        "<template>"
        '<link rel="canonical" href="/never-canonical">'
        '<meta name="description" content="template description">'
        '<meta name="robots" content="noindex">'
        '<meta property="og:title" content="template preview">'
        '<script type="application/ld+json">{"@type": "Product"}</script>'
        '<form method="post" action="/never-submitted"><input type="password"></form>'
        "</template>"
        "</head><body><p>Visible page</p></body></html>"
    )
    r = parser.parse_html(html, "https://example.com/page")
    assert r["canonical"] == "https://example.com/real-canonical"
    assert r["meta_description"] == "real description"
    assert r["robots"] == "index, follow"
    assert r["og"] == {"og:title": "real preview"}
    assert r["jsonld"] == [{"@type": "Article"}]
    assert r["forms"] == []


def test_template_only_form_is_not_extracted():
    """A form only a script could clone in is never submitted, so it must not raise
    an insecure-action or password-over-HTTP finding downstream (issue #236)."""
    html = (
        "<html><body>"
        "<template>"
        '<form method="post" action="http://collector.invalid/submit">'
        '<input type="password"></form>'
        "</template>"
        '<form method="get" action="/search"></form>'
        "</body></html>"
    )
    r = parser.parse_html(html, "https://example.com/login")
    assert r["forms"] == [
        {"method": "get", "action": "https://example.com/search", "has_password": False}
    ]


# ── robots ───────────────────────────────────────────────────────────────────


def test_robots_wildcard_and_precedence():
    parsed = robots.parse_robots(
        "User-agent: *\nDisallow: /api/\nDisallow: /*?\nAllow: /api/public\nSitemap: https://s/x.xml"
    )
    assert parsed["sitemaps"] == ["https://s/x.xml"]
    assert robots.is_allowed(parsed, "/api/public/x") is True
    assert robots.is_allowed(parsed, "/api/secret") is False
    assert robots.is_allowed(parsed, "/blog?page=2") is False
    assert robots.is_allowed(parsed, "/blog") is True


def test_robots_end_anchor():
    parsed = robots.parse_robots("User-agent: *\nDisallow: /*.pdf$")
    assert robots.is_allowed(parsed, "/file.pdf") is False
    assert robots.is_allowed(parsed, "/file.pdf?x=1") is True


# ── sitemap (pure parse_sitemap) ─────────────────────────────────────────────


def test_parse_sitemap_urlset():
    xml = (
        b'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://s/a</loc><lastmod>2026-01-01</lastmod></url>"
        b"<url><loc>https://s/b</loc></url></urlset>"
    )
    r = sitemap.parse_sitemap(xml, "https://s/")
    assert r["type"] == "urlset"
    assert len(r["urls"]) == 2


def test_parse_sitemap_index():
    xml = (
        b'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<sitemap><loc>https://s/child.xml</loc></sitemap></sitemapindex>"
    )
    r = sitemap.parse_sitemap(xml, "https://s/")
    assert r["type"] == "index"
    locs = [s["loc"] if isinstance(s, dict) else s for s in r["sitemaps"]]
    assert locs == ["https://s/child.xml"]


# ── hreflang ─────────────────────────────────────────────────────────────────


def test_hreflang_extract_and_validate():
    html = (
        '<link rel="alternate" hreflang="ru" href="/ru">'
        '<link rel="alternate" hreflang="en-US" href="/en">'
        '<link rel="alternate" hreflang="x-default" href="/">'
    )
    alts = hreflang.extract_hreflang(html, "https://s")
    assert len(alts) == 3
    assert hreflang.validate(alts, "https://s/ru") == []
    assert "no x-default alternate" in hreflang.validate(alts[:2], "")


# ── optimizer (pure helpers) ─────────────────────────────────────────────────


def test_compute_resize_fits_and_no_upscale():
    assert optimizer.compute_resize(2000, 1000, {"max_width": 1000}) == (1000, 500)
    assert optimizer.compute_resize(400, 300, {"max_width": 1000}) == (400, 300)


def _svg_visible_text(svg_text: str) -> list[str]:
    from lxml import etree

    root = etree.fromstring(svg_text.encode("utf-8"))
    return ["".join(node.itertext()) for node in root.xpath('//*[local-name()="text"]')]


def test_minify_svg_preserves_tspan_gap_and_xml_space_preserve():
    # A bare regex whitespace collapse cannot tell a layout indent from the single
    # space that is the only thing separating two <tspan>s, or from repeated spaces
    # an author marked significant with xml:space="preserve" (#229).
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg"><text><tspan>A</tspan> '
        '<tspan>B</tspan></text><text xml:space="preserve">C  D</text></svg>'
    )
    assert _svg_visible_text(source) == ["A B", "C  D"]
    assert _svg_visible_text(optimizer.minify_svg(source)) == ["A B", "C  D"]


def test_minify_svg_preserves_whitespace_significant_foreign_object():
    # <foreignObject> embeds arbitrary XHTML; a <pre> inside it depends on the
    # source whitespace to be preserved byte-for-byte (#480).
    source = (
        '<svg xmlns="http://www.w3.org/2000/svg"><foreignObject>'
        '<body xmlns="http://www.w3.org/1999/xhtml"><pre>line one\n'
        "line two\n   indented</pre></body></foreignObject></svg>"
    )
    assert optimizer.minify_svg(source) == source


def test_minify_svg_still_collapses_whitespace_outside_protected_blocks():
    # Negative control: ordinary inter-tag whitespace outside <text>/<foreignObject>
    # must still collapse as before.
    assert optimizer.minify_svg("<svg><rect/>   <rect/></svg>") == "<svg><rect/><rect/></svg>"


def test_optimize_files_preserves_svg_visible_text(tmp_path):
    source_text = (
        '<svg xmlns="http://www.w3.org/2000/svg"><text><tspan>A</tspan> '
        '<tspan>B</tspan></text><text xml:space="preserve">C  D</text></svg>'
    )
    source = tmp_path / "source.svg"
    source.write_text(source_text, encoding="utf-8")

    result = optimizer.optimize_files([str(source)], {"out_dir": str(tmp_path / "out")})
    output = Path(result["results"][0]["out"]).read_text(encoding="utf-8")

    assert result["ok"] is True
    assert _svg_visible_text(output) == _svg_visible_text(source_text)


def test_downloader_host_only_url_gets_content_type_extension(tmp_path):
    target = downloader.target_path(
        "https://example.com/", str(tmp_path), "domain-path", "image/gif"
    )
    assert target == str(tmp_path / "example.com" / "example.com-image.gif")


# ── downloader (network-free download_images loop) ───────────────────────────

_SYNTHETIC_PNG = b"\x89PNG\r\n\x1a\nsynthetic-image"


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, url: str, body: bytes):
        self.headers = {"content-type": "image/png"}
        self.url = httpx.URL(url)
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def close(self):
        pass

    def iter_bytes(self):
        yield self._body


class _FakeStreamClient:
    def __init__(self, request_headers: list[dict], final_url: str, body: bytes):
        self.request_headers = request_headers
        self.final_url = final_url
        self._body = body

    def stream(self, _method, _url, *, headers):
        self.request_headers.append(dict(headers))
        return _FakeStreamResponse(self.final_url, self._body)

    def close(self):
        pass


def _patch_downloader_transport(monkeypatch, url, body=_SYNTHETIC_PNG):
    """Replace the shared HTTP factory with a fake streaming client.

    Returns the two lists the fake records into: client-construction headers and
    per-request headers, so a test can assert on either without opening a socket.
    """
    factory_headers: list[dict] = []
    request_headers: list[dict] = []

    def fake_http_client(_timeout, **kwargs):
        factory_headers.append(dict(kwargs["headers"]))
        return _FakeStreamClient(request_headers, url, body), False

    monkeypatch.setattr(downloader, "http_client", fake_http_client)
    return factory_headers, request_headers


def test_download_images_writes_manifest_log_scan_can_read(tmp_path, monkeypatch):
    url = "https://assets.example.test/rendered-image"
    _patch_downloader_transport(monkeypatch, url)
    images = tmp_path / "images"

    result = downloader.download_images([url], str(images), {"retries": 0})[0]

    manifest = json.loads((images / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "images": [
            {
                "url": url,
                "path": "assets.example.test/rendered-image.png",
                "bytes": len(_SYNTHETIC_PNG),
            }
        ]
    }

    # A deliberately wrong recorded size must surface once log-scan can find the file.
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "pages.jsonl").write_text(
        json.dumps({"url": url, "size_bytes": len(_SYNTHETIC_PNG) + 1}) + "\n",
        encoding="utf-8",
    )
    scan = logscan.scan(logscan.load_run(str(run_dir), str(images)))
    assert scan["read"]["downloaded_files"] == 1
    assert [a["rule"] for a in scan["anomalies"]] == ["size_matches_file"]
    assert result["ok"] and not result.get("skipped")


def test_download_images_skip_existing_reuses_manifest_for_extensionless_url(tmp_path, monkeypatch):
    url = "https://assets.example.test/rendered-image"
    _, request_headers = _patch_downloader_transport(monkeypatch, url)
    images = tmp_path / "images"

    first = downloader.download_images([url], str(images), {"retries": 0})[0]
    second = downloader.download_images([url], str(images), {"retries": 0})[0]

    assert len(request_headers) == 1  # the second call made no request at all
    assert second["skipped"] is True
    assert second["path"] == first["path"]
    assert not (images / "rendered-image-1.png").exists()


def test_download_images_sends_the_caller_user_agent(tmp_path, monkeypatch):
    url = "https://assets.example.test/rendered-image"
    factory_headers, request_headers = _patch_downloader_transport(monkeypatch, url)
    images = tmp_path / "images"

    downloader.download_images([url], str(images), {"retries": 0, "user_agent": "AuditAgent/1.0"})

    assert factory_headers[0]["User-Agent"] == "AuditAgent/1.0"
    assert request_headers[0]["User-Agent"] == "AuditAgent/1.0"


# ── clusterer (local, needs sklearn) ─────────────────────────────────────────


def test_clusterer_groups_keywords():
    res = clusterer.run_clusterer(
        {
            "keywords": ["buy shoes", "cheap shoes", "seo audit", "technical seo audit"],
            "algorithm": "kmeans",
            "n_clusters": 2,
        }
    )
    assert res["count"] == 2
    assert sum(len(c["keywords"]) for c in res["clusters"]) == 4
