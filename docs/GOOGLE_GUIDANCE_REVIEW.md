# Google Search Central guidance review

What this toolkit's shipped guidance was checked against, when, and what changed as a result.

**Source root:** <https://developers.google.com/search/docs?hl=en>
**Snapshot date:** 2026-09-09
**Machine-readable record:** [google-search-review.json](google-search-review.json) — 175 rows

> This records published Google Search Central documentation as read on 2026-09-09. It is not a
> Google certification, it confers no Google endorsement, and it implies no ranking or indexing
> guarantee. Documentation published or changed after the snapshot date is outside this record.

## Coverage

| Population | Count |
|---|---|
| Canonical English guides reachable from the documentation navigation and section indexes | 161 |
| Migrated crawler/robots documentation and Search Central help pages, read as a supplement | 14 |
| **Rows in the record, all read** | **175** |
| Rows still pending a read or an exclusion decision | 0 |

An indexed URL is not a read guide: every row carries the date it was read and the review
artifact it was read into. All 44 additional URL spellings found by link extraction were
resolved to their canonical guide; two return 404 and are excluded rather than counted.

## What the review excludes

- Translations of English guides.
- Query-string and fragment duplicates of a canonical guide URL.
- Global product and footer links to unrelated Google product documentation.
- An unbounded crawl of external Help Center links.
- Updates/changelog pages and external support destinations, which are not documentation guides.
- Two of the 44 resolved alias spellings, which return 404.

## How guidance is labelled

Google's statements and this toolkit's own heuristics are separate things, and the record keeps
them separate. Every applicable rule carries one label:

| Label | Meaning | Count |
|---|---|---|
| `requirement` | Google states the behaviour as required | 65 |
| `eligibility` | A condition for a feature to be eligible; eligibility is not a guarantee of display | 36 |
| `recommendation` | Recommended without being required | 88 |
| `heuristic` | An observation about crawler behaviour, not a rule | 4 |
| `explanation` | Descriptive, carrying no obligation | 188 |

The toolkit's configurable specialist heuristics are never presented as Google requirements.
`URL_CONTAINS_SPACE` is the worked example: it stays a warning-level hygiene heuristic, and
[scenarios/url-hygiene.md](scenarios/url-hygiene.md) says so beside the check.

## What the review changed

Each row maps its guidance to the shipped skill, document, configuration, check or script that
interprets it, and records either no change with a reason, or a mismatch and its repair. Of 175
rows, 168 were `no_change` and 7 carried a repair, resolving to three issues:

| Issue | Merged in | Repair |
|---|---|---|
| [#670](https://github.com/PavloSEO/seotools/issues/670) | [#680](https://github.com/PavloSEO/seotools/pull/680) | Mobile render identity: the mobile representation now carries the mobile user agent and viewport |
| [#671](https://github.com/PavloSEO/seotools/issues/671) | [#673](https://github.com/PavloSEO/seotools/pull/673) | Redirect guidance: a 302 alone is not a defect, and a status code alone does not measure ranking-signal loss |
| [#692](https://github.com/PavloSEO/seotools/issues/692) | [#695](https://github.com/PavloSEO/seotools/pull/695) | JavaScript rendering, canonical, `notranslate` and encoded-URL-space guidance qualified, without changing runtime severities |

A correction to prose alone does not repair a wrong executable verdict, and a code fix alone does
not repair a misleading skill: each repair above carries offline regression evidence in
`tests/test_google_guidance.py` or `tests/test_redirect_guidance.py`.

## Two repairs to the record itself

The record as first assembled dropped evidence its own source artifacts held. Both gaps are
repaired here and stated in the JSON under `backfills`:

- Five rows carried `actual_read: true` with no read date. Restored from the source review
  artifact, which recorded 2026-09-09.
- Nine rows carried no labelled rules. Four were copied verbatim from itemized
  requirement/recommendation/heuristic lists; five came from a narrative summary and were
  labelled sentence by sentence. Every backfilled entry names which of the two it is, in its
  `derived_from` field, so a reader can tell a copied rule from a labelled one.

## Keeping this record honest

`tests/test_google_review_record.py` fails if a row loses its read date or its labels, if a
recorded repair is left `pending`, if the coverage counters stop matching the rows, or if the
record starts claiming certification or guaranteed rankings.
