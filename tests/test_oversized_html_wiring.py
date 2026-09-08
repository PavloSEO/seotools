"""End-to-end regression for #243: an oversized, unparsed HTML body must not be
reported as compliant metadata that happens to be missing.

PR #407 recorded ``PageRecord.body_unavailable`` but stopped there -- nothing
downstream read it yet, so ``TITLE_MISSING``/``DESC_MISSING``/``H1_MISSING``/
``CANONICAL_MISSING`` still fired against a page nobody actually parsed. This
exercises the exact chain a native crawl uses in production (see
``seohead.servers.handlers.crawl_site``): collect -> ``build_evidence`` ->
``AuditContext`` -> ``run_rules``. It mirrors the issue's own offline
reproducer, updated to assert the fixed behaviour instead of the bug.
"""

from __future__ import annotations

import pandas as pd
import pytest

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules

FOUR_CHECKS = {"TITLE_MISSING", "DESC_MISSING", "H1_MISSING", "CANONICAL_MISSING"}
UNMEASURED_NATIVE_FIELDS = {
    "META_KEYWORDS_PRESENT",
    "CANONICAL_MULTIPLE",
    "HTTP1_ONLY",
    "AMPHTML_PRESENT",
}


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.status_code = status_code
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.text = text
        self.content = text.encode("utf-8")


COMPLIANT_HTML = (
    "<html><head>"
    "<title>Actual title that easily meets the configured minimum</title>"
    '<meta name="description" content="A complete description that easily meets the threshold.">'
    '<link rel="canonical" href="https://example.com/oversized">'
    "</head><body><h1>Actual heading that easily meets the configured minimum</h1>"
    + ("body " * 100)
    + "</body></html>"
)

# Small enough to stay under every max_response_bytes used below, and genuinely
# carries none of the four -- the negative control.
BLANK_HTML = "<html><head></head><body>hi</body></html>"


def _run(urls_and_html: dict[str, str], max_response_bytes: int) -> AuditContext:
    result = collect_urls(
        list(urls_and_html),
        fetcher=lambda url: FakeResponse(urls_and_html[url]),
        max_response_bytes=max_response_bytes,
    )
    evidence = build_evidence(result)
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])
    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))
    run_rules(ctx)
    return ctx


def test_oversized_200_produces_no_false_findings_and_a_named_skip():
    url = "https://example.com/oversized"
    ctx = _run({url: COMPLIANT_HTML}, max_response_bytes=50)

    record = next(p for p in ctx.pages if p.url == url)
    assert record.status_code == 200  # oversized still stays visible/indexable (#1)
    assert record.metrics["_record"]["body_unavailable"] == "oversized"

    fired = {i.check for i in ctx.issues if i.check in FOUR_CHECKS}
    assert fired == set(), "an unparsed but compliant body must not read as missing metadata"

    skipped = {s.id: s.reason for s in ctx.skipped if s.id in FOUR_CHECKS}
    assert set(skipped) == FOUR_CHECKS
    for reason in skipped.values():
        assert reason and "1 page" in reason  # a real, named reason -- never a blank string


def test_ordinary_page_genuinely_missing_a_title_still_fires():
    url = "https://example.com/blank"
    ctx = _run({url: BLANK_HTML}, max_response_bytes=10_000)

    fired = {i.check for i in ctx.issues if i.target_url == url and i.check in FOUR_CHECKS}
    assert fired == FOUR_CHECKS
    assert not [s for s in ctx.skipped if s.id in FOUR_CHECKS]


def test_mixed_crawl_withholds_only_the_oversized_page():
    """A check that fires for real elsewhere in the same run is not "skipped"."""
    oversized_url = "https://example.com/oversized"
    blank_url = "https://example.com/blank"
    ctx = _run(
        {oversized_url: COMPLIANT_HTML, blank_url: BLANK_HTML},
        max_response_bytes=200,
    )

    oversized_findings = {
        i.check for i in ctx.issues if i.target_url == oversized_url and i.check in FOUR_CHECKS
    }
    assert oversized_findings == set(), "the oversized page must still get none of the four"

    blank_findings = {
        i.check for i in ctx.issues if i.target_url == blank_url and i.check in FOUR_CHECKS
    }
    assert blank_findings == FOUR_CHECKS, "a genuinely blank page elsewhere must still fire"

    # Each check fired for real (on the blank page), so none of the four is
    # reported as audit-wide "skipped" -- that would misrepresent a check that
    # plainly ran and found a genuine problem.
    assert not [s for s in ctx.skipped if s.id in FOUR_CHECKS]


def test_native_unprojected_fields_are_named_skips_even_with_a_mixed_body_population():
    """#659: no projected column is not evidence of a clean page.

    The parseable page deliberately carries each declaration, while the other
    page is oversized. Neither can make the native projection claim it checked
    fields it does not persist; the skip is run-wide and remains named.
    """
    normal_url = "https://example.com/normal"
    oversized_url = "https://example.com/oversized"
    normal_html = (
        "<html><head>"
        '<meta name="keywords" content="obsolete">'
        f'<link rel="canonical" href="{normal_url}">'
        '<link rel="canonical" href="https://example.com/other">'
        '<link rel="amphtml" href="https://example.com/amp">'
        "</head><body>content</body></html>"
    )
    limit = len(normal_html.encode("utf-8"))
    assert limit < len(COMPLIANT_HTML.encode("utf-8"))
    ctx = _run({normal_url: normal_html, oversized_url: COMPLIANT_HTML}, max_response_bytes=limit)
    records = {page.url: page.metrics["_record"] for page in ctx.pages}
    assert not records[normal_url]["body_unavailable"]
    assert records[oversized_url]["body_unavailable"] == "oversized"

    assert not [issue for issue in ctx.issues if issue.check in UNMEASURED_NATIVE_FIELDS]
    skipped = {item.id: item.reason for item in ctx.skipped if item.id in UNMEASURED_NATIVE_FIELDS}
    assert set(skipped) == UNMEASURED_NATIVE_FIELDS
    assert all("Internal:All" in reason for reason in skipped.values())


@pytest.mark.parametrize(
    "column,check_id,defect,clean",
    [
        ("Meta Keywords 1", "META_KEYWORDS_PRESENT", "obsolete", ""),
        ("Canonical Link Element 2", "CANONICAL_MULTIPLE", "https://example.com/other", ""),
        ("HTTP Version", "HTTP1_ONLY", "HTTP/1.1", "HTTP/2"),
        ("amphtml Link Element", "AMPHTML_PRESENT", "https://example.com/amp", ""),
    ],
)
def test_measured_export_columns_keep_both_verdicts(column, check_id, defect, clean):
    """A missing native column must not disable a measured export column."""
    exports = LoadedExports()
    exports.frames["internal_all"] = pd.DataFrame(
        [
            {
                "Address": f"https://example.com/{name}",
                "Content Type": "text/html",
                "Status Code": 200,
                "Indexability": "Indexable",
                column: value,
            }
            for name, value in (("defect", defect), ("clean", clean))
        ]
    )
    ctx = AuditContext(exports, load_config(None))
    run_rules(ctx)
    assert {issue.target_url for issue in ctx.issues if issue.check == check_id} == {
        "https://example.com/defect"
    }
    assert not [item for item in ctx.skipped if item.id == check_id]

    exports.frames["internal_all"] = exports.frames["internal_all"].drop(columns=[column])
    missing = AuditContext(exports, load_config(None))
    run_rules(missing)
    assert not [issue for issue in missing.issues if issue.check == check_id]
    assert [item for item in missing.skipped if item.id == check_id]
