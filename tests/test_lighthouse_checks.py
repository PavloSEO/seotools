"""Static Lighthouse audits (issue #59): MISSING_CHARSET, MISSING_DOCTYPE,
VIEWPORT_MISSING, NO_COMPRESSION.

None of these four fields (Content-Encoding, Doctype, Viewport, Meta Charset)
is a default Screaming Frog export column, so the honest-skip case is the
default fixture in ``tests/fixtures`` (see ``result`` in conftest.py, used
below) rather than a specially stripped-down export: it already lacks all
four, exactly like a real SF export would without matching Custom Extraction.
"""

from __future__ import annotations

import csv

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.audit import run_audit
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules

COLS = [
    "Address",
    "Content Type",
    "Status Code",
    "Status",
    "Indexability",
    "Title 1",
    "Meta Description 1",
    "H1-1",
    "Canonical Link Element 1",
    "Meta Robots 1",
    "Content-Encoding",
    "Doctype",
    "Viewport",
    "Meta Charset",
    "Size (bytes)",
]
TITLE = "A descriptive page title with sufficient length"
DESC = "A meta description deliberately longer than seventy characters to clear the validation threshold."


def _row(
    url: str,
    *,
    content_type: str = "text/html",
    encoding: str = "",
    doctype: str = "",
    viewport: str = "",
    meta_charset: str = "",
    size_bytes: int = 5000,
) -> list[str]:
    return [
        url,
        content_type,
        "200",
        "OK",
        "Indexable",
        TITLE,
        DESC,
        "H",
        url,
        "index,follow",
        encoding,
        doctype,
        viewport,
        meta_charset,
        str(size_bytes),
    ]


def _run(tmp_path, rows):
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLS)
        w.writerows(rows)
    return run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)


def _fired(res) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for issue in res.issues:
        out.setdefault(issue.check, set()).add(issue.target_url)
    return out


def _skipped(res) -> set[str]:
    return {s.id for s in res.skipped}


# -- honest skip: none of the four columns is present anywhere in the run ---


def test_all_four_checks_skip_without_any_evidence(result):
    """``result`` (conftest.py) is a real-shaped SF export with none of these
    four columns — exactly what a default Screaming Frog export looks like."""
    skipped = _skipped(result)
    for check_id in ("MISSING_CHARSET", "MISSING_DOCTYPE", "VIEWPORT_MISSING", "NO_COMPRESSION"):
        assert check_id in skipped
    fired = _fired(result)
    for check_id in ("MISSING_CHARSET", "MISSING_DOCTYPE", "VIEWPORT_MISSING", "NO_COMPRESSION"):
        assert check_id not in fired


def test_all_four_checks_fire_when_every_page_is_negative(tmp_path):
    """#268: the columns are present (a native crawl always projects them) but every
    page genuinely lacks all four declarations. That must read as four sitewide
    findings, not as "no evidence in the run" — gating on a passing value elsewhere
    in the corpus made one good page the only thing standing between this and an
    incorrect all-skip."""
    rows = [_row("https://example.com/bad")]
    res = _run(tmp_path, rows)
    fired = _fired(res)
    for check_id in ("MISSING_CHARSET", "MISSING_DOCTYPE", "VIEWPORT_MISSING", "NO_COMPRESSION"):
        assert "https://example.com/bad" in fired[check_id]
    assert not _skipped(res) & {
        "MISSING_CHARSET",
        "MISSING_DOCTYPE",
        "VIEWPORT_MISSING",
        "NO_COMPRESSION",
    }


# -- MISSING_CHARSET ---------------------------------------------------------


def test_charset_fires_when_neither_source_declares_it(tmp_path):
    rows = [
        # establishes evidence: this page's Content-Type carries a charset
        _row("https://example.com/ok", content_type="text/html; charset=UTF-8"),
        # neither the header nor Meta Charset declares one -> fires
        _row("https://example.com/bad", content_type="text/html"),
    ]
    fired = _fired(_run(tmp_path, rows))
    assert "https://example.com/bad" in fired["MISSING_CHARSET"]
    assert "https://example.com/ok" not in fired.get("MISSING_CHARSET", set())


def test_charset_via_meta_tag_alone_is_sufficient(tmp_path):
    rows = [
        _row("https://example.com/ok", content_type="text/html", meta_charset="utf-8"),
    ]
    fired = _fired(_run(tmp_path, rows))
    assert "MISSING_CHARSET" not in fired


# -- MISSING_DOCTYPE ----------------------------------------------------------


def test_doctype_fires_for_missing_and_legacy_declarations(tmp_path):
    rows = [
        _row("https://example.com/modern", doctype="<!DOCTYPE html>"),
        _row("https://example.com/missing", doctype=""),
        _row(
            "https://example.com/legacy",
            doctype='<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN">',
        ),
    ]
    fired = _fired(_run(tmp_path, rows))
    assert "https://example.com/modern" not in fired.get("MISSING_DOCTYPE", set())
    assert "https://example.com/missing" in fired["MISSING_DOCTYPE"]
    assert "https://example.com/legacy" in fired["MISSING_DOCTYPE"]


# -- VIEWPORT_MISSING ----------------------------------------------------------


def test_viewport_fires_for_missing_and_sub_unity_scale(tmp_path):
    rows = [
        _row("https://example.com/ok", viewport="width=device-width, initial-scale=1"),
        _row("https://example.com/missing", viewport=""),
        _row("https://example.com/zoomed-out", viewport="initial-scale=0.5"),
    ]
    fired = _fired(_run(tmp_path, rows))
    assert "https://example.com/ok" not in fired.get("VIEWPORT_MISSING", set())
    assert "https://example.com/missing" in fired["VIEWPORT_MISSING"]
    assert "https://example.com/zoomed-out" in fired["VIEWPORT_MISSING"]


# -- NO_COMPRESSION -------------------------------------------------------------


def test_compression_fires_above_the_ignore_threshold_only(tmp_path):
    rows = [
        _row("https://example.com/gzip", encoding="gzip", size_bytes=5000),
        _row("https://example.com/plain-large", encoding="", size_bytes=5000),
        _row("https://example.com/plain-tiny", encoding="", size_bytes=200),
    ]
    fired = _fired(_run(tmp_path, rows))
    assert "https://example.com/gzip" not in fired.get("NO_COMPRESSION", set())
    assert "https://example.com/plain-large" in fired["NO_COMPRESSION"]
    assert "https://example.com/plain-tiny" not in fired.get("NO_COMPRESSION", set())


def test_compression_accepts_a_stacked_content_encoding(tmp_path):
    """#269: 'gzip, br' names two real compression codings. Comparing the whole
    header string against a single-token set flagged it as uncompressed even
    though the earlier single-token 'gzip' control was correctly accepted."""
    rows = [
        _row("https://example.com/stacked", encoding="gzip, br", size_bytes=5000),
        _row("https://example.com/stacked-spaced", encoding=" GZIP , BR ", size_bytes=5000),
        _row("https://example.com/reference", encoding="gzip", size_bytes=5000),
        _row("https://example.com/still-plain", encoding="", size_bytes=5000),
    ]
    fired = _fired(_run(tmp_path, rows))
    assert "https://example.com/stacked" not in fired.get("NO_COMPRESSION", set())
    assert "https://example.com/stacked-spaced" not in fired.get("NO_COMPRESSION", set())
    assert "https://example.com/reference" not in fired.get("NO_COMPRESSION", set())
    assert "https://example.com/still-plain" in fired["NO_COMPRESSION"]


def test_compression_fires_when_content_encoding_present_but_size_bytes_absent(tmp_path):
    """#445: a missing Size (bytes) column must not default every page's size to 0
    (always under the ignore threshold) and let a genuinely uncompressed page pass
    as clean -- the column simply wasn't measured, so the ignore threshold cannot
    apply."""
    cols = [c for c in COLS if c != "Size (bytes)"]
    d = tmp_path / "exports"
    d.mkdir()
    with open(d / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        w.writerow(_row("https://example.com/plain-large", encoding="")[:-1])
        w.writerow(_row("https://example.com/gzip", encoding="gzip")[:-1])
    res = run_audit(input_mode="parse-exports", exports_dir=str(d), log=lambda m: None)
    fired = _fired(res)
    assert "https://example.com/plain-large" in fired["NO_COMPRESSION"]
    assert "https://example.com/gzip" not in fired.get("NO_COMPRESSION", set())


# -- end-to-end through a native seohead crawl, not a hand-typed CSV ---------
# The issue's whole premise is that a crawl already fetches every page and can
# evaluate these audits at no extra request cost; this exercises exactly that
# path (collect_urls -> build_evidence -> AuditContext -> run_rules), network
# free, with a fake fetcher standing in for the HTTP client.


class _FakeResponse:
    def __init__(self, text, headers, status_code=200):
        self.text = text
        self.status_code = status_code
        self.headers = headers


_GOOD_HTML = (
    "<!DOCTYPE html><html><head>"
    '<meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    "<title>Good page</title></head>"
    "<body>" + ("Enough body text to be a real page. " * 60) + "</body></html>"
)
_BAD_HTML = (
    "<html><head><title>Bad page</title></head>"
    "<body>"
    + ("Enough body text to clear the compression ignore threshold. " * 60)
    + "</body></html>"
)


def _fetcher(mapping):
    def fetch(url):
        return mapping[url]

    return fetch


def test_checks_fire_from_a_real_crawl_not_just_a_hand_typed_export():
    mapping = {
        "https://example.com/good": _FakeResponse(
            _GOOD_HTML,
            {"content-type": "text/html; charset=utf-8", "content-encoding": "gzip"},
        ),
        "https://example.com/bad": _FakeResponse(
            _BAD_HTML,
            {"content-type": "text/html"},
        ),
    }
    crawl_result = collect_urls(
        list(mapping),
        fetcher=_fetcher(mapping),
        sleeper=lambda _seconds: None,
    )
    evidence = build_evidence(crawl_result)
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])

    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))
    run_rules(ctx)

    fired: dict[str, set[str]] = {}
    for issue in ctx.issues:
        fired.setdefault(issue.check, set()).add(issue.target_url)

    for check_id in ("MISSING_CHARSET", "MISSING_DOCTYPE", "VIEWPORT_MISSING", "NO_COMPRESSION"):
        assert "https://example.com/bad" in fired[check_id]
        assert "https://example.com/good" not in fired.get(check_id, set())


# -- regression: a redirect/error stub must not be judged as the site's own document (#133) --
# is_html was Content-Type only, so a 301's redirect stub and a 404's error stub — both
# routinely served as text/html — were judged by these checks exactly like the live document
# they redirect from or fail to be. Bodies below have no doctype, no viewport meta and no
# compression, same as a real generic Apache/Nginx/IIS stub; the "reference" page supplies the
# opposite evidence so each check has something present to skip against, per its honesty
# contract, and stays exercised rather than skipped outright.

_STUB_HTML = (
    "<html><head><title>Stub</title></head><body>"
    + ("A generic, tiny, server-generated body — not the site's page. " * 30)
    + "</body></html>"
)


def test_lighthouse_checks_never_fire_on_a_redirect_or_error_stub():
    mapping = {
        "https://example.com/reference": _FakeResponse(
            _GOOD_HTML,
            {"content-type": "text/html; charset=utf-8", "content-encoding": "gzip"},
        ),
        "https://example.com/live": _FakeResponse(
            _BAD_HTML,
            {"content-type": "text/html"},
        ),
        "https://example.com/old-page": _FakeResponse(
            _STUB_HTML,
            {"content-type": "text/html", "location": "https://example.com/live"},
            status_code=301,
        ),
        "https://example.com/gone": _FakeResponse(
            _STUB_HTML,
            {"content-type": "text/html"},
            status_code=404,
        ),
    }
    crawl_result = collect_urls(
        list(mapping),
        fetcher=_fetcher(mapping),
        sleeper=lambda _seconds: None,
    )
    evidence = build_evidence(crawl_result)
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])

    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))

    # Acceptance criterion 1: the shared population itself excludes both stubs.
    assert [p.url for p in ctx.html_pages()] == [
        "https://example.com/reference",
        "https://example.com/live",
    ]

    run_rules(ctx)
    fired: dict[str, set[str]] = {}
    for issue in ctx.issues:
        fired.setdefault(issue.check, set()).add(issue.target_url)

    for check_id in ("MISSING_CHARSET", "MISSING_DOCTYPE", "VIEWPORT_MISSING", "NO_COMPRESSION"):
        # Fires on the real defective document...
        assert "https://example.com/live" in fired[check_id]
        # ...never on the stubs standing in for a redirect or an error.
        assert "https://example.com/old-page" not in fired.get(check_id, set())
        assert "https://example.com/gone" not in fired.get(check_id, set())


def test_a_header_charset_cannot_prove_another_page_is_missing_one(tmp_path):
    """The case no fixture covered: no Meta Charset column at all, and some pages
    carrying charset in Content-Type while others do not.

    Firing on the second page would report a defect nobody measured -- its HTML
    may well declare <meta charset>, which an export without that column does not
    show. Before #396 the check ran here on exactly that reasoning and produced a
    finding; it must skip instead, and the reason must name the missing column
    rather than claim Content-Type carries no charset, which is untrue on this
    very input.
    """
    import csv

    from seohead.sf.core.audit import run_audit

    exports = tmp_path / "exports"
    exports.mkdir()
    with open(exports / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Address", "Content Type", "Status Code", "Indexability"])
        writer.writerow(["https://example.com/a", "text/html; charset=UTF-8", "200", "Indexable"])
        writer.writerow(["https://example.com/b", "text/html", "200", "Indexable"])
    result = run_audit(input_mode="parse-exports", exports_dir=str(exports), log=lambda m: None)

    assert not [i for i in result.issues if i.check == "MISSING_CHARSET"]
    reason = next(s.reason for s in result.skipped if s.id == "MISSING_CHARSET")
    assert "Meta Charset column" in reason
    assert "Content-Type carries no charset" not in reason
