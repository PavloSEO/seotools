"""NEAR_DUPLICATE / DUPLICATE_BY_HASH computed from stored page text.

Near-duplicate clustering is an all-pairs comparison, so it can only run once
every page has been fetched, over a stored corpus (issue #15, item 3). This
exercises the self-computed fallback that reads ``input.html_store_dir`` and
the configured content area, wired in ``check_content_duplication``.
"""

from __future__ import annotations

import csv
import os

import pytest

from seohead.sf.config import load_config
from seohead.sf.core.audit import run_audit

COLS = ["Address", "Content Type", "Status Code", "Status", "Indexability"]

# A long-enough shared paragraph that SimHash treats as clearly near-identical
# when only a couple of words differ, and clearly distinct from unrelated text.
_PARAGRAPH = (
    "Screaming Frog crawls a website and reports on technical SEO issues across "
    "every page it discovers, including broken links, duplicate content, missing "
    "metadata, and redirect chains that slow down search engine crawlers. "
) * 3

URL_A = "https://example.com/a.html"
URL_B = "https://example.com/b.html"
URL_C = "https://example.com/c.html"
URL_D = "https://example.com/d.html"


def _page_html(paragraph: str) -> str:
    return f"<html><body><main><p>{paragraph}</p></main></body></html>"


def _write(tmp_path, urls: list[str], html_files: dict[str, str]):
    """A minimal Internal:All plus a Store-HTML-style ``<host>/<path>`` tree."""
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    with open(exports_dir / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows([[url, "text/html", "200", "OK", "Indexable"] for url in urls])

    html_dir = tmp_path / "html_store"
    for url, html in html_files.items():
        rel = url.split("://", 1)[1]  # host/path, mirroring SF's Store HTML tree
        path = html_dir / rel
        os.makedirs(path.parent, exist_ok=True)
        path.write_text(html, encoding="utf-8")

    cfg = load_config(None)
    cfg["input"]["html_store_dir"] = str(html_dir)
    return str(exports_dir), cfg


def _fired(res, check):
    return {i.target_url: i for i in res.issues if i.check == check}


def test_near_identical_pages_cluster(tmp_path):
    html_files = {
        URL_A: _page_html(_PARAGRAPH),
        URL_B: _page_html(_PARAGRAPH.replace("website", "site", 1)),
        URL_C: _page_html("A completely unrelated product page about shoes."),
    }
    exports_dir, cfg = _write(tmp_path, [URL_A, URL_B, URL_C], html_files)
    res = run_audit(
        input_mode="parse-exports", exports_dir=exports_dir, config=cfg, log=lambda m: None
    )
    fired = _fired(res, "NEAR_DUPLICATE")
    assert set(fired) == {URL_A, URL_B}
    assert fired[URL_A].group_id == fired[URL_B].group_id
    assert "NEAR_DUPLICATE" not in {s.id for s in res.skipped}


def test_exact_duplicate_pages_report_under_duplicate_by_hash_not_near(tmp_path):
    html_files = {URL_A: _page_html(_PARAGRAPH), URL_B: _page_html(_PARAGRAPH)}
    exports_dir, cfg = _write(tmp_path, [URL_A, URL_B], html_files)
    res = run_audit(
        input_mode="parse-exports", exports_dir=exports_dir, config=cfg, log=lambda m: None
    )
    exact = _fired(res, "DUPLICATE_BY_HASH")
    assert set(exact) == {URL_A, URL_B}
    # find_duplicates excludes a cluster fully explained by exact duplication
    # from the near-duplicate output, so it must not also fire NEAR_DUPLICATE.
    assert _fired(res, "NEAR_DUPLICATE") == {}


@pytest.mark.parametrize(
    ("disabled", "expected"),
    [
        ({}, {"NEAR_DUPLICATE", "DUPLICATE_BY_HASH"}),
        ({"NEAR_DUPLICATE": {"enabled": False}}, {"DUPLICATE_BY_HASH"}),
        ({"DUPLICATE_BY_HASH": {"enabled": False}}, {"NEAR_DUPLICATE"}),
        ({"NEAR_DUPLICATE": {"enabled": False}, "DUPLICATE_BY_HASH": {"enabled": False}}, set()),
    ],
)
def test_disabled_duplicate_checks_do_not_crash_or_leave_group_evidence(
    tmp_path, disabled, expected
):
    """#565: disabled stored-HTML duplicate checks must remain inert after clustering."""
    html_files = {
        URL_A: _page_html(_PARAGRAPH),
        URL_B: _page_html(_PARAGRAPH.replace("website", "site", 1)),
        URL_C: _page_html("An exactly duplicated product page with its own distinctive content."),
        URL_D: _page_html("An exactly duplicated product page with its own distinctive content."),
    }
    exports_dir, cfg = _write(tmp_path, list(html_files), html_files)
    cfg["checks"] = disabled

    res = run_audit(
        input_mode="parse-exports", exports_dir=exports_dir, config=cfg, log=lambda m: None
    )

    fired = {check: _fired(res, check) for check in ("NEAR_DUPLICATE", "DUPLICATE_BY_HASH")}
    groups = {group.check: group for group in res.groups if group.check in fired}
    assert {check for check, issues in fired.items() if issues} == expected
    assert set(groups) == expected
    assert {check.id for check in res.disabled} == set(disabled)

    if "NEAR_DUPLICATE" in expected:
        near = fired["NEAR_DUPLICATE"]
        assert set(near) == {URL_A, URL_B}
        assert {issue.group_id for issue in near.values()} == {groups["NEAR_DUPLICATE"].group_id}
        assert all("cluster_min_similarity" in issue.details for issue in near.values())
    if "DUPLICATE_BY_HASH" in expected:
        exact = fired["DUPLICATE_BY_HASH"]
        assert set(exact) == {URL_C, URL_D}
        assert {issue.group_id for issue in exact.values()} == {
            groups["DUPLICATE_BY_HASH"].group_id
        }
        assert {issue.details["duplicate_count"] for issue in exact.values()} == {2}


def test_skips_without_a_stored_html_directory(tmp_path):
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    with open(exports_dir / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerow([URL_A, "text/html", "200", "OK", "Indexable"])
    res = run_audit(input_mode="parse-exports", exports_dir=str(exports_dir), log=lambda m: None)
    reasons = {s.id: s.reason for s in res.skipped}
    assert "html_store_dir" in reasons["NEAR_DUPLICATE"]
    # DUPLICATE_BY_HASH is already declared skipped earlier, by rules.py's own
    # check_duplicates (no Hash column) — both reasons are honest, and the
    # first one recorded wins, so only its own check id needs to be present.
    assert "DUPLICATE_BY_HASH" in reasons


def test_low_html_store_coverage_skips_instead_of_reading_clean(tmp_path):
    """#455: 1 of 10 pages stored must not read as "0 duplicates found"."""
    urls = [f"https://example.com/p{i}.html" for i in range(10)]
    html_files = {urls[0]: _page_html("A completely unrelated product page about shoes.")}
    exports_dir, cfg = _write(tmp_path, urls, html_files)
    res = run_audit(
        input_mode="parse-exports", exports_dir=exports_dir, config=cfg, log=lambda m: None
    )
    reasons = {s.id: s.reason for s in res.skipped}
    assert _fired(res, "NEAR_DUPLICATE") == {}
    assert _fired(res, "DUPLICATE_BY_HASH") == {}
    assert "1 of 10" in reasons["NEAR_DUPLICATE"]
    # DUPLICATE_BY_HASH is already skipped earlier by rules.py's own
    # check_duplicates (no Hash column in this fixture) -- the first
    # recorded skip wins, so only NEAR_DUPLICATE's own reason is asserted here.


def test_high_html_store_coverage_stays_silent(tmp_path):
    """Negative control: near-full coverage must not start emitting a spurious skip."""
    urls = [URL_A, URL_B, URL_C]
    html_files = {
        URL_A: _page_html(_PARAGRAPH),
        URL_B: _page_html(_PARAGRAPH.replace("website", "site", 1)),
        URL_C: _page_html("A completely unrelated product page about shoes."),
    }
    exports_dir, cfg = _write(tmp_path, urls, html_files)
    res = run_audit(
        input_mode="parse-exports", exports_dir=exports_dir, config=cfg, log=lambda m: None
    )
    # Full coverage must not introduce a spurious skip for the check this
    # issue is about. DUPLICATE_BY_HASH is skipped regardless, for the
    # unrelated, pre-existing reason that this fixture has no Hash column.
    assert "NEAR_DUPLICATE" not in {s.id for s in res.skipped}


def test_defers_to_native_columns_when_present(tmp_path):
    cols = [*COLS, "No. Near Duplicates", "Hash"]
    exports_dir = tmp_path / "exports"
    exports_dir.mkdir()
    with open(exports_dir / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerow([URL_A, "text/html", "200", "OK", "Indexable", "0", "abc123"])
    html_dir = tmp_path / "html_store"
    os.makedirs(html_dir / "example.com", exist_ok=True)
    (html_dir / "example.com" / "a.html").write_text(_page_html(_PARAGRAPH), encoding="utf-8")
    cfg = load_config(None)
    cfg["input"]["html_store_dir"] = str(html_dir)
    res = run_audit(
        input_mode="parse-exports", exports_dir=str(exports_dir), config=cfg, log=lambda m: None
    )
    reasons = {s.id: s.reason for s in res.skipped}
    assert "native" in reasons["NEAR_DUPLICATE"]
    assert "native" in reasons["DUPLICATE_BY_HASH"]
