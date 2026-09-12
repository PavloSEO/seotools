"""Root sitemap discovery and failure-state contracts for #703."""

from __future__ import annotations

import httpx

from seohead.tools import sitemap


def _crawl(monkeypatch, responses: dict[str, httpx.Response]) -> dict:
    def responder(request: httpx.Request) -> httpx.Response:
        response = responses.get(str(request.url))
        return response if response is not None else httpx.Response(404, request=request)

    def client(*_args, **kwargs):
        return httpx.Client(transport=httpx.MockTransport(responder), **kwargs), False

    monkeypatch.setattr(sitemap, "http_client", client)
    return sitemap.crawl("https://example.test/")


def test_root_discovers_robots_sitemap_and_records_its_source(monkeypatch):
    robots = "Sitemap: https://example.test/declared.xml\n"
    xml = b"<urlset><url><loc>https://example.test/page</loc></url></urlset>"

    result = _crawl(
        monkeypatch,
        {
            "https://example.test/robots.txt": httpx.Response(200, text=robots),
            "https://example.test/declared.xml": httpx.Response(200, content=xml),
        },
    )

    assert result["ok"] is True
    assert result["urls"] == [
        {
            "loc": "https://example.test/page",
            "loc_normalized": "https://example.test/page",
            "lastmod": None,
            "changefreq": None,
            "priority": None,
        }
    ]
    assert result["sitemaps"][0]["source"] == "robots.txt"


def test_root_reports_the_robots_and_fallback_attempts_when_nothing_is_available(monkeypatch):
    result = _crawl(
        monkeypatch,
        {
            "https://example.test/robots.txt": httpx.Response(404),
            "https://example.test/sitemap.xml": httpx.Response(404),
        },
    )

    assert result["ok"] is False
    assert {error["url"] for error in result["errors"]} == {
        "https://example.test/robots.txt",
        "https://example.test/sitemap.xml",
    }


def test_partial_sitemap_success_stays_ok_and_names_the_malformed_source(monkeypatch):
    robots = "\n".join(
        [
            "Sitemap: https://example.test/good.xml",
            "Sitemap: https://example.test/bad.xml",
        ]
    )
    xml = b"<urlset><url><loc>https://example.test/page</loc></url></urlset>"
    result = _crawl(
        monkeypatch,
        {
            "https://example.test/robots.txt": httpx.Response(200, text=robots),
            "https://example.test/good.xml": httpx.Response(200, content=xml),
            "https://example.test/bad.xml": httpx.Response(
                200, content=b"<html>not a sitemap</html>"
            ),
        },
    )

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["errors"] == [
        {"url": "https://example.test/bad.xml", "error": "Unknown sitemap format"}
    ]


def test_every_malformed_sitemap_is_a_failed_result(monkeypatch):
    robots = "\n".join(
        [
            "Sitemap: https://example.test/one.xml",
            "Sitemap: https://example.test/two.xml",
        ]
    )
    result = _crawl(
        monkeypatch,
        {
            "https://example.test/robots.txt": httpx.Response(200, text=robots),
            "https://example.test/one.xml": httpx.Response(200, content=b"<html>one</html>"),
            "https://example.test/two.xml": httpx.Response(200, content=b"<html>two</html>"),
        },
    )

    assert result["ok"] is False
    assert result["count"] == 0
    assert len(result["errors"]) == 2
