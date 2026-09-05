"""Every key under scope must change what a crawl fetches.

A setting that is validated, recorded in the run manifest, and then read by
nothing is worse than a missing feature: it reports that it took effect.
"""

from __future__ import annotations

import pytest

from seohead.crawl.settings import ConfigError, load, validate
from seohead.crawl.spider import Scope, crawl_site
from tests.test_crawl_spider import FakeResponse, _fetcher, page

SITE = {
    "https://example.com/robots.txt": FakeResponse(
        "User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"}
    ),
    "https://example.com/": page("/blog/post", "/assets/img/logo.jpg", "/shop/item"),
    "https://example.com/blog/post": page(),
    "https://example.com/assets/img/logo.jpg": page(),
    "https://example.com/shop/item": page(),
}

SUBDOMAIN_SITE = {
    "https://example.com/robots.txt": FakeResponse(
        "User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"}
    ),
    "https://shop.example.com/robots.txt": FakeResponse(
        "User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"}
    ),
    "https://example.com/": page("https://shop.example.com/x", "https://other.com/y"),
    "https://shop.example.com/x": page(),
}


def _crawl(mapping, **kw):
    kw.setdefault("sleeper", lambda _s: None)
    kw.setdefault("min_delay", 0)
    return crawl_site("https://example.com/", fetcher=_fetcher(mapping), **kw)


def _urls(result) -> set[str]:
    return {p.url for p in result.pages}


def _fetched(result, url: str) -> bool:
    """Exact-match membership: a URL was fetched or it was not."""
    return any(page.url == url for page in result.pages)


# --- exclude_patterns -------------------------------------------------------
def test_exclude_patterns_keep_assets_out_of_the_budget():
    result = _crawl(SITE, scope={"exclude_patterns": [r"\.jpg$", "/assets/"]})
    assert not _fetched(result, "https://example.com/assets/img/logo.jpg")
    assert _fetched(result, "https://example.com/blog/post")
    assert result.excluded.get("excluded_by_pattern") == 1


def test_without_exclude_patterns_assets_are_fetched():
    # The behaviour the setting is supposed to change.
    assert _fetched(_crawl(SITE), "https://example.com/assets/img/logo.jpg")


# --- include_patterns -------------------------------------------------------
def test_include_patterns_restrict_the_crawl():
    result = _crawl(SITE, scope={"include_patterns": ["/blog/"]})
    assert _urls(result) == {"https://example.com/", "https://example.com/blog/post"}
    assert result.excluded.get("not_included_by_pattern") == 2


def test_the_seed_is_fetched_even_when_it_matches_no_include_pattern():
    # Otherwise a filtered crawl reports an empty site rather than a mistake.
    result = _crawl(SITE, scope={"include_patterns": ["/blog/"]})
    assert _fetched(result, "https://example.com/")


# --- scope.internal ---------------------------------------------------------
def test_host_scope_excludes_subdomains():
    result = _crawl(SUBDOMAIN_SITE)
    assert not _fetched(result, "https://shop.example.com/x")


def test_registrable_domain_scope_includes_subdomains():
    result = _crawl(SUBDOMAIN_SITE, scope={"internal": "registrable_domain"})
    assert _fetched(result, "https://shop.example.com/x")
    assert not _fetched(result, "https://other.com/y")


def test_registrable_domain_scope_does_not_cross_into_another_tenant_of_a_shared_suffix():
    """Issue #144: github.io is a multi-tenant hosting suffix, so a site seeded at
    alice's own subdomain must never treat bob's -- an unrelated customer of the
    same platform -- as in scope just because 'registrable_domain' widens beyond
    'host'."""
    site = {
        "https://alice.github.io/robots.txt": FakeResponse("", status_code=404),
        "https://alice.github.io/": page("https://bob.github.io/"),
    }
    result = crawl_site(
        "https://alice.github.io/",
        fetcher=_fetcher(site),
        sleeper=lambda _s: None,
        min_delay=0,
        scope={"internal": "registrable_domain"},
    )
    assert not _fetched(result, "https://bob.github.io/")
    assert result.excluded.get("outside_host") == 1


# --- exclude_hosts ----------------------------------------------------------
def test_exclude_hosts_wins_over_a_widened_scope():
    result = _crawl(
        SUBDOMAIN_SITE,
        scope={"internal": "registrable_domain", "exclude_hosts": ["shop.example.com"]},
    )
    assert not _fetched(result, "https://shop.example.com/x")
    assert result.excluded.get("excluded_host") == 1


# --- configuration ----------------------------------------------------------
def test_a_pattern_that_does_not_compile_is_rejected_before_the_crawl():
    # A crawl must not fail on its three-hundredth URL over a typo in a setting.
    with pytest.raises(ConfigError, match="not a valid regex"):
        load(overrides={"scope.exclude_patterns": ["["]})


def test_a_valid_pattern_passes_validation():
    config = load(overrides={"scope.exclude_patterns": [r"\.jpg$"]})
    assert config["scope"]["exclude_patterns"] == [r"\.jpg$"]
    validate(config)


def test_scope_reads_an_absent_config_as_the_default():
    assert Scope.from_config(None) == Scope()


# --- scope.segments (#358) --------------------------------------------------


def test_a_url_with_no_segments_declared_is_the_default_segment():
    assert Scope.from_config(None).segment_for("https://example.com/anything") == "default"


def test_segment_for_matches_by_prefix_host_and_pattern():
    rules = Scope.from_config(
        {
            "segments": [
                {"name": "en", "prefix": "/en/"},
                {"name": "shop", "host": "shop.example.com"},
                {"name": "legacy", "pattern": r"/old-\d+$"},
            ]
        }
    )
    assert rules.segment_for("https://example.com/en/about") == "en"
    assert rules.segment_for("https://shop.example.com/cart") == "shop"
    assert rules.segment_for("https://example.com/old-42") == "legacy"
    # Matches none of the three rules -- the built-in fallback, not a crash or a guess.
    assert rules.segment_for("https://example.com/fr/about") == "default"


def test_segment_for_is_first_match_wins_on_overlapping_rules():
    """#21's own specification: overlap must be predictable, decided by order, not by
    which rule happens to be "more specific"."""
    rules = Scope.from_config(
        {
            "segments": [
                {"name": "narrow", "prefix": "/en/help/"},
                {"name": "wide", "prefix": "/en/"},
            ]
        }
    )
    assert rules.segment_for("https://example.com/en/help/faq") == "narrow"
    assert rules.segment_for("https://example.com/en/about") == "wide"


def test_segments_only_fetches_just_that_segment():
    result = _crawl(
        SITE,
        scope={
            "segments": [
                {"name": "blog", "prefix": "/blog/"},
                {"name": "shop", "prefix": "/shop/"},
            ],
            "segments_only": ["blog"],
        },
    )
    assert _fetched(result, "https://example.com/blog/post")
    assert not _fetched(result, "https://example.com/shop/item")
    # The asset also falls outside "blog" (its own segment is the unnamed default),
    # so both it and the shop page are rejected as outside_segment.
    assert result.excluded.get("outside_segment") == 2


def test_the_seed_is_fetched_even_when_segments_only_excludes_its_own_segment():
    # Same invariant as include_patterns: the crawl's own start URL is never
    # filtered out, or a scoped run would report an empty site.
    result = _crawl(
        SITE,
        scope={
            "segments": [{"name": "blog", "prefix": "/blog/"}],
            "segments_only": ["blog"],
        },
    )
    assert _fetched(result, "https://example.com/")


def test_without_segments_only_every_declared_segment_is_fetched():
    result = _crawl(SITE, scope={"segments": [{"name": "blog", "prefix": "/blog/"}]})
    assert _fetched(result, "https://example.com/shop/item")


def test_a_host_segment_widens_internal_scope_for_a_subdomain():
    """A subdomain declared as a segment's host is crawled even under the
    conservative scope.internal='host' default -- the whole point of naming it
    once instead of writing scope.internal='registrable_domain' and hoping
    nothing else on the shared suffix leaks in (#358)."""
    result = _crawl(
        SUBDOMAIN_SITE,
        scope={"segments": [{"name": "shop", "host": "shop.example.com"}]},
    )
    assert _fetched(result, "https://shop.example.com/x")
    assert not _fetched(result, "https://other.com/y")


SEPARATE_DOMAIN_SITE = {
    "https://example.com/robots.txt": FakeResponse(
        "User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"}
    ),
    "https://example.fr/robots.txt": FakeResponse(
        "User-agent: *\nDisallow:\n", headers={"content-type": "text/plain"}
    ),
    "https://example.com/": page("https://example.fr/a-propos"),
    "https://example.fr/a-propos": page(),
}


def test_a_host_segment_allows_crawling_a_wholly_separate_domain():
    """The third shape #358 asks for: a segment is not limited to subfolders or
    subdomains of the seed's own registrable domain."""
    result = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(SEPARATE_DOMAIN_SITE),
        sleeper=lambda _s: None,
        min_delay=0,
        scope={"segments": [{"name": "fr", "host": "example.fr"}]},
    )
    assert _fetched(result, "https://example.fr/a-propos")


def test_without_the_host_segment_the_separate_domain_stays_out_of_scope():
    result = crawl_site(
        "https://example.com/",
        fetcher=_fetcher(SEPARATE_DOMAIN_SITE),
        sleeper=lambda _s: None,
        min_delay=0,
    )
    assert not _fetched(result, "https://example.fr/a-propos")
    assert result.excluded.get("outside_host") == 1
