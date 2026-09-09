"""Closes the cheapest gaps found by tests/test_check_verdict_coverage.py's live scan.

Each check here previously had a test that only measured the traversal (registration,
skip-when-absent) or a test that never existed at all -- not one that proves the check
both fires on markup with the defect and stays silent on markup without it (issue #98).
These are the checks that needed no new machinery, only a row of Internal:All (or a
native-filter export) shaped to carry -- or not carry -- the specific defect.

This file does not attempt every uncovered check; see NEW: baseline in
test_check_verdict_coverage.py for the count that remains and why writing shallow tests
for the rest would not be evidence.
"""

from __future__ import annotations

import csv

from seohead.sf.core.audit import run_audit

COLS = [
    "Address",
    "Content Type",
    "Status Code",
    "Status",
    "Indexability",
    "Title 1",
    "Meta Description 1",
    "Meta Keywords 1",
    "H1-1",
    "H2-1",
    "Canonical Link Element 1",
    "Canonical Link Element 2",
    "amphtml Link Element",
    "Meta Robots 1",
    "HTTP Version",
    "Outlinks",
    "External Outlinks",
    "Word Count",
]

LONG_TITLE = "T" * 300
LONG_DESC = "D" * 400
OK_TITLE = "A descriptive page title with sufficient length"
OK_DESC = "A meta description deliberately longer than seventy characters to clear the minimum."


def _row(
    url,
    *,
    status="200",
    code="200",
    indexability="Indexable",
    title=OK_TITLE,
    desc=OK_DESC,
    keywords="",
    h1="Heading",
    h2="Subheading",
    canonical=None,
    canonical_2="",
    amphtml="",
    robots="index,follow",
    http_version="2",
):
    return [
        url,
        "text/html",
        code,
        status,
        indexability,
        title,
        desc,
        keywords,
        h1,
        h2,
        canonical if canonical is not None else url,
        canonical_2,
        amphtml,
        robots,
        http_version,
        "10",
        "5",
        "500",
    ]


def _run(tmp_path, rows, extra_files=None, config_overrides=None):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(COLS)
        w.writerows(rows)
    for name, (header, file_rows) in (extra_files or {}).items():
        with open(d / name, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(file_rows)
    return run_audit(
        input_mode="parse-exports",
        exports_dir=str(d),
        config_overrides=config_overrides,
        log=lambda m: None,
    )


def _fired(res):
    out = {}
    for i in res.issues:
        out.setdefault(i.check, set()).add(i.target_url)
    return out


BAD = "https://example.com/bad"
OK = "https://example.com/ok"


def test_title_length_and_equals_h1_fire_and_stay_silent(tmp_path):
    rows = [
        _row(BAD, title=LONG_TITLE, h1=LONG_TITLE),
        _row(OK),
    ]
    f = _fired(_run(tmp_path, rows))
    assert BAD in f.get("TITLE_TOO_LONG", set())
    assert OK not in f.get("TITLE_TOO_LONG", set())
    assert BAD in f.get("TITLE_EQUALS_H1", set())
    assert OK not in f.get("TITLE_EQUALS_H1", set())


def test_title_too_short_fires_and_stays_silent(tmp_path):
    rows = [_row(BAD, title="Hi"), _row(OK)]
    f = _fired(_run(tmp_path, rows))
    assert BAD in f.get("TITLE_TOO_SHORT", set())
    assert OK not in f.get("TITLE_TOO_SHORT", set())


def test_description_length_and_duplicate_fire_and_stay_silent(tmp_path):
    dup_url_1 = "https://example.com/dup1"
    dup_url_2 = "https://example.com/dup2"
    rows = [
        _row(BAD, desc=LONG_DESC),
        _row("https://example.com/short", desc="short"),
        _row(dup_url_1, desc="Shared description text that is long enough to clear minimums."),
        _row(dup_url_2, desc="Shared description text that is long enough to clear minimums."),
        _row(OK),
    ]
    f = _fired(_run(tmp_path, rows))
    assert BAD in f.get("DESC_TOO_LONG", set())
    assert OK not in f.get("DESC_TOO_LONG", set())
    assert "https://example.com/short" in f.get("DESC_TOO_SHORT", set())
    assert OK not in f.get("DESC_TOO_SHORT", set())
    assert {dup_url_1, dup_url_2} <= f.get("DESC_DUPLICATE", set())
    assert OK not in f.get("DESC_DUPLICATE", set())


def test_h1_duplicate_and_h2_missing_fire_and_stay_silent(tmp_path):
    dup_url_1 = "https://example.com/h1dup1"
    dup_url_2 = "https://example.com/h1dup2"
    rows = [
        _row(dup_url_1, h1="Shared H1"),
        _row(dup_url_2, h1="Shared H1"),
        _row(BAD, h1="Unique BAD heading", h2=""),
        _row(OK, h1="Unique OK heading"),
    ]
    f = _fired(_run(tmp_path, rows, config_overrides={"requirements": {"require_h2": True}}))
    assert {dup_url_1, dup_url_2} <= f.get("H1_DUPLICATE", set())
    assert OK not in f.get("H1_DUPLICATE", set())
    assert BAD in f.get("H2_MISSING", set())
    assert OK not in f.get("H2_MISSING", set())


def _skipped(res):
    return {s.id: s.reason for s in res.skipped}


def test_a_requirement_that_is_off_names_its_check_unevaluated(tmp_path):
    """An unreachable check is not a clean one (#635).

    ``require_h2`` is false by default, so the H2_MISSING branch never runs. Before
    this it left no trace at all, and coverage counted the check as silent -- the
    bucket that means "ran over every page and found nothing" -- on a page that has
    no H2 to find.
    """
    rows = [_row(BAD, h2=""), _row(OK)]
    res = _run(tmp_path, rows)

    assert BAD not in _fired(res).get("H2_MISSING", set())
    reason = _skipped(res).get("H2_MISSING")
    assert reason is not None, "H2_MISSING must be declared skipped, not left silent"
    assert "require_h2" in reason


def test_a_requirement_that_is_on_still_judges_its_check(tmp_path):
    """The silent half: turning the requirement on must not leave the check skipped."""
    rows = [_row(BAD, h2=""), _row(OK)]
    res = _run(tmp_path, rows, config_overrides={"requirements": {"require_h2": True}})

    assert BAD in _fired(res).get("H2_MISSING", set())
    assert OK not in _fired(res).get("H2_MISSING", set())
    assert "H2_MISSING" not in _skipped(res)


def test_canonical_requirement_turned_off_names_its_check_unevaluated(tmp_path):
    """``require_canonical`` defaults to true, so this branch is rare -- and untested
    until now, which is how the same shape survived in two places."""
    rows = [_row(BAD, canonical=""), _row(OK)]
    res = _run(tmp_path, rows, config_overrides={"requirements": {"require_canonical": False}})

    assert BAD not in _fired(res).get("CANONICAL_MISSING", set())
    reason = _skipped(res).get("CANONICAL_MISSING")
    assert reason is not None
    assert "require_canonical" in reason


def test_canonical_requirement_left_on_still_judges_its_check(tmp_path):
    rows = [_row(BAD, canonical=""), _row(OK)]
    res = _run(tmp_path, rows)

    assert BAD in _fired(res).get("CANONICAL_MISSING", set())
    assert "CANONICAL_MISSING" not in _skipped(res)


def test_url_hygiene_checks_fire_and_stay_silent(tmp_path):
    space_url = "https://example.com/has space"
    encoded_query_url = "https://example.com/search?q=red%20shoes"
    slashes_url = "https://example.com/a//b"
    repetitive_url = "https://example.com/shop/shop"
    http_url = "http://example.com/insecure"
    long_url = "https://example.com/" + "x" * 500
    params_url = "https://example.com/page?utm=1"
    rows = [
        _row(space_url),
        _row(encoded_query_url),
        _row(slashes_url),
        _row(repetitive_url),
        _row(http_url),
        _row(long_url),
        _row(params_url, canonical=""),
        _row(OK),
    ]
    f = _fired(_run(tmp_path, rows))
    assert space_url in f.get("URL_CONTAINS_SPACE", set())
    assert encoded_query_url in f.get("URL_CONTAINS_SPACE", set())
    assert OK not in f.get("URL_CONTAINS_SPACE", set())
    assert slashes_url in f.get("URL_MULTIPLE_SLASHES", set())
    assert OK not in f.get("URL_MULTIPLE_SLASHES", set())
    assert repetitive_url in f.get("URL_REPETITIVE_PATH", set())
    assert OK not in f.get("URL_REPETITIVE_PATH", set())
    assert http_url in f.get("HTTP_URL", set())
    assert OK not in f.get("HTTP_URL", set())
    assert long_url in f.get("URL_TOO_LONG", set())
    assert OK not in f.get("URL_TOO_LONG", set())
    assert params_url in f.get("URL_HAS_PARAMS", set())
    assert OK not in f.get("URL_HAS_PARAMS", set())


def test_meta_directives_and_markup_checks_fire_and_stay_silent(tmp_path):
    rows = [
        _row(BAD, robots="index,follow,nofollow,nosnippet,noimageindex", keywords="seo, keywords"),
        _row("https://example.com/relcanon", canonical="/other-page"),
        _row("https://example.com/amp", amphtml="https://example.com/amp/page"),
        _row(OK),
    ]
    f = _fired(_run(tmp_path, rows))
    assert BAD in f.get("NOFOLLOW_PAGE", set())
    assert OK not in f.get("NOFOLLOW_PAGE", set())
    assert BAD in f.get("NOSNIPPET", set())
    assert OK not in f.get("NOSNIPPET", set())
    assert BAD in f.get("NOIMAGEINDEX", set())
    assert OK not in f.get("NOIMAGEINDEX", set())
    assert BAD in f.get("META_KEYWORDS_PRESENT", set())
    assert OK not in f.get("META_KEYWORDS_PRESENT", set())
    assert "https://example.com/relcanon" in f.get("CANONICAL_RELATIVE", set())
    assert OK not in f.get("CANONICAL_RELATIVE", set())
    assert "https://example.com/amp" in f.get("AMPHTML_PRESENT", set())
    assert OK not in f.get("AMPHTML_PRESENT", set())


def test_response_code_checks_fire_and_stay_silent(tmp_path):
    server_error_url = "https://example.com/500"
    no_response_url = "https://example.com/no-response"
    rows = [
        _row(server_error_url, code="500", status="Server Error", indexability="Non-Indexable"),
        _row(no_response_url, code="0", status="No Response", indexability="Non-Indexable"),
        _row(OK),
    ]
    f = _fired(_run(tmp_path, rows))
    assert server_error_url in f.get("SERVER_ERROR_5XX", set())
    assert OK not in f.get("SERVER_ERROR_5XX", set())
    assert no_response_url in f.get("NO_RESPONSE", set())
    assert OK not in f.get("NO_RESPONSE", set())


def test_native_filter_export_checks_fire_only_on_listed_urls(tmp_path):
    """MISSING_HSTS and STRUCTURED_DATA_MISSING are export-driven (#check_native_exports):
    every Address in the matching export gets the finding, every other page stays clean."""
    rows = [_row(BAD), _row(OK)]
    extra_files = {
        "security_missing_hsts_header.csv": (["Address"], [[BAD]]),
        "structured_data_missing.csv": (["Address"], [[BAD]]),
    }
    f = _fired(_run(tmp_path, rows, extra_files))
    assert BAD in f.get("MISSING_HSTS", set())
    assert OK not in f.get("MISSING_HSTS", set())
    assert BAD in f.get("STRUCTURED_DATA_MISSING", set())
    assert OK not in f.get("STRUCTURED_DATA_MISSING", set())
