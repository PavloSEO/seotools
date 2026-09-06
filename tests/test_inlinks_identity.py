from __future__ import annotations

import csv

import pytest

from seohead.sf.core.audit import run_audit
from seohead.sf.tasks import build_tasks


def _write_internal(path, rows):
    with open(path / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["Address", "Content Type", "Status Code", "Status", "Indexability", "Redirect URL"]
        )
        writer.writerows(rows)


@pytest.mark.parametrize(
    "destinations",
    [
        ["https://Example.test/old", "https://example.test/old"],
        ["https://example.test/old", "https://Example.test/old"],
    ],
)
def test_redirect_group_uses_crawled_identity_independent_of_inlink_row_order(
    tmp_path, destinations
):
    _write_internal(
        tmp_path,
        [
            [
                "https://example.test/old",
                "text/html",
                "301",
                "Moved",
                "Non-Indexable",
                "https://example.test/new",
            ]
        ],
    )
    with open(
        tmp_path / "redirection_(3xx)_inlinks.csv", "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Source",
                "Destination",
                "Anchor Text",
                "Status Code",
                "Follow",
                "Link Position",
                "Link Path",
            ]
        )
        for source, destination in zip(
            ["https://example.test/a", "https://example.test/b"], destinations, strict=True
        ):
            writer.writerow(
                [source, destination, source.rsplit("/", 1)[-1], "301", "true", "Content", "/a"]
            )
    result = run_audit(
        input_mode="parse-exports", exports_dir=str(tmp_path), log=lambda _message: None
    )
    issue = next(item for item in result.issues if item.check == "INTERNAL_LINK_TO_REDIRECT")
    assert issue.target_url == "https://example.test/old"
    assert issue.details["final_url"] == "https://example.test/new"
    assert issue.evidence["raw_destinations"] == destinations
    task = next(
        task
        for task in build_tasks(result.to_json())["tasks"]
        if task["check"] == "INTERNAL_LINK_TO_REDIRECT"
    )
    assert task["urls"] == ["https://example.test/old"]
    assert {link["target_url"] for link in task["broken_links"]} == {"https://example.test/old"}


def test_hrefless_invalid_hreflang_is_checked_without_target_findings(tmp_path):
    _write_internal(
        tmp_path,
        [["https://example.test/en", "text/html", "200", "OK", "Indexable", ""]],
    )
    with open(tmp_path / "all_hreflang.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Source", "Destination", "Hreflang"])
        writer.writerow(["https://example.test/en", "", "xx-badcode"])
    result = run_audit(
        input_mode="parse-exports", exports_dir=str(tmp_path), log=lambda _message: None
    )
    assert any(item.check == "HREFLANG_INVALID_CODE" for item in result.issues)
    assert not any(
        item.check in {"HREFLANG_BROKEN_TARGET", "HREFLANG_MISSING_RETURN_LINK"}
        for item in result.issues
    )


def test_hrefless_duplicate_language_is_checked_without_target_findings(tmp_path):
    _write_internal(
        tmp_path,
        [["https://example.test/en", "text/html", "200", "OK", "Indexable", ""]],
    )
    with open(tmp_path / "all_hreflang.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Source", "Destination", "Hreflang"])
        writer.writerow(["https://example.test/en", "", "en"])
        writer.writerow(["https://example.test/en", "", "EN"])
    result = run_audit(
        input_mode="parse-exports", exports_dir=str(tmp_path), log=lambda _message: None
    )
    assert any(item.check == "HREFLANG_MULTIPLE_ENTRIES" for item in result.issues)
    assert not any(item.check == "HREFLANG_NOT_CANONICAL" for item in result.issues)
