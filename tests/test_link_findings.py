"""Offline tests for the link/form security findings (issue #125)."""

from seohead.crawl.link_findings import (
    follow_and_nofollow_inlinks,
    form_url_insecure,
    forms_on_http_pages_with_password,
    outlinks_to_localhost,
    protocol_relative_links,
    unsafe_cross_origin_links,
)
from seohead.crawl.spider import FormEdge, LinkEdge


def edge(source, destination, *, nofollow=False, rel=(), target="", raw_href=""):
    return LinkEdge(
        source=source,
        destination=destination,
        anchor="",
        nofollow=nofollow,
        rel=rel,
        target=target,
        raw_href=raw_href,
    )


# ── outlinks_to_localhost ─────────────────────────────────────────────────────


def test_localhost_hostname_is_flagged():
    links = [edge("https://example.com/", "http://localhost:8000/debug")]
    found = outlinks_to_localhost(links)
    assert found == [{"target_url": "https://example.com/", "destination": links[0].destination}]


def test_loopback_ip_literal_is_flagged():
    links = [edge("https://example.com/", "http://127.0.0.1/admin")]
    assert len(outlinks_to_localhost(links)) == 1


def test_ipv6_loopback_is_flagged():
    links = [edge("https://example.com/", "http://[::1]/x")]
    assert len(outlinks_to_localhost(links)) == 1


def test_ordinary_external_host_is_not_flagged():
    links = [edge("https://example.com/", "https://other.example/x")]
    assert outlinks_to_localhost(links) == []


def test_dotted_localhost_subdomain_is_flagged():
    links = [edge("https://example.com/", "http://api.localhost/x")]
    assert len(outlinks_to_localhost(links)) == 1


# ── unsafe_cross_origin_links ─────────────────────────────────────────────────


def test_blank_target_without_noopener_or_noreferrer_is_unsafe():
    links = [edge("https://example.com/", "https://other.example/", target="_blank")]
    assert len(unsafe_cross_origin_links(links)) == 1


def test_blank_target_with_noopener_is_safe():
    links = [
        edge("https://example.com/", "https://other.example/", target="_blank", rel=("noopener",))
    ]
    assert unsafe_cross_origin_links(links) == []


def test_blank_target_with_noreferrer_is_also_safe():
    links = [
        edge("https://example.com/", "https://other.example/", target="_blank", rel=("noreferrer",))
    ]
    assert unsafe_cross_origin_links(links) == []


def test_ordinary_target_is_never_flagged():
    links = [edge("https://example.com/", "https://other.example/", target="_self")]
    assert unsafe_cross_origin_links(links) == []


def test_edge_with_no_captured_attributes_is_not_a_false_positive():
    """capture_attributes off: target/rel default to '' / () rather than reporting unsafe."""
    links = [edge("https://example.com/", "https://other.example/")]
    assert unsafe_cross_origin_links(links) == []


# ── unsafe_cross_origin_links: origin comparison (issue #336) ─────────────────


def test_same_origin_blank_target_without_rel_is_not_flagged():
    """A same-origin new-tab link has no window.opener handle to a *different* origin."""
    links = [edge("https://example.test/", "https://example.test/account", target="_blank")]
    assert unsafe_cross_origin_links(links) == []


def test_different_host_blank_target_without_rel_remains_flagged():
    links = [edge("https://example.test/", "https://other.example/account", target="_blank")]
    assert len(unsafe_cross_origin_links(links)) == 1


def test_different_scheme_same_host_is_cross_origin():
    links = [edge("https://example.test/", "http://example.test/account", target="_blank")]
    assert len(unsafe_cross_origin_links(links)) == 1


def test_different_explicit_port_same_host_is_cross_origin():
    links = [edge("https://example.test/", "https://example.test:8443/account", target="_blank")]
    assert len(unsafe_cross_origin_links(links)) == 1


def test_explicit_default_port_is_same_origin_as_implicit():
    """https://host:443/ names the same origin as https://host/ -- 443 is implicit."""
    links = [edge("https://example.test/", "https://example.test:443/account", target="_blank")]
    assert unsafe_cross_origin_links(links) == []


def test_explicit_default_http_port_is_same_origin_as_implicit():
    links = [edge("http://example.test/", "http://example.test:80/account", target="_blank")]
    assert unsafe_cross_origin_links(links) == []


def test_noopener_suppresses_cross_origin_finding():
    links = [
        edge(
            "https://example.test/",
            "https://other.example/account",
            target="_blank",
            rel=("noopener",),
        )
    ]
    assert unsafe_cross_origin_links(links) == []


def test_noreferrer_suppresses_cross_origin_finding():
    links = [
        edge(
            "https://example.test/",
            "https://other.example/account",
            target="_blank",
            rel=("noreferrer",),
        )
    ]
    assert unsafe_cross_origin_links(links) == []


# ── protocol_relative_links ───────────────────────────────────────────────────


def test_protocol_relative_href_is_flagged():
    links = [
        edge(
            "https://example.com/",
            "https://cdn.example/x.js",
            raw_href="//cdn.example/x.js",
        )
    ]
    found = protocol_relative_links(links)
    assert found == [
        {
            "target_url": "https://example.com/",
            "destination": "https://cdn.example/x.js",
            "raw_href": "//cdn.example/x.js",
        }
    ]


def test_ordinary_relative_href_is_not_flagged():
    links = [edge("https://example.com/", "https://example.com/a", raw_href="/a")]
    assert protocol_relative_links(links) == []


def test_absolute_href_is_not_flagged():
    links = [
        edge(
            "https://example.com/",
            "https://other.example/x",
            raw_href="https://other.example/x",
        )
    ]
    assert protocol_relative_links(links) == []


def test_unmeasured_raw_href_is_not_a_false_positive():
    links = [edge("https://example.com/", "https://other.example/x")]
    assert protocol_relative_links(links) == []


# ── follow_and_nofollow_inlinks ───────────────────────────────────────────────


def test_page_linked_both_follow_and_nofollow_is_flagged():
    links = [
        edge("https://example.com/a", "https://example.com/target", nofollow=False),
        edge("https://example.com/b", "https://example.com/target", nofollow=True),
    ]
    assert follow_and_nofollow_inlinks(links, "example.com") == ["https://example.com/target"]


def test_page_linked_only_followed_is_not_flagged():
    links = [edge("https://example.com/a", "https://example.com/target", nofollow=False)]
    assert follow_and_nofollow_inlinks(links, "example.com") == []


def test_external_destination_is_ignored():
    """Internal only, matching the SF issue's own scope."""
    links = [
        edge("https://example.com/a", "https://other.example/x", nofollow=False),
        edge("https://example.com/b", "https://other.example/x", nofollow=True),
    ]
    assert follow_and_nofollow_inlinks(links, "example.com") == []


def test_host_match_is_case_insensitive():
    links = [
        edge("https://example.com/a", "https://example.com/target", nofollow=False),
        edge("https://example.com/b", "https://example.com/target", nofollow=True),
    ]
    assert follow_and_nofollow_inlinks(links, "EXAMPLE.COM") == ["https://example.com/target"]


# ── form_url_insecure ─────────────────────────────────────────────────────────


def test_http_action_is_flagged_regardless_of_page_scheme():
    forms = [
        FormEdge(
            page="https://example.com/",
            method="post",
            action="http://x.example/submit",
            has_password=False,
        )
    ]
    found = form_url_insecure(forms)
    assert found == [
        {
            "target_url": "https://example.com/",
            "action": "http://x.example/submit",
            "method": "post",
        }
    ]


def test_https_action_is_not_flagged():
    forms = [
        FormEdge(
            page="https://example.com/",
            method="post",
            action="https://x.example/submit",
            has_password=False,
        )
    ]
    assert form_url_insecure(forms) == []


# ── forms_on_http_pages_with_password ─────────────────────────────────────────


def test_password_form_on_http_page_is_flagged():
    forms = [
        FormEdge(page="http://example.com/login", method="post", action="/login", has_password=True)
    ]
    assert forms_on_http_pages_with_password(forms) == [
        {"target_url": "http://example.com/login", "action": "/login"}
    ]


def test_password_form_on_https_page_is_not_flagged():
    forms = [
        FormEdge(
            page="https://example.com/login", method="post", action="/login", has_password=True
        )
    ]
    assert forms_on_http_pages_with_password(forms) == []


def test_non_password_form_on_http_page_is_not_flagged():
    forms = [
        FormEdge(page="http://example.com/search", method="get", action="/s", has_password=False)
    ]
    assert forms_on_http_pages_with_password(forms) == []
