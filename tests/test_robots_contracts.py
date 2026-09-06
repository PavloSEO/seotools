"""Offline public contracts for robots retrieval and group boundaries."""

from __future__ import annotations

import pytest

from seohead.crawl.spider import crawl_site
from seohead.tools import robots


class Response:
    def __init__(self, text: str = "", status_code: int = 200, content_type: str = "text/plain"):
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}


class Client:
    def __init__(self, response: Response):
        self.response = response

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def get(self, _url: str) -> Response:
        return self.response


@pytest.mark.parametrize("status_code", [429, 503])
def test_unavailable_robots_status_stops_the_spider_and_fails_the_tool(monkeypatch, status_code):
    response = Response(status_code=status_code)
    monkeypatch.setattr(robots, "http_client", lambda *_args, **_kwargs: (Client(response), False))

    checked = robots.check_robots("https://example.com/")
    crawled = crawl_site(
        "https://example.com/",
        min_delay=0,
        sleeper=lambda _delay: None,
        fetcher=lambda url: (
            response if url.endswith("/robots.txt") else Response("<title>Home</title>")
        ),
    )

    assert checked["ok"] is False
    assert checked["status_code"] == status_code
    assert "rules could not be read" in checked["error"]
    assert crawled.finish_reason == "robots_unavailable"
    assert str(status_code) in crawled.stopped_reason


def test_404_robots_remains_a_missing_permissive_file(monkeypatch):
    response = Response(status_code=404)
    monkeypatch.setattr(robots, "http_client", lambda *_args, **_kwargs: (Client(response), False))

    checked = robots.check_robots("https://example.com/")
    crawled = crawl_site(
        "https://example.com/",
        min_delay=0,
        sleeper=lambda _delay: None,
        fetcher=lambda url: (
            response if url.endswith("/robots.txt") else Response("<title>Home</title>")
        ),
    )

    assert checked["ok"] is True
    assert checked["exists"] is False
    assert checked["note"] == "no robots.txt (crawl allowed)"
    assert {page.url for page in crawled.pages} == {"https://example.com/"}


def test_crawl_delay_closes_an_agent_run_before_the_next_agent():
    parsed = robots.parse_robots(
        "User-agent: FirstBot\nCrawl-delay: 10,5\n"
        "User-agent: SecondBot\nCrawl-delay: 2\nDisallow: /second-only/\n"
    )

    assert parsed["groups"] == [
        {
            "user_agents": ["FirstBot"],
            "allow": [],
            "disallow": [],
            "crawl_delay": 10.5,
            "request_rate_delay": None,
        },
        {
            "user_agents": ["SecondBot"],
            "allow": [],
            "disallow": ["/second-only/"],
            "crawl_delay": 2.0,
            "request_rate_delay": None,
        },
    ]
    assert robots.crawl_delay(parsed, "FirstBot") == 10.5
    assert robots.crawl_delay(parsed, "SecondBot") == 2.0
    assert robots.is_allowed(parsed, "/second-only/", "FirstBot") is True
    assert robots.is_allowed(parsed, "/second-only/", "SecondBot") is False


@pytest.mark.parametrize("delay", ["soon", "Infinity", "NaN", "-1"])
def test_malformed_crawl_delay_still_closes_an_agent_run(delay):
    parsed = robots.parse_robots(
        f"User-agent: FirstBot\nCrawl-delay: {delay}\n"
        "User-agent: SecondBot\nDisallow: /second-only/\n"
    )

    assert robots.crawl_delay(parsed, "FirstBot") is None
    assert robots.is_allowed(parsed, "/second-only/", "FirstBot") is True
    assert robots.is_allowed(parsed, "/second-only/", "SecondBot") is False


def test_repeated_agent_groups_after_crawl_delay_still_combine():
    parsed = robots.parse_robots(
        "User-agent: ExampleBot\nCrawl-delay: 1\nUser-agent: ExampleBot\nDisallow: /private/\n"
    )

    assert len(parsed["groups"]) == 2
    assert robots.crawl_delay(parsed, "ExampleBot") == 1.0
    assert robots.is_allowed(parsed, "/private/", "ExampleBot") is False


def test_sitemap_does_not_close_an_agent_run():
    parsed = robots.parse_robots(
        "User-agent: FirstBot\nSitemap: https://example.com/sitemap.xml\n"
        "User-agent: SecondBot\nDisallow: /private/\n"
    )

    assert parsed["groups"] == [
        {
            "user_agents": ["FirstBot", "SecondBot"],
            "allow": [],
            "disallow": ["/private/"],
            "crawl_delay": None,
            "request_rate_delay": None,
        }
    ]


def test_spider_uses_its_selected_agent_crawl_delay():
    robots_text = (
        "User-agent: SEOHEAD-Tools\nCrawl-delay: 10\nUser-agent: OtherBot\nCrawl-delay: 2\n"
    )

    result = crawl_site(
        "https://example.com/",
        min_delay=0,
        sleeper=lambda _delay: None,
        fetcher=lambda url: (
            Response(robots_text)
            if url.endswith("/robots.txt")
            else Response("<title>Home</title>")
        ),
    )

    assert result.crawl_delay_applied == 10.0
    assert result.effective_delay >= 10.0
