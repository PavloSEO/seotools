"""The seam between ``parser.parse_html`` and ``crawl.collect`` over Open Graph keys.

Both modules were correct on their own. ``parse_html`` stores each tag under its full
property name -- ``{"og:title": ...}`` -- and says so in its own docstring; ``collect``
asked the same dict for ``"title"``. ``dict.get`` answered ``None`` on every page, so
every native crawl ever run recorded empty ``og_title``, ``og_description`` and
``og_image``, and an audit reading those columns reported "Open Graph present but its
title is empty" about sites whose Open Graph is fine (#646). Nothing caught it because
no test spanned both halves: a parser test asserted the prefixed keys, a collect test
asserted the record shape, and the key name between them was never compared.

So these tests crawl two loopback fixture sites -- one carrying Open Graph, one carrying
none -- and follow the value the whole way: parser -> collect -> ``pages.jsonl`` -> the
``pages`` table of the scan artifact -> the ``OG:Title`` column of the Internal:All frame
the analyzer reads. The absent site is here for the other direction: empty because the
page has no Open Graph must stay distinguishable from empty because nobody read it, and
must not be reported as a defect on that basis alone.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from seohead.crawl import evidence as evidence_module
from seohead.servers import handlers
from seohead.storage import import_run, open_scan
from seohead.tools.parser import parse_html

# The commit the fixture run is attributed to; import_run demands a full lowercase SHA
# and never reads it back as anything but an identifier.
BUILD = "25fd2ed032a31d63c5811722619e35c14b631476"

OG_TITLE = "A social title the crawler must record"
OG_DESCRIPTION = "A social description, distinct from the meta description on the same page."
OG_IMAGE = "https://cdn.example/preview.png"

OG_TAGS = (
    f'<meta property="og:title" content="{OG_TITLE}">'
    f'<meta property="og:description" content="{OG_DESCRIPTION}">'
    f'<meta property="og:image" content="{OG_IMAGE}">'
)

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<meta name="description" content="A fixture page whose description is long enough to clear the audit's length threshold.">
{og}
</head><body>
<main><h1>{title}</h1>
<p>Body copy for the fixture page, with enough words in it to be measured as a real page
rather than as a thin one.</p>
<p><a href="/second/">Second</a> <a href="/">Home</a></p>
</main>
</body></html>"""

ROBOTS = "User-agent: *\nAllow: /\n"


def _handler(with_og: bool) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/robots.txt":
                return self._send(ROBOTS.encode(), "text/plain; charset=utf-8")
            if path in ("/", "/second/"):
                title = "Home" if path == "/" else "Second"
                html = PAGE.format(title=title, og=OG_TAGS if with_og else "")
                return self._send(html.encode(), "text/html; charset=utf-8")
            self._send(b"<html><body>not found</body></html>", "text/html", status=404)

        def log_message(self, format: str, *args) -> None:
            pass

    return Handler


@contextlib.contextmanager
def _serve(*, with_og: bool) -> Iterator[str]:
    """Serve the fixture site on an OS-assigned loopback port for the duration of the block."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(with_og))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _crawl(tmp_path: Path, monkeypatch, *, with_og: bool) -> tuple[str, Path, list[dict]]:
    """Run one real crawl of the fixture site and return its base URL, output dir and frame."""
    # The crawler refuses private-network targets unless explicitly authorized; a loopback
    # fixture is exactly the case that authorization exists for.
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    monkeypatch.setenv("SEOHEAD_HTTP_CACHE_DIR", "off")
    frames: list[dict] = []
    real_build_evidence = evidence_module.build_evidence

    def spy(*args, **kwargs):
        built = real_build_evidence(*args, **kwargs)
        frames.append(built["frames"]["internal_all"])
        return built

    out_dir = tmp_path / ("with-og" if with_og else "no-og")
    with (
        _serve(with_og=with_og) as base_url,
        patch.object(evidence_module, "build_evidence", spy),
    ):
        result = handlers.crawl_site(
            url=f"{base_url}/", out_dir=str(out_dir), max_urls=10, config=None
        )
    assert result["urls_collected"] == 2, result
    assert frames, "the crawl must have projected its pages onto the analyzer's frame"
    frames[:] = [frames[0]]
    return base_url, out_dir, frames


def _pages(out_dir: Path) -> dict[str, dict]:
    lines = (out_dir / "pages.jsonl").read_text(encoding="utf-8").splitlines()
    return {row["url"]: row for row in (json.loads(line) for line in lines if line.strip())}


@pytest.fixture(scope="module")
def monkeypatch_module():
    from _pytest.monkeypatch import MonkeyPatch

    patch_ = MonkeyPatch()
    yield patch_
    patch_.undo()


@pytest.fixture(scope="module")
def crawled_with_og(tmp_path_factory, monkeypatch_module):
    return _crawl(tmp_path_factory.mktemp("og-present"), monkeypatch_module, with_og=True)


@pytest.fixture(scope="module")
def crawled_without_og(tmp_path_factory, monkeypatch_module):
    return _crawl(tmp_path_factory.mktemp("og-absent"), monkeypatch_module, with_og=False)


# -- the key shape both halves have to agree on -------------------------------


def test_the_parser_keys_open_graph_by_its_full_property_name():
    """The canonical shape, asserted here so the two modules cannot drift apart again.

    This is the fact ``collect`` got wrong: the dict is keyed ``"og:title"``, and the
    unprefixed name it used to ask for is not in it at all.
    """
    parsed = parse_html(
        '<meta property="og:title" content="X">'
        '<meta property="og:description" content="Y">'
        '<meta property="og:image" content="https://cdn.example/z.png">',
        "https://example.com/",
    )
    assert parsed["og"] == {
        "og:title": "X",
        "og:description": "Y",
        "og:image": "https://cdn.example/z.png",
    }
    for unprefixed in ("title", "description", "image"):
        assert parsed["og"].get(unprefixed) is None


def test_the_collector_reads_the_keys_the_parser_writes():
    """The seam itself, with no crawl around it: parse a document, record it, compare."""
    from seohead.crawl.collect import _record_from_parsed

    parsed = parse_html(f"<html><head>{OG_TAGS}</head><body></body></html>", "https://example.com/")
    record = _record_from_parsed(parsed)
    assert record["og_title"] == OG_TITLE
    assert record["og_description"] == OG_DESCRIPTION
    assert record["og_image"] == OG_IMAGE


# -- direction one: a page that carries Open Graph records it, all the way -----


def test_a_crawl_records_the_open_graph_a_page_carries(crawled_with_og):
    base_url, out_dir, _frames = crawled_with_og
    pages = _pages(out_dir)
    assert set(pages) == {f"{base_url}/", f"{base_url}/second/"}
    for url, page in pages.items():
        assert page["og_title"] == OG_TITLE, url
        assert page["og_description"] == OG_DESCRIPTION, url
        assert page["og_image"] == OG_IMAGE, url


def test_the_recorded_open_graph_survives_into_the_scan_artifact(crawled_with_og, tmp_path):
    """pages.jsonl is an intermediate; the ``pages`` table is what a later question reads."""
    _base_url, out_dir, _frames = crawled_with_og
    artifact = import_run(out_dir, tmp_path / "scan.sqlite", producer_build=BUILD)
    con = open_scan(artifact)
    try:
        rows = con.execute(
            "SELECT og_title, og_description, og_image FROM pages ORDER BY page_ordinal"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 2
    for row in rows:
        assert row["og_title"] == OG_TITLE
        assert row["og_description"] == OG_DESCRIPTION
        assert row["og_image"] == OG_IMAGE


def test_the_recorded_open_graph_reaches_the_analyzer_frame(crawled_with_og):
    """``evidence`` projects the record onto SF's own column names; that is what checks read."""
    _base_url, _out_dir, frames = crawled_with_og
    frame = frames[0]
    assert set(frame["OG:Title"]) == {OG_TITLE}
    assert set(frame["OG:Description"]) == {OG_DESCRIPTION}
    assert set(frame["OG:Image"]) == {OG_IMAGE}


def test_a_site_with_open_graph_has_its_open_graph_check_evaluated(crawled_with_og):
    """The check must run and find nothing -- not skip for want of a column it now has.

    Both readings print no OG_MISSING finding, which is why the bug survived: the audit
    of a site with Open Graph looked identical to the audit of a site without it.
    """
    _base_url, out_dir, _frames = crawled_with_og
    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert not [issue for issue in audit["issues"] if issue["check"] == "OG_MISSING"]
    skipped = {entry["id"] for entry in audit["run"]["checks_skipped"]}
    assert "OG_MISSING" not in skipped


# -- direction two: absent must not read as measured ---------------------------


def test_a_page_with_no_open_graph_records_empty(crawled_without_og):
    _base_url, out_dir, _frames = crawled_without_og
    for url, page in _pages(out_dir).items():
        assert page["og_title"] == "", url
        assert page["og_description"] == "", url
        assert page["og_image"] == "", url


def test_absent_open_graph_is_skipped_rather_than_flagged(crawled_without_og):
    """The honesty contract that the wrong key was quietly standing in for.

    With no Open Graph anywhere in the crawl, ``check_og`` must say it had nothing to
    read -- not emit one finding per page. Before the fix this branch was reached on
    every site, including the ones whose pages do carry Open Graph.
    """
    _base_url, out_dir, _frames = crawled_without_og
    audit = json.loads((out_dir / "audit.json").read_text(encoding="utf-8"))
    assert not [issue for issue in audit["issues"] if issue["check"] == "OG_MISSING"]
    skipped = {entry["id"]: entry.get("reason", "") for entry in audit["run"]["checks_skipped"]}
    assert "OG_MISSING" in skipped
    assert "Open Graph" in skipped["OG_MISSING"]
