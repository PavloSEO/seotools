"""Nested sitemap-index parsing, retry/failure tracking, SSRF + XXE guards."""

from __future__ import annotations

import gzip

import pytest

from seohead.sf.core import sitemap_coverage as S

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def _index(*locs):
    items = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in locs)
    return f"<sitemapindex {NS}>{items}</sitemapindex>".encode()


def _urlset(*locs):
    items = "".join(f"<url><loc>{u}</loc><lastmod>2025-01-01</lastmod></url>" for u in locs)
    return f"<urlset {NS}>{items}</urlset>".encode()


def test_two_level_nested_index(monkeypatch):
    # Root index -> two sub-indexes -> leaf URL sets.
    tree = {
        "https://example.com/index-a.xml": _index(
            "https://example.com/a-1.xml", "https://example.com/a-2.xml"
        ),
        "https://example.com/index-b.xml": _index("https://example.com/b-1.xml"),
        "https://example.com/a-1.xml": _urlset(
            "https://example.com/a/1", "https://example.com/a/2"
        ),
        "https://example.com/a-2.xml": _urlset("https://example.com/a/3"),
        "https://example.com/b-1.xml": _urlset("https://example.com/b/1"),
    }
    monkeypatch.setattr(S, "_fetch", lambda u, ua, t, retries=2: tree.get(u))
    root = _index("https://example.com/index-a.xml", "https://example.com/index-b.xml")
    fails: list[str] = []
    out = S._parse_sitemap_bytes(root, "ua", 1, set(), {"example.com"}, failures=fails)
    locs = sorted(e["loc"] for e in out)
    assert locs == [
        "https://example.com/a/1",
        "https://example.com/a/2",
        "https://example.com/a/3",
        "https://example.com/b/1",
    ]
    assert fails == []
    assert all(e["lastmod"] == "2025-01-01" for e in out)


def test_failed_child_is_tracked(monkeypatch):
    tree = {
        "https://example.com/a.xml": _urlset("https://example.com/1")
    }  # b.xml is intentionally missing to exercise failure tracking.
    monkeypatch.setattr(S, "_fetch", lambda u, ua, t, retries=2: tree.get(u))
    root = _index("https://example.com/a.xml", "https://example.com/b.xml")
    fails: list[str] = []
    out = S._parse_sitemap_bytes(root, "ua", 1, set(), {"example.com"}, failures=fails)
    assert [e["loc"] for e in out] == ["https://example.com/1"]
    assert fails == ["https://example.com/b.xml"]  # Do not silently drop failures.


def test_ssrf_blocks_foreign_host(monkeypatch):
    calls: list[str] = []

    def fake(u, ua, t, retries=2):
        calls.append(u)
        return _urlset("https://example.com/ok")

    monkeypatch.setattr(S, "_fetch", fake)
    root = _index("https://example.com/child.xml", "https://example.org/child.xml")
    S._parse_sitemap_bytes(root, "ua", 1, set(), {"example.com"}, failures=[])
    assert "https://example.com/child.xml" in calls
    assert "https://example.org/child.xml" not in calls  # Never fetch a foreign host.


def test_xxe_payload_rejected():
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        b"<urlset><url><loc>&e;</loc></url></urlset>"
    )
    assert S._parse_sitemap_bytes(xxe, "ua", 1, set(), set()) == []


def test_fetch_rejects_non_http_schemes():
    # SSRF/file-read guard: never touch file://, ftp://, etc. (no network needed)
    assert S._fetch("file:///etc/passwd", "ua", 1) is None
    assert S._fetch("ftp://host/x", "ua", 1) is None


def test_gunzip_bomb_guarded(monkeypatch):
    monkeypatch.setattr(S, "MAX_DECOMPRESSED_BYTES", 1000)
    bomb = gzip.compress(b"A" * 50_000)  # expands well past the lowered cap
    with pytest.raises(ValueError):
        S._safe_gunzip(bomb)


def test_sitemap_rejects_dtd_and_entities():
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        b"<urlset><url><loc>&e;</loc></url></urlset>"
    )
    assert S._parse_sitemap_bytes(xxe, "ua", 1, set(), set()) == []


def test_sitemap_parses_clean_urlset():
    xml = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/a</loc><lastmod>2025-01-01</lastmod></url>"
        b"<url><loc>https://example.com/b</loc></url></urlset>"
    )
    out = S._parse_sitemap_bytes(xml, "ua", 1, set(), {"x.com"})
    assert [e["loc"] for e in out] == ["https://example.com/a", "https://example.com/b"]
    assert out[0]["lastmod"] == "2025-01-01"


def test_bare_sitemap_xml_does_not_invent_an_origin_for_relative_locations():
    """A direct parser caller without a document URL keeps relative loc text intact."""
    out = S._parse_sitemap_bytes(
        _urlset("/relative-page", "https://example.com/absolute-page"),
        "ua",
        1,
        set(),
        set(),
    )

    assert [(entry["loc"], entry["source"]) for entry in out] == [
        ("/relative-page", ""),
        ("https://example.com/absolute-page", ""),
    ]


def test_a_parse_error_is_a_named_failure_not_a_silent_empty_result():
    """#146: a document that fetched fine but doesn't parse (an unescaped '&' is the
    classic generator bug) must be distinguishable from "no such document" -- both used
    to return [] with nothing recorded anywhere."""
    broken = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/a?x=1&y=2</loc></url></urlset>"  # bare '&'
    )
    fails: list[str] = []
    out = S._parse_sitemap_bytes(
        broken, "ua", 1, set(), set(), failures=fails, source="https://example.com/sitemap.xml"
    )
    assert out == []
    assert fails == ["https://example.com/sitemap.xml"], (
        "a parse failure on a fetched document must be named, the same way a fetch "
        "failure already is"
    )


def _mini_ctx(tmp_path, crawled_urls):
    """The minimum AuditContext run_sitemap() needs, built from a real SF export."""
    import csv

    from seohead.sf.config import load_config
    from seohead.sf.core.context import AuditContext
    from seohead.sf.core.loader import load_exports

    with open(tmp_path / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Address", "Content Type", "Status Code", "Indexability"])
        writer.writerows([[u, "text/html", "200", "Indexable"] for u in crawled_urls])
    return AuditContext(load_exports(str(tmp_path)), load_config(None))


def test_run_sitemap_reports_a_broken_sitemap_instead_of_claiming_none_was_set(
    monkeypatch, tmp_path
):
    """#146: network was enabled and a sitemap with a 200 and real content was fetched, so
    the skip reason "no sitemap URL set (no export and network disabled)" would itself be
    false -- both halves of it. A malformed sitemap must surface as a named fetch/parse
    failure, not disappear into that false reason.
    """
    ctx = _mini_ctx(tmp_path, ["https://example.com/a"])
    broken = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/a?x=1&y=2</loc></url></urlset>"
    )

    def fake_fetch(url, ua, timeout, retries=2):
        if url == "https://example.com/robots.txt":
            return b"User-agent: *\n"
        if url == "https://example.com/sitemap.xml":
            return broken
        return None

    monkeypatch.setattr(S, "_fetch", fake_fetch)
    summary = S.run_sitemap(ctx, sitemap_url="https://example.com/sitemap.xml")

    assert summary["urls_in_sitemap"] == 0
    assert summary["sitemap_fetch_failures"] == ["https://example.com/sitemap.xml"]
    assert any(issue.check == "SITEMAP_FETCH_INCOMPLETE" for issue in ctx.issues)
    skipped = {s.id: s.reason for s in ctx.skipped}
    assert "SITEMAP_DESYNC" in skipped
    assert skipped["SITEMAP_DESYNC"] != "no sitemap URL set (no export and network disabled)", (
        "network was enabled and a sitemap was fetched -- this reason is false on both counts"
    )
    assert "fetch/parse failed" in skipped["SITEMAP_DESYNC"]


def test_depth_cap_reports_incomplete_evidence_not_a_fetched_empty_sitemap(monkeypatch, tmp_path):
    """#312: a sitemap-index chain that reaches ``MAX_SITEMAP_DEPTH`` fetches and parses
    every document cleanly -- it simply stops following children -- so the resulting empty
    URL set must surface as truncated evidence (SITEMAP_FETCH_INCOMPLETE, a SITEMAP_DESYNC
    skip that names the depth cap), never as "sitemap fetched but declared zero URLs"."""
    ctx = _mini_ctx(tmp_path, ["https://example.com/"])
    names = [f"https://example.com/{n}.xml" for n in range(7)]

    def _index_doc(child):
        return (
            b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + f"<sitemap><loc>{child}</loc></sitemap>".encode()
            + b"</sitemapindex>"
        )

    responses = {
        "https://example.com/robots.txt": f"User-agent: *\nSitemap: {names[0]}\n".encode(),
        **{names[n]: _index_doc(names[n + 1]) for n in range(6)},
        names[6]: (
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>https://example.com/page</loc></url></urlset>"
        ),
    }

    def fake_fetch(url, ua, timeout, retries=2):
        return responses.get(url)

    monkeypatch.setattr(S, "_fetch", fake_fetch)
    summary = S.run_sitemap(ctx, sitemap_url=names[0])

    assert summary["urls_in_sitemap"] == 0
    assert any(issue.check == "SITEMAP_FETCH_INCOMPLETE" for issue in ctx.issues), (
        "a depth-truncated index must be named incomplete evidence, not silently empty"
    )
    skipped = {s.id: s.reason for s in ctx.skipped}
    assert "SITEMAP_DESYNC" in skipped
    assert skipped["SITEMAP_DESYNC"] != "sitemap fetched but declared zero URLs"
    assert "depth cap" in skipped["SITEMAP_DESYNC"]


def test_a_shallow_nested_index_is_unaffected_by_the_depth_guard(monkeypatch, tmp_path):
    """Negative control for #312: a nested index that resolves well within the depth cap
    keeps reporting real URLs and a real desync verdict -- the depth guard must not touch
    a chain that never reaches it."""
    ctx = _mini_ctx(tmp_path, ["https://example.com/page"])
    root = "https://example.com/root.xml"
    leaf = "https://example.com/leaf.xml"
    responses = {
        "https://example.com/robots.txt": f"User-agent: *\nSitemap: {root}\n".encode(),
        root: (
            b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + f"<sitemap><loc>{leaf}</loc></sitemap>".encode()
            + b"</sitemapindex>"
        ),
        leaf: (
            b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>https://example.com/page</loc></url></urlset>"
        ),
    }

    def fake_fetch(url, ua, timeout, retries=2):
        return responses.get(url)

    monkeypatch.setattr(S, "_fetch", fake_fetch)
    summary = S.run_sitemap(ctx, sitemap_url=root)

    assert summary["urls_in_sitemap"] == 1
    assert not any(issue.check == "SITEMAP_FETCH_INCOMPLETE" for issue in ctx.issues)
    skipped = {s.id: s.reason for s in ctx.skipped}
    assert "SITEMAP_DESYNC" not in skipped or "depth cap" not in skipped.get("SITEMAP_DESYNC", "")


def test_run_sitemap_resolves_relative_index_and_urlset_locations(monkeypatch, tmp_path):
    """#567: live SF sitemap coverage follows the same document-relative locations as crawl."""
    from seohead.tools.sitemap import parse_sitemap

    root = "https://example.com/sitemaps/index.xml"
    child = "https://example.com/sitemaps/parts/urls.xml"
    child_xml = _urlset("../relative-page", "https://example.com/absolute-page")
    expected_urls = [entry["loc"] for entry in parse_sitemap(child_xml, child)["urls"]]
    ctx = _mini_ctx(tmp_path, expected_urls)
    responses = {
        "https://example.com/robots.txt": b"User-agent: *\n",
        root: _index("parts/urls.xml"),
        child: child_xml,
    }
    calls: list[str] = []

    def fake_fetch(url, ua, timeout, retries=2):
        calls.append(url)
        return responses.get(url)

    monkeypatch.setattr(S, "_fetch", fake_fetch)
    summary = S.run_sitemap(ctx, sitemap_url=root)

    assert calls == ["https://example.com/robots.txt", root, child]
    assert expected_urls == [
        "https://example.com/sitemaps/relative-page",
        "https://example.com/absolute-page",
    ]
    assert summary["urls_in_sitemap"] == 2
    assert summary["in_sitemap_and_linked"] == sorted(expected_urls)
    assert summary["in_sitemap_not_in_crawl"] == 0


def test_a_genuinely_empty_urlset_still_reports_declared_zero(monkeypatch, tmp_path):
    """Negative control for #312: a sitemap that fetches fine and truly declares zero
    URLs (no index, no depth cap involved at all) keeps its original, honest reason."""
    ctx = _mini_ctx(tmp_path, ["https://example.com/"])
    empty = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'

    def fake_fetch(url, ua, timeout, retries=2):
        if url == "https://example.com/robots.txt":
            return b"User-agent: *\n"
        if url == "https://example.com/sitemap.xml":
            return empty
        return None

    monkeypatch.setattr(S, "_fetch", fake_fetch)
    summary = S.run_sitemap(ctx, sitemap_url="https://example.com/sitemap.xml")

    assert summary["urls_in_sitemap"] == 0
    assert not any(issue.check == "SITEMAP_FETCH_INCOMPLETE" for issue in ctx.issues)
    skipped = {s.id: s.reason for s in ctx.skipped}
    assert skipped["SITEMAP_DESYNC"] == "sitemap fetched but declared zero URLs"


def test_crawl_partial_withholds_sitemap_desync_as_a_named_skip(monkeypatch, tmp_path):
    """#362: a thresholded sitemap-versus-crawl verdict from run_sitemap's own comparison
    is unsound on a partial crawl -- the caller must be able to say so and get a named
    skip instead of a fired finding."""
    ctx = _mini_ctx(tmp_path, ["https://example.com/"])
    sitemap_xml = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/a</loc></url>"
        b"<url><loc>https://example.com/b</loc></url>"
        b"<url><loc>https://example.com/c</loc></url></urlset>"
    )

    def fake_fetch(url, ua, timeout, retries=2):
        if url == "https://example.com/robots.txt":
            return b"User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
        if url == "https://example.com/sitemap.xml":
            return sitemap_xml
        return None

    monkeypatch.setattr(S, "_fetch", fake_fetch)
    summary = S.run_sitemap(ctx, sitemap_url="https://example.com/sitemap.xml", crawl_partial=True)

    assert summary["urls_in_sitemap"] == 3
    assert not any(issue.check == "SITEMAP_DESYNC" for issue in ctx.issues)
    skipped = {s.id: s.reason for s in ctx.skipped}
    assert "SITEMAP_DESYNC" in skipped
    assert "partial" in skipped["SITEMAP_DESYNC"]


def test_a_complete_crawl_still_fires_sitemap_desync(monkeypatch, tmp_path):
    """Negative control for #362: the identical fixture with ``crawl_partial=False``
    (the default) still emits the finding -- withholding must be tied to partial state,
    not the mere existence of the parameter."""
    ctx = _mini_ctx(tmp_path, ["https://example.com/"])
    sitemap_xml = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/a</loc></url>"
        b"<url><loc>https://example.com/b</loc></url>"
        b"<url><loc>https://example.com/c</loc></url></urlset>"
    )

    def fake_fetch(url, ua, timeout, retries=2):
        if url == "https://example.com/robots.txt":
            return b"User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
        if url == "https://example.com/sitemap.xml":
            return sitemap_xml
        return None

    monkeypatch.setattr(S, "_fetch", fake_fetch)
    summary = S.run_sitemap(ctx, sitemap_url="https://example.com/sitemap.xml")

    assert summary["urls_in_sitemap"] == 3
    assert any(issue.check == "SITEMAP_DESYNC" for issue in ctx.issues)


def test_stale_lastmod_names_the_crawled_home_page_not_a_bare_origin(monkeypatch, tmp_path):
    """#285: SITEMAP_STALE_LASTMOD used ``_base_url`` directly, a bare origin with no
    path -- it matches no row in pages.jsonl, so log-scan's findings_are_about_crawled_urls
    rule flagged this site-wide finding as pointing outside the run's own page list. The
    other site-wide checks in this module already route through ``_site_target`` for the
    same reason; this one must too."""
    ctx = _mini_ctx(tmp_path, ["https://example.com/"])
    stale = (
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://example.com/</loc><lastmod>2020-01-01</lastmod></url></urlset>"
    )

    def fake_fetch(url, ua, timeout, retries=2):
        if url == "https://example.com/robots.txt":
            return b"User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
        if url == "https://example.com/sitemap.xml":
            return stale
        return None

    monkeypatch.setattr(S, "_fetch", fake_fetch)
    S.run_sitemap(ctx, sitemap_url="https://example.com/sitemap.xml")

    stale_issues = [i for i in ctx.issues if i.check == "SITEMAP_STALE_LASTMOD"]
    assert len(stale_issues) == 1
    assert stale_issues[0].target_url == "https://example.com/", (
        "target_url must be the crawled home page, not a bare origin absent from pages.jsonl"
    )


def test_explicit_sitemap_url_is_fetched_even_with_no_crawled_pages(monkeypatch, tmp_path):
    """#452: an explicit --sitemap target carries its own host and needs no crawled page
    to derive a base URL from. A crawl with no pages at all (empty/missing Internal:All)
    must not make this fall through to a fetch-nothing, false-skip-reason path."""
    ctx = _mini_ctx(tmp_path, [])
    assert ctx.pages == []
    called: list[str] = []

    def fake_fetch(url, ua, timeout, retries=2):
        called.append(url)
        return None

    monkeypatch.setattr(S, "_fetch", fake_fetch)
    S.run_sitemap(ctx, sitemap_url="https://example.com/sitemap.xml", compare_with_crawl=True)

    assert "https://example.com/sitemap.xml" in called, (
        "an explicit sitemap_url must be fetched even when the crawl produced no pages"
    )
    skipped = {s.id: s.reason for s in ctx.skipped}
    assert skipped.get("SITEMAP_DESYNC") != "no sitemap URL set (no export and network disabled)", (
        "a sitemap_url was passed and network was not disabled -- this reason is false on both counts"
    )


def test_no_sitemap_url_and_no_pages_still_skips_cleanly(monkeypatch, tmp_path):
    """Negative control for #452: with no sitemap_url/sitemap_urls and live_recheck off,
    an empty-pages crawl must still skip every check with today's existing reasons --
    the fix must not make the tool fetch anything when the caller gave it nothing to fetch."""
    ctx = _mini_ctx(tmp_path, [])
    assert ctx.pages == []
    called: list[str] = []
    monkeypatch.setattr(S, "_fetch", lambda url, ua, timeout, retries=2: called.append(url))

    S.run_sitemap(ctx)

    assert called == [], "no target was given -- nothing should be fetched"
    skipped = {s.id: s.reason for s in ctx.skipped}
    assert skipped["SITEMAP_DESYNC"] == "no sitemap URL set (no export and network disabled)"
    assert "no sitemap URL to check" in skipped["SITEMAP_NOT_IN_ROBOTS"]


def test_max_sitemap_urls_cap_records_truncation_of_remaining_children(monkeypatch):
    """#454: hitting MAX_SITEMAP_URLS mid-<sitemapindex> must not silently drop the
    remaining, never-fetched child sitemaps -- the same class of incomplete evidence the
    depth cap (#312) already names via ``truncated``."""
    monkeypatch.setattr(S, "MAX_SITEMAP_URLS", 5)

    def _urlset3(prefix):
        urls = "".join(f"<url><loc>https://example.com/{prefix}/{i}</loc></url>" for i in range(3))
        return (
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()
        )

    children = {
        "https://example.com/s1.xml": _urlset3("a"),
        "https://example.com/s2.xml": _urlset3("b"),
        "https://example.com/s3.xml": _urlset3("c"),
    }
    monkeypatch.setattr(S, "_fetch", lambda u, ua, t, retries=2: children.get(u))
    root = _index(
        "https://example.com/s1.xml",
        "https://example.com/s2.xml",
        "https://example.com/s3.xml",
    )
    failures: list[str] = []
    truncated: list[str] = []
    out = S._parse_sitemap_bytes(
        root,
        "ua",
        1,
        set(),
        {"example.com"},
        failures=failures,
        truncated=truncated,
        source="https://example.com/sitemap_index.xml",
    )
    assert len(out) == 6, "the two fetched children's URLs are still returned"
    assert failures == []
    assert truncated == ["https://example.com/s3.xml"], (
        "the child sitemap never fetched because of the URL cap must be named, not silently dropped"
    )


def test_max_sitemap_urls_cap_does_not_fire_on_a_genuinely_complete_parse(monkeypatch):
    """Negative control for #454: an index whose children stay under MAX_SITEMAP_URLS
    must report no truncation and no failures -- the fix must not fire on a genuinely
    complete parse."""
    monkeypatch.setattr(S, "MAX_SITEMAP_URLS", 100)

    def _urlset3(prefix):
        urls = "".join(f"<url><loc>https://example.com/{prefix}/{i}</loc></url>" for i in range(3))
        return (
            f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'.encode()
        )

    children = {
        "https://example.com/s1.xml": _urlset3("a"),
        "https://example.com/s2.xml": _urlset3("b"),
        "https://example.com/s3.xml": _urlset3("c"),
    }
    monkeypatch.setattr(S, "_fetch", lambda u, ua, t, retries=2: children.get(u))
    root = _index(
        "https://example.com/s1.xml",
        "https://example.com/s2.xml",
        "https://example.com/s3.xml",
    )
    failures: list[str] = []
    truncated: list[str] = []
    out = S._parse_sitemap_bytes(
        root,
        "ua",
        1,
        set(),
        {"example.com"},
        failures=failures,
        truncated=truncated,
        source="https://example.com/sitemap_index.xml",
    )
    assert len(out) == 9
    assert failures == []
    assert truncated == []
