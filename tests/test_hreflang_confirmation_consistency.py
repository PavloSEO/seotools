"""HREFLANG_INCONSISTENT_CONFIRMATION (#386): A declares B as "fr"; B says it is "de".

HREFLANG_MISSING_RETURN_LINK asks only whether a return link exists. A pair can be
fully reciprocal and still be discarded by Google, because the contract is that both
sides name the *same* language and region code -- a page's counterparts must call it
what it calls itself. The coverage row's own note said as much: "a return link is
checked for existence, not for declaring the same locale back".

The counterpart's self-referencing hreflang is the authority for what it is, so a
counterpart that declares nothing about itself is passed over rather than guessed at,
and the ordinary correct layout -- every page self-referencing, x-default alongside a
real code -- must stay silent.
"""

from __future__ import annotations

import csv

from seohead.sf.core.audit import run_audit

INTERNAL_COLS = ["Address", "Content Type", "Status Code", "Status", "Indexability"]
HREFLANG_COLS = ["Source", "Destination", "Hreflang"]

EN = "https://example.com/en"
FR = "https://example.com/fr"
DE = "https://example.com/de"

CHECK = "HREFLANG_INCONSISTENT_CONFIRMATION"


def _write(tmp_path, urls, hreflang_rows, *, with_hreflang_export=True):
    directory = tmp_path / "exports"
    directory.mkdir()
    with open(directory / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(INTERNAL_COLS)
        writer.writerows([url, "text/html", "200", "OK", "Indexable"] for url in urls)
    if with_hreflang_export:
        with open(directory / "all_hreflang.csv", "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(HREFLANG_COLS)
            writer.writerows(hreflang_rows)
    return str(directory)


def _audit(tmp_path, urls, hreflang_rows, **kw):
    return run_audit(
        input_mode="parse-exports",
        exports_dir=_write(tmp_path, urls, hreflang_rows, **kw),
        log=lambda _m: None,
    )


def _fired(res):
    return {issue.target_url: issue for issue in res.issues if issue.check == CHECK}


def test_a_counterpart_confirming_a_different_code_is_reported_against_the_declaring_page(
    tmp_path,
):
    """/en calls /fr "fr"; /fr calls itself "de". The pair is reciprocal and still
    invalid, and the page holding the annotation that disagrees is the one to fix."""
    rows = [
        [EN, EN, "en"],
        [EN, FR, "fr"],
        [FR, FR, "de"],
        [FR, EN, "en"],
    ]
    fired = _fired(_audit(tmp_path, [EN, FR], rows))
    assert set(fired) == {EN}
    inconsistent = fired[EN].details["inconsistent"]
    assert inconsistent == [{"counterpart": FR, "declared_here": "fr", "confirmed_there": ["de"]}]


def test_a_consistent_set_with_x_default_stays_silent(tmp_path):
    """The most common correct hreflang layout there is: every page self-references,
    and one of them additionally carries x-default. x-default is a fallback for
    unmatched users, not a locale claim, so reading it as a contradiction of "en"
    would fire on a site that did everything right."""
    rows = [
        [EN, EN, "en"],
        [EN, EN, "x-default"],
        [EN, FR, "fr"],
        [FR, FR, "fr"],
        [FR, EN, "en"],
        [FR, EN, "x-default"],
    ]
    assert _fired(_audit(tmp_path, [EN, FR], rows)) == {}


def test_case_differences_alone_are_not_an_inconsistency(tmp_path):
    """Language tags are case-insensitive: "en-GB" and "en-gb" are one tag."""
    rows = [
        [EN, EN, "en-GB"],
        [EN, FR, "fr-FR"],
        [FR, FR, "fr-fr"],
        [FR, EN, "en-gb"],
    ]
    assert _fired(_audit(tmp_path, [EN, FR], rows)) == {}


def test_a_region_variant_is_reported_because_it_is_genuinely_a_different_annotation(
    tmp_path,
):
    """ "en" and "en-GB" are not the same tag. Folding them together to be lenient
    would hide exactly the mismatch this check exists to find."""
    rows = [
        [EN, EN, "en-GB"],
        [EN, FR, "fr"],
        [FR, FR, "fr-CA"],
        [FR, EN, "en-GB"],
    ]
    fired = _fired(_audit(tmp_path, [EN, FR], rows))
    assert set(fired) == {EN}
    assert fired[EN].details["inconsistent"][0]["confirmed_there"] == ["fr-ca"]


def test_a_counterpart_with_no_self_reference_is_not_faulted(tmp_path):
    """That page makes no statement about what it is, so there is nothing to
    disagree with -- HREFLANG_MISSING_SELF_REFERENCE already names it."""
    rows = [
        [EN, EN, "en"],
        [EN, FR, "fr"],
        [FR, EN, "en"],
    ]
    res = _audit(tmp_path, [EN, FR], rows)
    assert _fired(res) == {}
    assert FR in {i.target_url for i in res.issues if i.check == "HREFLANG_MISSING_SELF_REFERENCE"}


def test_a_target_outside_the_crawl_is_left_alone(tmp_path):
    """Nobody fetched it, so it never declared anything about itself; a page that was
    never measured must not read as either confirming or contradicting."""
    rows = [
        [EN, EN, "en"],
        [EN, DE, "de"],
    ]
    assert _fired(_audit(tmp_path, [EN], rows)) == {}


def test_a_self_confirmation_outside_the_crawl_is_left_alone(tmp_path):
    """An exported counterpart row is not evidence that the counterpart was crawled."""
    rows = [
        [EN, EN, "en"],
        [EN, DE, "de"],
        [DE, DE, "fr"],
    ]
    assert _fired(_audit(tmp_path, [EN], rows)) == {}


def test_every_disagreeing_pair_on_one_page_is_reported_once_with_its_counterparts(
    tmp_path,
):
    rows = [
        [EN, EN, "en"],
        [EN, FR, "fr"],
        [EN, DE, "de"],
        [FR, FR, "fr-CA"],
        [DE, DE, "de-AT"],
        [FR, EN, "en"],
        [DE, EN, "en"],
    ]
    fired = _fired(_audit(tmp_path, [EN, FR, DE], rows))
    assert set(fired) == {EN}
    assert fired[EN].occurrences_count == 2
    assert [entry["counterpart"] for entry in fired[EN].details["inconsistent"]] == [FR, DE]


def test_the_check_skips_by_name_without_the_hreflang_export(tmp_path):
    res = _audit(tmp_path, [EN, FR], [], with_hreflang_export=False)
    reasons = {s.id: s.reason for s in res.skipped}
    assert "all_hreflang" in reasons[CHECK]
    assert CHECK not in {i.check for i in res.issues}
