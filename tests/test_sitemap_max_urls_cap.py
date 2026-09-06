"""The global MAX_URLS cap in crawl() must be a hard ceiling, not approximate (#473).

``crawl()`` processes one frontier batch per iteration via ``pool.map()``. The cap check
lived only in the inner loop over one document's entries: once it tripped and set
``truncated = True``, the outer loop kept iterating the remaining documents in the same
batch, and each of those appended at least one more URL before its own inner check ever
fired. ``count`` could then land strictly above ``MAX_URLS`` while still reporting
``truncated: True``, contradicting the module's own documented invariant.
"""

from __future__ import annotations

import httpx

from seohead.tools import sitemap as S

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def _urlset(n: int, prefix: str) -> bytes:
    items = "".join(f"<url><loc>https://example.com/{prefix}/{i}</loc></url>" for i in range(n))
    return f"<urlset {NS}>{items}</urlset>".encode()


def _index(*locs: str) -> bytes:
    items = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in locs)
    return f"<sitemapindex {NS}>{items}</sitemapindex>".encode()


def _install_fake_transport(tree: dict[str, bytes], monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = tree.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=body, headers={"content-type": "application/xml"})

    def fake_http_client(*_args, **_kwargs):
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), True

    monkeypatch.setattr(S, "http_client", fake_http_client)


def test_max_urls_is_a_hard_ceiling_across_a_frontier_batch(monkeypatch):
    # Five children in one frontier batch (concurrency=1 still fetches the whole level as
    # one batch): 40, 10, 10, 10, 10 URLs declared. MAX_URLS=50 trips inside child #1
    # (0-indexed) after 10 more URLs (40+10=50); children #2-#4 must contribute nothing.
    children = [f"https://example.com/child{i}.xml" for i in range(5)]
    tree = {
        "https://example.com/index.xml": _index(*children),
        children[0]: _urlset(40, "c0"),
        children[1]: _urlset(10, "c1"),
        children[2]: _urlset(10, "c2"),
        children[3]: _urlset(10, "c3"),
        children[4]: _urlset(10, "c4"),
    }
    _install_fake_transport(tree, monkeypatch)
    monkeypatch.setattr(S, "MAX_URLS", 50)

    result = S.crawl("https://example.com/index.xml", concurrency=1)

    assert result["truncated"] is True
    assert result["count"] == 50, "must be an exact ceiling, not merely close to it"
    assert len(result["urls"]) == 50


def test_a_tree_under_the_cap_is_collected_in_full(monkeypatch):
    """Negative control: same shape, well under the cap -> nothing truncated, nothing dropped."""
    children = [f"https://example.com/child{i}.xml" for i in range(5)]
    tree = {
        "https://example.com/index.xml": _index(*children),
        children[0]: _urlset(4, "c0"),
        children[1]: _urlset(1, "c1"),
        children[2]: _urlset(1, "c2"),
        children[3]: _urlset(1, "c3"),
        children[4]: _urlset(1, "c4"),
    }
    _install_fake_transport(tree, monkeypatch)
    monkeypatch.setattr(S, "MAX_URLS", 50)

    result = S.crawl("https://example.com/index.xml", concurrency=1)

    assert result["truncated"] is False
    assert result["count"] == 8
    assert len(result["urls"]) == 8
