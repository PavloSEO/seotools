from __future__ import annotations

import httpx

from seohead.crawl.sitemap_capture import SourceRoot, capture_declared_roots
from seohead.crawl.throttle import DispatchGate, Throttle
from seohead.tools import sitemap


class _Response:
    def __init__(self, url, content):
        self.url = url
        self.content = content

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self, documents):
        self.documents = documents

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def get(self, url, **_kwargs):
        return _Response(url, self.documents[url])


def test_streaming_sink_expands_nested_index_without_urls_or_seen_map(monkeypatch):
    root = "https://example.test/root.xml"
    child = "https://example.test/child.xml"
    documents = {
        root: b"<sitemapindex><sitemap><loc>https://example.test/child.xml</loc></sitemap></sitemapindex>",
        child: b"<urlset><url><loc>https://example.test/a/</loc></url><url><loc>https://example.test/a</loc></url><url><loc>https://example.test/b</loc></url></urlset>",
    }
    monkeypatch.setattr(
        sitemap, "http_client", lambda *_args, **_kwargs: (_Client(documents), False)
    )
    entries = []
    result = sitemap.crawl(root, sink=entries.append)
    assert result["count"] == 2
    assert result["urls"] == []
    assert result["duplicates"] == []
    assert result["duplicate_count"] == 1
    assert [entry["loc_normalized"] for entry in entries] == [
        "https://example.test/a",
        "https://example.test/b",
    ]


def test_sitemap_request_gate_paces_httpx_redirect_hops_and_nested_documents(monkeypatch):
    now = [0.0]
    calls = []

    def handler(request):
        calls.append((now[0], request.url.path))
        if request.url.path == "/root.xml":
            return httpx.Response(302, headers={"location": "/index.xml"}, request=request)
        if request.url.path == "/index.xml":
            return httpx.Response(
                200,
                content=(
                    b"<sitemapindex><sitemap><loc>https://example.test/child.xml</loc>"
                    b"</sitemap></sitemapindex>"
                ),
                request=request,
            )
        return httpx.Response(
            200,
            content=b"<urlset><url><loc>https://example.test/page</loc></url></urlset>",
            request=request,
        )

    def client_factory(_timeout, **kwargs):
        return (
            httpx.Client(
                transport=httpx.MockTransport(handler),
                follow_redirects=kwargs["follow_redirects"],
                max_redirects=kwargs["max_redirects"],
                event_hooks=kwargs.get("event_hooks"),
            ),
            False,
        )

    monkeypatch.setattr(sitemap, "http_client", client_factory)
    throttle = Throttle(min_delay=1.0, adaptive=False)
    gate = DispatchGate(
        throttle,
        lambda seconds: now.__setitem__(0, now[0] + seconds),
        lambda: now[0],
    )

    result = sitemap.crawl("https://example.test/root.xml", concurrency=1, request_gate=gate.wait_turn)

    assert result["count"] == 1
    assert calls == [(0.0, "/root.xml"), (1.0, "/index.xml"), (2.0, "/child.xml")]


def test_capture_streams_root_members_in_global_order_and_chunks():
    members = []
    seeds = []
    finished = []

    def crawl_fn(url, *, concurrency, sink):
        for loc in (f"{url}/one", f"{url}/two"):
            sink({"loc": loc})
        return {"ok": True, "root": url, "count": 2, "errors": [], "truncated": False}

    result = capture_declared_roots(
        [
            SourceRoot(7, "https://example.test/a.xml", "explicit"),
            SourceRoot(8, "https://example.test/b.xml", "robots"),
        ],
        write_sitemap_members=lambda sitemap_id, rows: members.append((sitemap_id, rows)),
        finish_sitemap=lambda sitemap_id, complete, reason: finished.append(
            (sitemap_id, complete, reason)
        ),
        emit_seed=lambda loc, root: seeds.append((loc, root.sitemap_id)),
        crawl_fn=crawl_fn,
    )
    assert members == [
        (7, [(0, "https://example.test/a.xml/one"), (1, "https://example.test/a.xml/two")]),
        (8, [(2, "https://example.test/b.xml/one"), (3, "https://example.test/b.xml/two")]),
    ]
    assert [seed[1] for seed in seeds] == [7, 7, 8, 8]
    assert [summary.complete for summary in result] == [True, True]
    assert finished == [
        (7, False, "capture in progress"),
        (7, True, ""),
        (8, False, "capture in progress"),
        (8, True, ""),
    ]


def test_capture_finishes_partial_root_after_error_without_claiming_complete():
    finished = []

    result = capture_declared_roots(
        [SourceRoot(9, "https://example.test/root.xml", "explicit")],
        write_sitemap_members=lambda *_args: None,
        finish_sitemap=lambda sitemap_id, complete, reason: finished.append(
            (sitemap_id, complete, reason)
        ),
        emit_seed=lambda *_args: None,
        crawl_fn=lambda *_args, **_kwargs: {
            "ok": True,
            "root": "https://example.test/root.xml",
            "count": 1,
            "errors": [{"error": "HTTP 500"}],
            "truncated": False,
        },
    )
    assert result[0].complete is False
    assert result[0].reason == "HTTP 500"
    assert finished == [(9, False, "capture in progress"), (9, False, "HTTP 500")]
