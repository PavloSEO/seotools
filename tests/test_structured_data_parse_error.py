"""STRUCTURED_DATA_PARSE_ERROR (#386): found and parsed JSON-LD block counts were already
recorded per page; this reads the two together and fires only when found exceeds parsed --
a page with no structured data at all (0 found, 0 parsed) must stay silent.
"""

from __future__ import annotations

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


_MALFORMED_JSONLD_PAGE = (
    "<html><head><title>Broken schema</title>"
    '<script type="application/ld+json">{"@type": "Article", "name": "X"</script>'
    f"</head><body>{_page('')}</body></html>"
)

_VALID_JSONLD_PAGE = (
    "<html><head><title>Valid schema</title>"
    '<script type="application/ld+json">{"@type": "Article", "name": "X"}</script>'
    f"</head><body>{_page('')}</body></html>"
)

_NO_JSONLD_PAGE = f"<html><head><title>No schema</title></head><body>{_page('')}</body></html>"


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


def test_structured_data_parse_error_fires_only_for_the_malformed_block():
    mapping = {
        "https://example.com/broken": _FakeResponse(
            _MALFORMED_JSONLD_PAGE, {"content-type": "text/html"}
        ),
        "https://example.com/valid": _FakeResponse(
            _VALID_JSONLD_PAGE, {"content-type": "text/html"}
        ),
        "https://example.com/none": _FakeResponse(_NO_JSONLD_PAGE, {"content-type": "text/html"}),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("STRUCTURED_DATA_PARSE_ERROR", set()) == {"https://example.com/broken"}


def test_structured_data_parse_error_details_carry_both_counts():
    mapping = {
        "https://example.com/broken": _FakeResponse(
            _MALFORMED_JSONLD_PAGE, {"content-type": "text/html"}
        )
    }
    ctx = _run_crawl(mapping)
    issue = next(i for i in ctx.issues if i.check == "STRUCTURED_DATA_PARSE_ERROR")
    assert issue.details["blocks_found"] == 1
    assert issue.details["blocks_parsed"] == 0


def test_structured_data_parse_error_skips_honestly_on_a_plain_sf_export(result):
    skipped = {s.id for s in result.skipped}
    assert "STRUCTURED_DATA_PARSE_ERROR" in skipped
    assert "STRUCTURED_DATA_PARSE_ERROR" not in {i.check for i in result.issues}
