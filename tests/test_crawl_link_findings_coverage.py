"""Native link/form checks declare whether their retained evidence was evaluated (#577)."""

from __future__ import annotations

from unittest.mock import patch

from seohead.crawl import link_findings
from seohead.crawl.collect import CrawlResult, PageRecord
from seohead.crawl.settings import load
from seohead.crawl.spider import FormEdge, LinkEdge, SpiderResult
from seohead.servers import handlers

_LINK_FORM_CHECKS = {
    "OUTLINK_TO_LOCALHOST",
    "FOLLOW_AND_NOFOLLOW_INLINKS",
    "FORM_URL_INSECURE",
    "FORM_ON_HTTP_URL",
}


def _page() -> PageRecord:
    return PageRecord(
        url="https://example.test/",
        status_code=200,
        content_type="text/html",
        title="A stored page",
        word_count=3,
    )


def _audit(result):
    return handlers._audit_crawl_result(
        result,
        settings=load(overrides={"speed.min_delay_seconds": 0}),
        url=None,
        sitemap_seed={"sitemap_url": None, "sitemap_urls": [], "declared": []},
        discovery={"mode": "list"},
        offline=True,
    )[1]


def _skips(audit) -> dict[str, str]:
    return {entry["id"]: entry["reason"] for entry in audit["run"]["checks_skipped"]}


def test_url_less_spider_result_runs_pure_link_and_form_predicates():
    """Saved/offline evidence can answer these predicates without requesting a URL again."""
    result = SpiderResult(
        pages=[_page()],
        links=[
            LinkEdge(
                source="https://example.test/",
                destination="https://example.test/target",
                anchor="Target",
                nofollow=False,
            )
        ],
        forms=[
            FormEdge(
                page="https://example.test/",
                method="post",
                action="https://example.test/submit",
                has_password=False,
            )
        ],
    )
    with (
        patch.object(
            link_findings,
            "outlinks_to_localhost",
            wraps=link_findings.outlinks_to_localhost,
        ) as localhost,
        patch.object(
            link_findings,
            "form_url_insecure",
            wraps=link_findings.form_url_insecure,
        ) as insecure_form,
        patch.object(
            link_findings,
            "forms_on_http_pages_with_password",
            wraps=link_findings.forms_on_http_pages_with_password,
        ) as password_form,
    ):
        audit = _audit(result)

    assert localhost.called
    assert insecure_form.called
    assert password_form.called
    fired = {issue["check"] for issue in audit["issues"]}
    skipped = _skips(audit)
    for check_id in _LINK_FORM_CHECKS - {"FOLLOW_AND_NOFOLLOW_INLINKS"}:
        assert check_id not in fired
        assert check_id not in skipped  # clean only because its predicate ran above
    assert "FOLLOW_AND_NOFOLLOW_INLINKS" in skipped
    assert "no crawl start URL" in skipped["FOLLOW_AND_NOFOLLOW_INLINKS"]


def test_url_list_without_retained_edges_or_forms_skips_by_name():
    """List mode cannot turn absent graph evidence into a clean security verdict."""
    audit = _audit(CrawlResult(pages=[_page()]))

    skipped = _skips(audit)
    assert set(skipped) >= _LINK_FORM_CHECKS
    assert "no link-edge evidence" in skipped["OUTLINK_TO_LOCALHOST"]
    assert "no link-edge evidence" in skipped["FOLLOW_AND_NOFOLLOW_INLINKS"]
    assert "no form evidence" in skipped["FORM_URL_INSECURE"]
    assert "no form evidence" in skipped["FORM_ON_HTTP_URL"]
