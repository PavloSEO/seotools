"""IMG_MISSING_ALT_ATTRIBUTE and IMG_ALT_TOO_LONG (#386): a native crawl's own per-<img>
inventory distinguishes the alt attribute being absent from a decorative alt="" -- the
issue's own most likely source of a wrong finding. A decorative image with alt="" must
stay silent while a missing alt attribute fires.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules
from seohead.tools.parser import extract_images


def test_missing_alt_attribute_is_distinguished_from_decorative_empty_alt():
    html = (
        "<html><body>"
        '<img src="a.jpg" alt="">'  # decorative, correctly marked -- not missing
        '<img src="b.jpg">'  # the attribute itself is absent
        "</body></html>"
    )
    soup = BeautifulSoup(html, features="lxml")
    images = extract_images(soup)
    assert images[0]["has_alt"] is True
    assert images[0]["alt_length"] == 0
    assert images[1]["has_alt"] is False


def test_alt_length_is_recorded_for_images_that_have_the_attribute():
    long_alt = "x" * 150
    html = f'<html><body><img src="a.jpg" alt="{long_alt}"></body></html>'
    soup = BeautifulSoup(html, features="lxml")
    images = extract_images(soup)
    assert images[0]["alt_length"] == 150


class _FakeResponse:
    def __init__(self, text: str, headers: dict[str, str]):
        self.text = text
        self.status_code = 200
        self.headers = headers


def _page(body: str) -> str:
    return body + ("Enough body text to be a real page. " * 40)


_DECORATIVE_PAGE = f"""<html><head><title>Decorative</title></head>
<body><img src="deco.jpg" alt="">{_page("")}</body></html>"""

_MISSING_ATTR_PAGE = f"""<html><head><title>Missing attribute</title></head>
<body><img src="hero.jpg">{_page("")}</body></html>"""

_LONG_ALT_PAGE = f"""<html><head><title>Long alt</title></head>
<body><img src="hero.jpg" alt="{"x" * 150}">{_page("")}</body></html>"""


def _fetcher(mapping):
    def fetch(url):
        return mapping[url]

    return fetch


def _run_crawl(mapping):
    crawl_result = collect_urls(list(mapping), fetcher=_fetcher(mapping), sleeper=lambda _s: None)
    evidence = build_evidence(crawl_result)
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])
    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))
    run_rules(ctx)
    return ctx


def _fired(ctx) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for issue in ctx.issues:
        out.setdefault(issue.check, set()).add(issue.target_url)
    return out


def test_img_missing_alt_attribute_fires_only_for_the_missing_attribute_page():
    mapping = {
        "https://example.com/decorative": _FakeResponse(
            _DECORATIVE_PAGE, {"content-type": "text/html"}
        ),
        "https://example.com/missing": _FakeResponse(
            _MISSING_ATTR_PAGE, {"content-type": "text/html"}
        ),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("IMG_MISSING_ALT_ATTRIBUTE", set()) == {"https://example.com/missing"}


def test_img_alt_too_long_fires_only_past_the_threshold():
    mapping = {
        "https://example.com/long": _FakeResponse(_LONG_ALT_PAGE, {"content-type": "text/html"}),
        "https://example.com/decorative": _FakeResponse(
            _DECORATIVE_PAGE, {"content-type": "text/html"}
        ),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("IMG_ALT_TOO_LONG", set()) == {"https://example.com/long"}


def test_image_checks_skip_honestly_on_a_plain_sf_export(result):
    """``result`` (conftest.py) is a real-shaped SF export with no per-image inventory."""
    skipped = {s.id for s in result.skipped}
    assert "IMG_MISSING_ALT_ATTRIBUTE" in skipped
    assert "IMG_ALT_TOO_LONG" in skipped
    fired = {i.check for i in result.issues}
    assert "IMG_MISSING_ALT_ATTRIBUTE" not in fired
    assert "IMG_ALT_TOO_LONG" not in fired
