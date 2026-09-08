"""Regression tests for the fetch/analyze split and honest tag coverage (issue #20).

``analyze_tech`` must run on an already-fetched document with zero network
access, capture tag identifiers (not just names), and stamp how the document
was measured. ``tag_coverage`` aggregates those results site-wide, excluding
failed fetches from the denominator and crediting a tag manager for tags it is
expected to inject.
"""

import httpx

from seohead.recon import tech

GA4_HTML = (
    "<html><head>"
    '<script src="https://www.googletagmanager.com/gtag/js?id=G-ABC1234567"></script>'
    "</head><body>ok</body></html>"
)
GTM_HTML = (
    "<html><head>"
    '<script src="https://www.googletagmanager.com/gtm.js?id=GTM-WXYZ12"></script>'
    "</head><body>ok</body></html>"
)
METRIKA_HTML = (
    "<html><head>"
    '<script>ym(12345678, "init", {});</script>'
    '<script src="https://mc.yandex.ru/metrika/tag.js"></script>'
    "</head><body>ok</body></html>"
)
PLAIN_HTML = "<html><head></head><body>nothing here</body></html>"


def test_analyze_tech_makes_no_network_request(monkeypatch):
    """A document handed to analyze_tech is never fetched again."""

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("analyze_tech must not open an HTTP connection")

    monkeypatch.setattr(tech, "http_client", _forbidden)
    result = tech.analyze_tech(
        GA4_HTML,
        headers={"content-type": "text/html"},
        url="https://example.com/",
        status_code=200,
    )
    assert result["ok"] is True
    assert result["url"] == "https://example.com/"


def test_analyze_tech_captures_identifier_not_just_name():
    result = tech.analyze_tech(GA4_HTML, url="https://example.com/")
    entry = next(t for t in result["technologies"] if t["name"] == "Google Analytics 4")
    assert entry["identifiers"] == ["G-ABC1234567"]


def test_analyze_tech_stamps_measurement_representation():
    static = tech.analyze_tech(PLAIN_HTML, url="https://example.com/")
    assert static["measurement"] == {"representation": "static_markup", "script_executed": False}

    rendered = tech.analyze_tech(PLAIN_HTML, url="https://example.com/", rendered=True)
    assert rendered["measurement"] == {"representation": "rendered_dom", "script_executed": True}


def test_detect_tech_delegates_to_analyze_tech(monkeypatch):
    """The network-fetching entry point still returns the same shape as before."""

    class _FakeResponse:
        def __init__(self):
            self.text = GTM_HTML
            self.headers = {"content-type": "text/html"}
            self.cookies = httpx.Cookies()
            self.url = "https://example.com/"
            self.status_code = 200

    class _FakeClient:
        def get(self, _url):
            return _FakeResponse()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(tech, "http_client", lambda timeout: (_FakeClient(), None))
    result = tech.detect_tech("https://example.com/")
    assert result["ok"] is True
    entry = next(t for t in result["technologies"] if t["name"] == "Google Tag Manager")
    assert entry["identifiers"] == ["GTM-WXYZ12"]


def test_tag_coverage_excludes_failed_fetches_from_denominator():
    pages = [
        tech.analyze_tech(GA4_HTML, url="https://example.com/a"),
        tech.analyze_tech(PLAIN_HTML, url="https://example.com/b"),
        {"ok": False, "url": "https://example.com/c", "error": "timeout"},
    ]
    report = tech.tag_coverage(pages, tags=("Google Analytics 4",))
    assert report["pages_considered"] == 2
    assert report["pages_excluded_fetch_failed"] == 1
    row = report["tags"][0]
    assert row["pages_with_tag"] == 1
    assert row["fraction"] == 0.5


def test_tag_coverage_credits_tag_manager_injection_as_correct():
    """A page with GTM and no direct GA4 tag is flagged as likely-injected, not broken."""
    pages = [
        tech.analyze_tech(GTM_HTML, url="https://example.com/a"),
        tech.analyze_tech(PLAIN_HTML, url="https://example.com/b"),
    ]
    report = tech.tag_coverage(pages, tags=("Google Analytics 4",))
    row = report["tags"][0]
    assert row["pages_with_tag"] == 0
    template = tech._url_template("https://example.com/a")
    assert row["by_template"][template]["likely_injected_by_manager"] == 1


def test_tag_coverage_flags_conflicting_identifiers_across_the_site():
    other_ga4_html = GA4_HTML.replace("G-ABC1234567", "G-DIFFERENT9")
    pages = [
        tech.analyze_tech(GA4_HTML, url="https://example.com/a"),
        tech.analyze_tech(other_ga4_html, url="https://example.com/b"),
    ]
    report = tech.tag_coverage(pages, tags=("Google Analytics 4",))
    row = report["tags"][0]
    assert row["conflicting_identifiers"] is True
    assert row["identifiers"] == ["G-ABC1234567", "G-DIFFERENT9"]


def test_tag_coverage_reports_uniform_measurement_stamp():
    pages = [tech.analyze_tech(METRIKA_HTML, url="https://example.com/")]
    report = tech.tag_coverage(pages)
    assert report["measurement_stamp"] == "static_markup"


def test_tag_coverage_reports_mixed_measurement_stamp():
    pages = [
        tech.analyze_tech(PLAIN_HTML, url="https://example.com/a"),
        tech.analyze_tech(PLAIN_HTML, url="https://example.com/b", rendered=True),
    ]
    report = tech.tag_coverage(pages)
    assert report["measurement_stamp"] == ["rendered_dom", "static_markup"]
