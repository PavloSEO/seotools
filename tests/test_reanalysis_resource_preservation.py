"""A changed parser must preserve known resource evidence and name cold inputs."""

from dataclasses import replace

from seohead.storage import open_scan
from seohead.storage.reanalysis import derived_scan, replace_reparsed_page
from seohead.storage.reanalysis_pages import iterate_reparsed_pages
from tests.test_scan_resource_integration import _crawl, _Response, _runtime_versions, _settings


def test_reparse_keeps_resource_order_and_known_bodies_when_one_declaration_changes(tmp_path):
    source = tmp_path / "source.sqlite"
    unchanged = tmp_path / "same.sqlite"
    changed = tmp_path / "changed.sqlite"
    config = _settings(**{"resources.fetch": True})

    def fetcher(url):
        if url.endswith("robots.txt"):
            return _Response(b"User-agent: *\nAllow: /\n", "text/plain")
        if url.endswith(".js"):
            return _Response(url.encode(), "application/javascript")
        return _Response(
            b'<html><head><script src="/z.js"></script><script src="/a.js"></script>'
            b"</head><body>Owned synthetic fixture</body></html>",
            "text/html",
        )

    _crawl(source, config, fetcher)
    with derived_scan(source, unchanged, "test", "b" * 40, _runtime_versions()) as (scan, con):
        for replay in iterate_reparsed_pages(con, config):
            replace_reparsed_page(scan, replay)

    query = (
        "SELECT u.url,r.response_id,r.capture_state,r.reason FROM resource_refs r "
        "JOIN urls u ON u.url_id=r.resource_url_id ORDER BY r.kind,r.ordinal"
    )
    with open_scan(unchanged, require_audit=False) as con:
        original = [tuple(row) for row in con.execute(query)]
    assert [row[0] for row in original] == [
        "https://example.test/z.js",
        "https://example.test/a.js",
    ]
    assert all(row[1] is not None and row[2:] == ("measured", "") for row in original)

    with derived_scan(unchanged, changed, "test", "c" * 40, _runtime_versions()) as (scan, con):
        for replay in iterate_reparsed_pages(con, config):
            resources = dict(replay.resources)
            resources["static"] = (
                resources["static"][0],
                {"kind": "script", "url": "https://example.test/new.js", "raw_url": "/new.js"},
            )
            replace_reparsed_page(scan, replace(replay, resources=resources))
    with open_scan(changed, require_audit=False) as con:
        rows = [tuple(row) for row in con.execute(query)]
        assert (
            con.execute("SELECT COUNT(*) FROM responses WHERE purpose='script'").fetchone()[0] == 2
        )
    assert rows[0] == original[0]
    assert rows[1] == ("https://example.test/new.js", None, "not_fetched", "not_in_corpus")
