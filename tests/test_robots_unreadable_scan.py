"""An unreadable robots.txt must stay a statement about the site (#629).

``_fetch_robots`` has a specific note for each way robots.txt can fail to be read,
and the crawl's behaviour is decided from ``unavailable``: a 404 means "no
restrictions" and the crawl goes on, anything else means "this host could not say
what is disallowed" and ``robots.unavailable_means_stop`` ends the run with that
note on the last line. Both outcomes need the *parsed* half to be the shape
``parse_robots`` produces, because in SQLite scan mode it is written into the
artifact's ``robots_summary`` context and validated there.
"""

from contextlib import contextmanager

import pytest

from seohead.crawl import sqlite_adapter
from seohead.crawl.collect import fetch_one as real_fetch_one
from seohead.crawl.settings import load
from seohead.crawl.spider import _fetch_robots
from seohead.servers import scan_handlers
from seohead.storage import open_scan
from seohead.tools.robots import parse_robots

PARSED_KEYS = set(parse_robots(""))

PAGE = (
    "<html><head><title>Home</title></head><body><main><h1>Home</h1>"
    "</main></body></html>"
)


class _Response:
    def __init__(self, status_code, text="", content_type="text/html", headers=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": content_type, **(headers or {})}


def _transport(robots_response):
    def fetch(url):
        if url.endswith("/robots.txt"):
            if isinstance(robots_response, Exception):
                raise robots_response
            return robots_response
        return _Response(200, PAGE)

    return fetch


UNREADABLE = {
    "not_found": _Response(404, "", "text/html"),
    "server_error": _Response(503, "", "text/html"),
    "rate_limited": _Response(429, "", "text/html"),
    "off_host_redirect": _Response(
        302, "", "text/html", {"location": "https://cookiesync.example.net/x?to=robots"}
    ),
    "redirect_loop": _Response(
        302, "", "text/html", {"location": "https://example.test/robots.txt"}
    ),
    "unreachable": ConnectionError("connection reset"),
    "html_body": _Response(
        302, "", "text/html", {"location": "https://example.test/robots-page"}
    ),
}


@pytest.mark.parametrize("case", sorted(UNREADABLE))
def test_every_unreadable_robots_returns_the_parsed_shape(case):
    """The stand-in for a ruleset that could not be read is still a ruleset.

    A different key set is not an empty ruleset: it is one the scan artifact
    refuses to store, and the crawl then dies on an internal invariant instead of
    reporting what happened to robots.txt.
    """
    parsed, note, _unavailable = _fetch_robots(
        "https://example.test/", _transport(UNREADABLE[case]), None
    )
    assert set(parsed) == PARSED_KEYS, f"{case}: {note}"


def test_a_parseable_robots_still_parses():
    """The silent half: the success path was never broken and must stay unchanged."""
    robots = _Response(
        200,
        "User-agent: *\nDisallow: /private\nSitemap: https://example.test/sitemap.xml\n",
        "text/plain",
    )
    parsed, _note, unavailable = _fetch_robots(
        "https://example.test/", _transport(robots), None
    )
    assert unavailable is False
    assert parsed["groups"][0]["disallow"] == ["/private"]
    assert parsed["sitemaps"] == ["https://example.test/sitemap.xml"]


def test_a_missing_robots_txt_does_not_stop_a_scan_crawl(tmp_path, monkeypatch):
    """404 means no restrictions (RFC 9309), and the artifact records that.

    This is the end-to-end half of the shape check above: the run reaches
    ``write_context``, which is where the wrong shape used to surface as
    ``native parsed robots summary is invalid``.
    """
    fetch = _transport(UNREADABLE["not_found"])

    @contextmanager
    def no_client(*_args, **_kwargs):
        yield None

    monkeypatch.setattr(sqlite_adapter, "_client_context", no_client)
    monkeypatch.setattr(
        sqlite_adapter,
        "fetch_one",
        lambda url, **kwargs: real_fetch_one(url, **{**kwargs, "fetcher": fetch}),
    )
    monkeypatch.setattr(
        sqlite_adapter,
        "_fetch_robots",
        lambda start, _fetcher, _client, wait=None: _fetch_robots(
            start, fetch, None, wait=wait
        ),
    )

    scan = tmp_path / "scan.sqlite"
    result = scan_handlers.crawl_site_scan(
        "https://example.test/",
        scan_out=str(scan),
        settings=load(overrides={"speed.min_delay_seconds": 0.0}),
        producer_build="a" * 40,
    )

    assert result["urls_collected"] == 1
    with open_scan(scan) as con:
        reason = con.execute(
            "SELECT reason FROM context_items WHERE kind='robots_summary'"
        ).fetchone()[0]
    assert reason == "no robots.txt"
