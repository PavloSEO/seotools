"""Breadth-first link discovery on a synthetic site. No network."""

import pytest

from seohead.crawl.spider import crawl_site

ROBOTS_OK = "User-agent: *\nDisallow: /private/\n"


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}


def page(*links: str, title: str = "t") -> FakeResponse:
    """An HTML response linking to each href, in document order."""
    body = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return FakeResponse(
        f"<html><head><title>{title}</title></head><body><h1>{title}</h1>{body}</body></html>"
    )


SITE = {
    "https://example.com/robots.txt": FakeResponse(
        ROBOTS_OK, headers={"content-type": "text/plain"}
    ),
    "https://example.com/": page("/a", "/b", "https://other.com/x"),
    "https://example.com/a": page("/c"),
    "https://example.com/b": page("/c"),
    "https://example.com/c": page(),
    "https://example.com/private/secret": page(),
}


def _fetcher(mapping):
    def fetch(url):
        value = mapping.get(url)
        if value is None:
            return FakeResponse("", status_code=404)
        if isinstance(value, Exception):
            raise value
        return value

    return fetch


def _crawl(mapping=None, **kw):
    kw.setdefault("sleeper", lambda _s: None)
    kw.setdefault("min_delay", 0)
    return crawl_site("https://example.com/", fetcher=_fetcher(mapping or SITE), **kw)


def test_follows_links_and_finds_the_whole_site():
    result = _crawl()
    found = {p.url for p in result.pages}
    assert found == {
        "https://example.com/",
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    }


def test_visits_each_url_once_even_when_linked_twice():
    result = _crawl()
    urls = [p.url for p in result.pages]
    assert len(urls) == len(set(urls))


def test_a_fragment_link_is_not_its_own_page():
    """#194: a fragment never selects a distinct server resource, so a
    fragment-bearing link must resolve to the same request and the same
    PageRecord as its fragment-free sibling, not become its own page."""
    requests: list[str] = []

    def fetch(url):
        requests.append(url)
        if url.endswith("/robots.txt"):
            return FakeResponse(
                "User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"}
            )
        if url == "https://example.com/":
            return FakeResponse('<a href="/about#team">About</a><a href="/about">Plain</a>')
        return FakeResponse("<title>About</title>")

    result = crawl_site("https://example.com/", fetcher=fetch, sleeper=lambda _s: None, min_delay=0)
    assert requests.count("https://example.com/about") == 1
    assert "https://example.com/about#team" not in requests
    urls = [p.url for p in result.pages]
    assert urls.count("https://example.com/about") == 1
    assert "https://example.com/about#team" not in urls
    # Both anchors are still real evidence of what the page links to, fragment
    # and all -- only the frontier and the fetched identity are deduplicated.
    assert {e.destination for e in result.links} == {
        "https://example.com/about#team",
        "https://example.com/about",
    }


def test_a_fragment_variant_cannot_displace_a_later_unique_url():
    """#194: with a small URL budget, several fragment variants of one page must
    cost the frontier one slot, not one each, or they crowd out a distinct URL
    discovered right after them."""
    site = {
        "https://example.com/robots.txt": FakeResponse(
            "User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"}
        ),
        "https://example.com/": page("/guide#first", "/guide#second", "/other"),
        "https://example.com/guide": page(),
        "https://example.com/other": page(),
    }
    result = _crawl(site, max_urls=3)
    urls = {p.url for p in result.pages}
    assert urls == {
        "https://example.com/",
        "https://example.com/guide",
        "https://example.com/other",
    }


def test_external_links_are_recorded_but_never_fetched():
    result = _crawl()
    assert "https://other.com/x" not in {p.url for p in result.pages}
    assert any(edge.destination == "https://other.com/x" for edge in result.links)
    assert result.excluded.get("outside_host", 0) >= 1


def test_depth_is_recorded_and_bounded():
    result = _crawl(max_depth=1)
    depths = {p.url: p.crawl_depth for p in result.pages}
    assert depths["https://example.com/"] == 0
    assert depths["https://example.com/a"] == 1
    assert "https://example.com/c" not in depths
    assert result.excluded.get("depth_limit", 0) >= 1


def test_a_redirect_target_beyond_max_depth_is_recorded_as_excluded():
    """#464: handle_redirect must record the identical condition handle_links
    already does for depth >= depth_limit, instead of dropping the redirect
    target with no trace."""
    site = dict(SITE)
    site["https://example.com/"] = FakeResponse(
        "", status_code=301, headers={"location": "https://example.com/target"}
    )
    result = _crawl(site, max_depth=0)
    assert result.excluded.get("depth_limit", 0) >= 1
    assert "https://example.com/target" not in {p.url for p in result.pages}


def test_a_redirect_one_hop_past_the_last_permitted_depth_is_excluded():
    """#464, second reproduction: the start page links to a page at the last
    permitted depth that itself redirects one hop further."""
    site = dict(SITE)
    site["https://example.com/"] = page("/a")
    site["https://example.com/a"] = FakeResponse(
        "", status_code=301, headers={"location": "https://example.com/target"}
    )
    result = _crawl(site, max_depth=1)
    assert result.excluded.get("depth_limit", 0) >= 1
    assert "https://example.com/target" not in {p.url for p in result.pages}


def test_a_redirect_within_the_depth_budget_is_still_enqueued():
    """#464 negative control: a redirect discovered within budget keeps being
    enqueued exactly as before, and must not spuriously appear as excluded."""
    site = {
        "https://example.com/robots.txt": FakeResponse(
            ROBOTS_OK, headers={"content-type": "text/plain"}
        ),
        "https://example.com/": FakeResponse(
            "", status_code=301, headers={"location": "https://example.com/target"}
        ),
        "https://example.com/target": page(),
    }
    result = _crawl(site, max_depth=2)
    assert "https://example.com/target" in {p.url for p in result.pages}
    assert result.excluded.get("depth_limit", 0) == 0


def test_robots_disallow_is_honoured():
    site = dict(SITE)
    site["https://example.com/"] = page("/a", "/private/secret")
    result = _crawl(site)
    assert "https://example.com/private/secret" not in {p.url for p in result.pages}
    assert result.excluded.get("blocked_by_robots", 0) == 1


def test_a_5xx_robots_stops_the_crawl_rather_than_allowing_it():
    """RFC 9309: an unavailable robots.txt is a full disallow."""
    site = dict(SITE)
    site["https://example.com/robots.txt"] = FakeResponse("", status_code=503)
    result = _crawl(site)
    assert result.pages == []
    assert result.partial is True
    assert "503" in result.stopped_reason


# --- robots.txt redirects (#135): must be followed, but only so far ---------


def test_robots_redirect_is_followed_and_its_disallow_applies():
    site = dict(SITE)
    site["https://example.com/robots.txt"] = FakeResponse(
        "", status_code=301, headers={"location": "/robots-real.txt", "content-type": "text/html"}
    )
    site["https://example.com/robots-real.txt"] = FakeResponse(
        "User-agent: *\nDisallow: /private/\n", headers={"content-type": "text/plain"}
    )
    site["https://example.com/"] = page("/a", "/private/secret")
    result = _crawl(site)
    assert "https://example.com/private/secret" not in {p.url for p in result.pages}
    assert result.robots_blocked == ["https://example.com/private/secret"]
    # The redirect happened where nothing used to say so — robots_note must
    # name it rather than reading like an ordinary, unrestricted robots.txt.
    assert "redirected" in result.robots_note
    assert result.stopped_reason == ""


def test_robots_redirect_off_host_is_not_trusted():
    """A robots.txt fetched from somebody else's server must not govern this crawl."""
    site = dict(SITE)
    site["https://example.com/robots.txt"] = FakeResponse(
        "", status_code=301, headers={"location": "https://other.com/robots.txt"}
    )
    result = _crawl(site)
    assert result.pages == []
    assert result.partial is True
    assert "off-site" in result.stopped_reason


def test_robots_redirect_loop_is_treated_as_unavailable():
    site = dict(SITE)
    site["https://example.com/robots.txt"] = FakeResponse(
        "", status_code=302, headers={"location": "/r2.txt"}
    )
    site["https://example.com/r2.txt"] = FakeResponse(
        "", status_code=302, headers={"location": "/robots.txt"}
    )
    result = _crawl(site)
    assert result.pages == []
    assert result.partial is True
    assert "loop" in result.stopped_reason


def test_robots_redirect_exceeding_the_hop_budget_is_treated_as_unavailable():
    site = dict(SITE)
    site["https://example.com/robots.txt"] = FakeResponse(
        "", status_code=302, headers={"location": "/hop0"}
    )
    for i in range(6):
        site[f"https://example.com/hop{i}"] = FakeResponse(
            "", status_code=302, headers={"location": f"/hop{i + 1}"}
        )
    site["https://example.com/hop6"] = FakeResponse(
        ROBOTS_OK, headers={"content-type": "text/plain"}
    )
    result = _crawl(site)
    assert result.pages == []
    assert result.partial is True
    assert "redirected more than" in result.stopped_reason


def test_robots_redirected_to_a_non_text_plain_body_is_treated_as_unavailable():
    """An HTML page at the redirect target parses to an empty, permissive ruleset
    just like the original bug — that must not pass for "no restrictions"."""
    site = dict(SITE)
    site["https://example.com/robots.txt"] = FakeResponse(
        "", status_code=301, headers={"location": "/login"}
    )
    site["https://example.com/login"] = FakeResponse(
        "<html>please sign in</html>", headers={"content-type": "text/html"}
    )
    result = _crawl(site)
    assert result.pages == []
    assert result.partial is True
    assert "non-text/plain" in result.stopped_reason


def test_robots_redirect_unavailable_does_not_stop_when_configured_not_to():
    site = dict(SITE)
    site["https://example.com/robots.txt"] = FakeResponse(
        "", status_code=301, headers={"location": "https://other.com/robots.txt"}
    )
    result = _crawl(site, unavailable_means_stop=False)
    assert "https://example.com/a" in {p.url for p in result.pages}
    assert result.stopped_reason == ""
    assert "off-site" in result.robots_note


def test_the_url_budget_stops_the_crawl_and_marks_it_partial():
    result = _crawl(max_urls=2)
    assert len(result.pages) == 2
    assert result.partial is True
    assert "url limit" in result.stopped_reason


def test_traversal_is_deterministic_across_runs():
    first = [p.url for p in _crawl().pages]
    second = [p.url for p in _crawl().pages]
    assert first == second


def test_breadth_first_visits_shallow_pages_before_deep_ones():
    result = _crawl()
    depths = [p.crawl_depth for p in result.pages]
    assert depths == sorted(depths), "a BFS frontier must not descend early"


def test_a_same_host_redirect_is_followed_within_the_budget():
    site = dict(SITE)
    site["https://example.com/"] = FakeResponse(
        "",
        status_code=301,
        headers={"location": "https://example.com/a", "content-type": "text/html"},
    )
    result = _crawl(site)
    assert "https://example.com/a" in {p.url for p in result.pages}


def test_an_off_host_redirect_is_recorded_and_not_followed():
    site = dict(SITE)
    site["https://example.com/"] = FakeResponse(
        "", status_code=301, headers={"location": "https://other.com/", "content-type": "text/html"}
    )
    result = _crawl(site)
    assert "https://other.com/" not in {p.url for p in result.pages}
    assert result.excluded.get("redirect_off_host", 0) == 1


def test_repeated_timeouts_stop_the_crawl():
    """A failing origin must be left alone, not walked to the end of the queue."""
    targets = [f"/p{i}" for i in range(9)]
    site = {
        "https://example.com/robots.txt": FakeResponse(ROBOTS_OK),
        "https://example.com/": page(*targets),
    }
    for path in targets:
        site[f"https://example.com{path}"] = TimeoutError("read timed out")
    result = _crawl(site, max_urls=50)
    assert result.partial is True
    assert "timeouts" in result.stopped_reason
    assert len(result.pages) < len(targets), "must stop before exhausting the queue"


def test_the_link_graph_is_collected():
    result = _crawl()
    edges = {(e.source, e.destination) for e in result.links}
    assert ("https://example.com/", "https://example.com/a") in edges
    assert ("https://example.com/a", "https://example.com/c") in edges


def test_links_carry_no_position_by_default():
    result = _crawl()
    assert all(edge.position == "" for edge in result.links)


def test_classify_links_wires_position_into_the_link_graph():
    """Issue #20 part 3: link position classification is wired into the
    spider's own link recording, at no extra requests."""
    site = dict(SITE)
    site["https://example.com/"] = FakeResponse(
        "<html><body>"
        '<nav><a href="/a">A</a></nav>'
        '<footer><a href="/b">B</a></footer>'
        '<p>Body copy <a href="https://other.com/x">X</a></p>'
        "</body></html>"
    )
    result = _crawl(site, classify_links=True)
    by_dest = {edge.destination: edge.position for edge in result.links}
    assert by_dest["https://example.com/a"] == "nav"
    assert by_dest["https://example.com/b"] == "footer"
    assert by_dest["https://other.com/x"] == "content"


@pytest.mark.parametrize("bad", ["", "not a url", "ftp:/"])
def test_a_non_crawlable_start_url_is_refused(bad):
    with pytest.raises(ValueError):
        crawl_site(bad, fetcher=_fetcher(SITE))


# --- robots.txt is matched on path and query, not the path alone ------------
QUERY_SITE = {
    "https://example.com/robots.txt": FakeResponse(
        "User-agent: *\nDisallow: /*?\n", headers={"content-type": "text/plain"}
    ),
    "https://example.com/": page("/blog", "/blog?page=2"),
    "https://example.com/blog": page(),
    "https://example.com/blog?page=2": page(),
}


def test_query_string_disallow_is_enforced():
    # "Disallow: /*?" exists to block query strings; dropping the query before
    # the comparison made it unmatchable and the rule silently inert.
    result = _crawl(QUERY_SITE)
    assert "https://example.com/blog?page=2" not in {p.url for p in result.pages}
    assert result.robots_blocked == ["https://example.com/blog?page=2"]


def test_query_string_disallow_is_reported_under_report_only():
    result = _crawl(QUERY_SITE, robots_policy="report_only")
    assert "https://example.com/blog?page=2" in {p.url for p in result.pages}
    assert result.robots_blocked == ["https://example.com/blog?page=2"]


# --- <template>-only links are never crawled (issue #140) -------------------

TEMPLATE_SITE = {
    "https://example.com/robots.txt": FakeResponse(
        ROBOTS_OK, headers={"content-type": "text/plain"}
    ),
    "https://example.com/": FakeResponse(
        '<html><body><template><a href="/ghost">gone</a></template>'
        "<p>Real page content with enough words for the page to be meaningful.</p>"
        "</body></html>"
    ),
    # No entry for /ghost: fetching it would be an error, proving the spider never tried.
}


def test_template_only_link_is_never_enqueued_or_stored():
    result = _crawl(TEMPLATE_SITE)
    assert {p.url for p in result.pages} == {"https://example.com/"}
    assert result.links == []
