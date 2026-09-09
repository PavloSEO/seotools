"""The documentation fixture publishes its own bound loopback origin."""

from __future__ import annotations

import httpx

from seohead.tools import sitemap
from seohead.tools.robots import parse_robots
from tests.doc_fixtures.site_server import SITE_DIR, run_fixture_site


def test_fixture_sitemap_and_robots_rewrite_only_the_served_origin(monkeypatch):
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    with run_fixture_site() as origin, httpx.Client() as client:
        baseline = sitemap.parse_sitemap(
            (SITE_DIR / "sitemap.xml").read_bytes(), f"{origin}/sitemap.xml"
        )
        assert {entry["loc"] for entry in baseline["urls"]} == {
            "http://127.0.0.1/",
            "http://127.0.0.1/page",
            "http://127.0.0.1/about",
        }

        for path in ("/sitemap.xml", "/robots.txt"):
            get = client.get(f"{origin}{path}")
            head = client.head(f"{origin}{path}")
            assert get.status_code == head.status_code == 200
            assert get.headers["content-type"] == head.headers["content-type"]
            assert (
                int(get.headers["content-length"])
                == int(head.headers["content-length"])
                == len(get.content)
            )
            assert origin in get.text

        parsed = sitemap.parse_sitemap(
            client.get(f"{origin}/sitemap.xml").content, f"{origin}/sitemap.xml"
        )
        assert {entry["loc"] for entry in parsed["urls"]} == {
            f"{origin}/",
            f"{origin}/page",
            f"{origin}/about",
        }
        assert parse_robots(client.get(f"{origin}/robots.txt").text)["sitemaps"] == [
            f"{origin}/sitemap.xml"
        ]

        crawled = sitemap.crawl(f"{origin}/sitemap.xml")
        assert crawled["errors"] == []
        assert {entry["loc"] for entry in crawled["urls"]} == {
            f"{origin}/",
            f"{origin}/page",
            f"{origin}/about",
        }
