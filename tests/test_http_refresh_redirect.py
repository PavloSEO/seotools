"""HTTP_REFRESH_REDIRECT (#385): the raw HTTP "Refresh" response header, distinct from a
<meta http-equiv="refresh"> element (META_REFRESH_REDIRECT, which reads markup, not headers).
"""

from __future__ import annotations

import pytest

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules


class _FakeResponse:
    def __init__(self, text: str, headers: dict[str, str]):
        self.text = text
        self.status_code = 200
        self.headers = headers


def _page(body: str) -> str:
    return body + ("Enough body text to be a real page. " * 40)


_PLAIN_PAGE = f"<html><head><title>Plain</title></head><body>{_page('')}</body></html>"


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


def test_http_refresh_redirect_fires_only_for_the_header_page():
    mapping = {
        "https://example.com/refresh": _FakeResponse(
            _PLAIN_PAGE,
            {"content-type": "text/html", "refresh": "5; url=https://example.com/new"},
        ),
        "https://example.com/plain": _FakeResponse(_PLAIN_PAGE, {"content-type": "text/html"}),
    }
    ctx = _run_crawl(mapping)
    fired = _fired(ctx)
    assert fired.get("HTTP_REFRESH_REDIRECT", set()) == {"https://example.com/refresh"}
    issue = next(issue for issue in ctx.issues if issue.check == "HTTP_REFRESH_REDIRECT")
    assert issue.details == {"refresh_header": "5; url=https://example.com/new"}


@pytest.mark.parametrize("refresh", ["30", "30; url=", '30; url=""'])
def test_http_refresh_delay_or_empty_target_is_not_a_redirect(refresh):
    """The real collector/evidence/rules path must not turn a page reload into a redirect."""
    mapping = {
        "https://example.com/reload": _FakeResponse(
            _PLAIN_PAGE,
            {"content-type": "text/html", "refresh": refresh},
        )
    }

    ctx = _run_crawl(mapping)
    assert _fired(ctx).get("HTTP_REFRESH_REDIRECT", set()) == set()


def test_http_refresh_redirect_is_distinct_from_a_meta_refresh_element():
    """A page whose only redirect mechanism is a <meta http-equiv="refresh"> element in the
    markup -- no Refresh response header at all -- must not trip the header-based check;
    the two read different evidence (a response header vs. an HTML element)."""
    meta_refresh_page = (
        f"<html><head><title>Meta refresh</title>"
        f'<meta http-equiv="refresh" content="5; url=https://example.com/new"></head>'
        f"<body>{_page('')}</body></html>"
    )
    mapping = {
        "https://example.com/meta": _FakeResponse(meta_refresh_page, {"content-type": "text/html"})
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("HTTP_REFRESH_REDIRECT", set()) == set()


def test_http_refresh_redirect_skips_honestly_on_a_plain_sf_export(result):
    skipped = {s.id for s in result.skipped}
    assert "HTTP_REFRESH_REDIRECT" in skipped
    assert "HTTP_REFRESH_REDIRECT" not in {i.check for i in result.issues}
