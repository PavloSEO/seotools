"""#357: the crawl keeps what the hreflang tags say, not only where they sat.

``_hreflang_tags`` always found every ``<link rel="alternate" hreflang>``;
``parse_html`` used them for one boolean about their position and discarded the
language codes and the alternate URLs. A native crawl therefore could not answer
whether a page was localised, and the analyzer's three hreflang checks -- which
read the *All Hreflang* frame and nothing else -- had nothing to read unless a
Screaming Frog export was supplied.
"""

from __future__ import annotations

from seohead.crawl.collect import collect_urls as _collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.tools.parser import parse_html


def collect_urls(urls, **kw):
    kw.setdefault("sleeper", lambda _seconds: None)
    return _collect_urls(urls, **kw)


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}


def _fetch(mapping):
    def fetcher(url):
        return mapping[url]

    return fetcher


def _page(*alternates, body="<h1>t</h1>"):
    links = "".join(
        f'<link rel="alternate" hreflang="{lang}" href="{href}">' for lang, href in alternates
    )
    return (
        "<html><head><title>Title long enough for the length rule</title>"
        '<meta name="description" content="A description long enough for the length rule to pass.">'
        f"{links}</head><body>{body}</body></html>"
    )


# ── the declaration is kept as the document wrote it ────────────────────────


def test_the_code_and_the_href_survive_exactly_as_written():
    """A code with the wrong case or a malformed region is itself a finding.
    Normalising on capture would hide it, so both forms are kept: the href as
    written, and the same href resolved the way a browser resolves it."""
    parsed = parse_html(
        _page(("en-GB", "/en/about"), ("FR", "https://example.fr/a-propos")),
        "https://example.com/about",
    )
    assert parsed["hreflang"] == [
        {
            "lang": "en-GB",
            "raw_href": "/en/about",
            "url": "https://example.com/en/about",
        },
        {
            "lang": "FR",
            "raw_href": "https://example.fr/a-propos",
            "url": "https://example.fr/a-propos",
        },
    ]


def test_document_order_is_preserved():
    parsed = parse_html(
        _page(("de", "/de/"), ("es", "/es/"), ("x-default", "/")),
        "https://example.com/",
    )
    assert [a["lang"] for a in parsed["hreflang"]] == ["de", "es", "x-default"]


def test_an_alternate_that_points_nowhere_is_recorded_rather_than_dropped():
    """It declares a language and names no target. That is the malformed
    declaration a reciprocity check exists to find, not noise to filter out."""
    parsed = parse_html(
        '<html><head><link rel="alternate" hreflang="de"></head><body></body></html>',
        "https://example.com/",
    )
    assert parsed["hreflang"] == [{"lang": "de", "raw_href": "", "url": ""}]


def test_a_template_only_alternate_is_not_a_declaration():
    parsed = parse_html(
        "<html><head></head><body><template>"
        '<link rel="alternate" hreflang="de" href="/de/">'
        "</template></body></html>",
        "https://example.com/",
    )
    assert parsed["hreflang"] == []


def test_the_position_boolean_still_answers_its_own_question():
    """hreflang_outside_head answers where the tags sat. Both questions are
    wanted; keeping the declaration does not replace it."""
    in_head = parse_html(_page(("de", "/de/")), "https://example.com/")
    in_body = parse_html(
        '<html><head></head><body><link rel="alternate" hreflang="de" href="/de/"></body></html>',
        "https://example.com/",
    )
    assert in_head["position"]["hreflang_outside_head"] is False
    assert in_body["position"]["hreflang_outside_head"] is True


# ── the evidence reaches the analyzer ───────────────────────────────────────


def test_a_crawl_projects_its_declarations_onto_the_all_hreflang_frame():
    result = collect_urls(
        ["https://example.com/", "https://example.com/de/"],
        fetcher=_fetch(
            {
                "https://example.com/": FakeResponse(
                    _page(("en", "https://example.com/"), ("de", "https://example.com/de/"))
                ),
                "https://example.com/de/": FakeResponse(_page(("de", "https://example.com/de/"))),
            }
        ),
    )
    evidence = build_evidence(result)
    assert "all_hreflang" in evidence["found"]
    assert "all_hreflang" not in evidence["missing"]
    frame = evidence["frames"]["all_hreflang"]
    rows = {(row.Source, row.Destination, row.Hreflang) for row in frame.itertuples()}
    assert rows == {
        ("https://example.com/", "https://example.com/", "en"),
        ("https://example.com/", "https://example.com/de/", "de"),
        ("https://example.com/de/", "https://example.com/de/", "de"),
    }


def test_a_site_that_declares_nothing_leaves_the_frame_absent():
    """An empty frame would read as "this site has no hreflang errors" on a site
    that never claimed to be localised -- a clean bill of health nobody asked
    for and nothing measured. Absent, the checks skip and say why."""
    result = collect_urls(
        ["https://example.com/"],
        fetcher=_fetch({"https://example.com/": FakeResponse(_page())}),
    )
    evidence = build_evidence(result)
    assert "all_hreflang" not in evidence["found"]
    assert "all_hreflang" in evidence["missing"]


# ── the checks that had nothing to read now answer ──────────────────────────


def _audit_from(result):
    """The same assembly crawl_site performs, minus the network-touching stages."""
    from seohead.sf.config import load_config
    from seohead.sf.core.context import AuditContext
    from seohead.sf.core.inlinks import run_inlinks
    from seohead.sf.core.loader import LoadedExports

    evidence = build_evidence(result)
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])
    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))
    run_inlinks(ctx)
    return ctx


def test_a_broken_return_link_is_found_without_a_screaming_frog_export():
    """The English page names the German one; the German page names only itself.
    Google's contract requires the annotation to be reciprocal, and until now the
    only way to see this was a Bulk Export -> Links -> All Hreflang file."""
    result = collect_urls(
        ["https://example.com/", "https://example.com/de/"],
        fetcher=_fetch(
            {
                "https://example.com/": FakeResponse(
                    _page(("en", "https://example.com/"), ("de", "https://example.com/de/"))
                ),
                # No link back to the English page: this is the defect.
                "https://example.com/de/": FakeResponse(_page(("de", "https://example.com/de/"))),
            }
        ),
    )
    ctx = _audit_from(result)
    missing = [i for i in ctx.issues if i.check == "HREFLANG_MISSING_RETURN_LINK"]
    assert [i.target_url for i in missing] == ["https://example.com/de/"]
    assert "HREFLANG_MISSING_RETURN_LINK" not in {s.id for s in ctx.skipped}


def test_a_reciprocated_pair_is_silent():
    """The negative control. Both pages name each other, so nothing fires --
    and the check must have run rather than skipped, or the silence proves
    nothing."""
    both = (("en", "https://example.com/"), ("de", "https://example.com/de/"))
    result = collect_urls(
        ["https://example.com/", "https://example.com/de/"],
        fetcher=_fetch(
            {
                "https://example.com/": FakeResponse(_page(*both)),
                "https://example.com/de/": FakeResponse(_page(*both)),
            }
        ),
    )
    ctx = _audit_from(result)
    assert not [i for i in ctx.issues if i.check == "HREFLANG_MISSING_RETURN_LINK"]
    assert "HREFLANG_MISSING_RETURN_LINK" not in {s.id for s in ctx.skipped}


def test_without_any_declaration_the_check_skips_and_says_why():
    result = collect_urls(
        ["https://example.com/"],
        fetcher=_fetch({"https://example.com/": FakeResponse(_page())}),
    )
    ctx = _audit_from(result)
    assert "HREFLANG_MISSING_RETURN_LINK" in {s.id for s in ctx.skipped}
    assert not [i for i in ctx.issues if i.check == "HREFLANG_MISSING_RETURN_LINK"]


def test_the_declarations_reach_pages_jsonl(tmp_path):
    """The acceptance criterion is that a crawled page's alternates appear in
    pages.jsonl with the code and URL as the document wrote them."""
    import json

    from seohead.crawl.collect import _write

    result = collect_urls(
        ["https://example.com/"],
        fetcher=_fetch(
            {"https://example.com/": FakeResponse(_page(("FR", "https://example.fr/a-propos")))}
        ),
    )
    path = tmp_path / "pages.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in result.pages:
            _write(handle, record)
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["hreflang"] == [
        {
            "lang": "FR",
            "raw_href": "https://example.fr/a-propos",
            "url": "https://example.fr/a-propos",
        }
    ]
