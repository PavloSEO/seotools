"""Regression tests for issues #468, #469, #471 in seohead/tools/parser.py."""

from __future__ import annotations

from bs4 import BeautifulSoup

from seohead.tools.parser import document_charset, document_position, parse_html

# --- #468: document_charset() false-positives on coincidental "charset" text ---


def test_charset_ignores_unrelated_meta_mentioning_charset():
    html = """<html><head>
<title>Test page</title>
<meta name="description" content="Learn about charset encoding issues in old browsers.">
</head><body><h1>Hi</h1><p>content</p></body></html>"""
    parsed = parse_html(html, "https://example.com/page")
    assert parsed["charset"] is None


def test_charset_still_finds_real_meta_charset_attribute():
    assert document_charset('<meta charset="utf-8">') == "utf-8"


def test_charset_still_finds_http_equiv_content_type():
    html = '<meta http-equiv="Content-Type" content="text/html; charset=utf-8">'
    assert document_charset(html) == "utf-8"


def test_charset_prefers_real_meta_over_unrelated_mention_earlier_in_head():
    html = """<html><head>
<meta name="description" content="Uses charset weirdly in this description.">
<meta charset="utf-8">
</head><body>hi</body></html>"""
    assert document_charset(html) == "utf-8"


# --- #469: directives_outside_head must require every instance outside <head> ---


def test_directives_outside_head_false_when_one_instance_is_in_head():
    html = """<html><head>
<meta name="robots" content="noindex">
<div>oops</div>
<meta name="robots" content="index,follow">
</head><body>hi</body></html>"""
    soup = BeautifulSoup(html, "lxml")
    result = document_position(soup, html)
    assert result["directives_outside_head"] is not True


def test_directives_outside_head_true_when_all_instances_outside_head():
    """Negative control: a real positive must keep firing."""
    html = """<html><head></head><body>
<meta name="robots" content="noindex">
</body></html>"""
    soup = BeautifulSoup(html, "lxml")
    result = document_position(soup, html)
    assert result["directives_outside_head"] is True


# --- #471: only http(s) hrefs should be treated as ordinary links ---


def test_non_http_schemes_are_excluded_from_links():
    html = """
<html><body>
<a href="sms:+15551234567">Text us</a>
<a href="skype:live.foo?call">Call on Skype</a>
<a href="mailto:a@b.com">Mail (control)</a>
</body></html>
"""
    parsed = parse_html(html, "https://example.com/page")
    hrefs = [link["raw_href"] for link in parsed["links"]]
    assert "sms:+15551234567" not in hrefs
    assert "skype:live.foo?call" not in hrefs
    assert "mailto:a@b.com" not in hrefs


def test_whatsapp_scheme_link_never_reads_external_via_fake_hostname():
    html = '<a href="whatsapp://send?phone=15551234567">WhatsApp</a>'
    parsed = parse_html(html, "https://example.com/page")
    assert all(link["href"] != "whatsapp://send?phone=15551234567" for link in parsed["links"])
    assert not any(
        link.get("external") and "send" in link.get("href", "") for link in parsed["links"]
    )


def test_normal_http_links_still_extracted_with_correct_external_flag():
    html = """
<html><body>
<a href="https://other.example.org/x">external</a>
<a href="/internal">internal</a>
</body></html>
"""
    parsed = parse_html(html, "https://example.com/page")
    by_href = {link["raw_href"]: link for link in parsed["links"]}
    assert by_href["https://other.example.org/x"]["external"] is True
    assert by_href["/internal"]["external"] is False
