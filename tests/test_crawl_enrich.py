"""External URL joins are explicit, offline, and can feed list mode."""

import csv

import pytest

from seohead.servers import handlers


def _audit(*, partial=False):
    return {
        "run": {"crawl_partial": partial},
        "pages": [{"url": "https://example.test/seen"}],
        "issues": [],
    }


def test_external_csv_metrics_join_and_write_reliable_orphan_list(tmp_path):
    external = tmp_path / "traffic.csv"
    with external.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(
            [
                ["url", "impressions"],
                ["https://example.test/seen", "100"],
                ["https://example.test/orphan", "20"],
            ]
        )
    out_urls = tmp_path / "follow-up.txt"

    result = handlers.crawl_enrich(
        audit=_audit(), external_csv=str(external), out_urls=str(out_urls)
    )

    assert result["join"]["summary"]["joined"] == 1
    assert result["orphan_detection"] == {
        "state": "complete",
        "reason": "crawl completed; external-only same-origin URLs can enter list mode",
        "urls": ["https://example.test/orphan"],
    }
    assert out_urls.read_text(encoding="utf-8") == "https://example.test/orphan\n"


def test_partial_crawl_does_not_claim_external_only_urls_are_orphans(tmp_path):
    external = tmp_path / "traffic.csv"
    external.write_text("url\nhttps://example.test/orphan\n", encoding="utf-8")

    result = handlers.crawl_enrich(audit=_audit(partial=True), external_csv=str(external))

    assert result["orphan_detection"]["state"] == "partial"
    assert result["orphan_detection"]["urls"] == []


def test_writing_orphan_list_from_a_partial_crawl_is_refused(tmp_path):
    external = tmp_path / "traffic.csv"
    external.write_text("url\nhttps://example.test/orphan\n", encoding="utf-8")

    with pytest.raises(ValueError, match="partial"):
        handlers.crawl_enrich(
            audit=_audit(partial=True), external_csv=str(external), out_urls=str(tmp_path / "urls.txt")
        )
