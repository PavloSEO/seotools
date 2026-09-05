"""X-Robots-Tag must feed the donor/follow verdict in backlinks-check (issue #322).

The tool's page-directive model says page-level directives override per-link
``rel`` values. Before this fix, ``_inspect_donor`` only read ``<meta
name="robots">``/``<meta name="googlebot">`` and never looked at the response
header, so a donor page that blocks indexing purely through an HTTP
``X-Robots-Tag`` header was reported as indexable and dofollow.
"""

from __future__ import annotations

from seohead.recon import backlinks


class _Response:
    def __init__(
        self, headers=None, html=None, status_code=200, url="https://donor.example.test/article"
    ):
        self.status_code = status_code
        self.url = url
        self.headers = headers or {}
        self.text = html or ('<a href="https://target.example.test/landing">source</a>')


class _Client:
    def __init__(self, response: _Response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        return self._response


def _run(monkeypatch, response: _Response):
    monkeypatch.setattr(backlinks, "http_client", lambda *a, **k: (_Client(response), False))
    return backlinks.check_backlinks(
        "https://target.example.test/landing",
        ["https://donor.example.test/article"],
    )


def test_global_x_robots_tag_noindex_nofollow_blocks_the_verdict(monkeypatch):
    """Positive control: a global X-Robots-Tag must block indexability and follow."""
    response = _Response(headers={"X-Robots-Tag": "noindex, nofollow"})
    result = _run(monkeypatch, response)

    row = result["results"][0]
    assert row["found"] is True
    assert row["donor_indexable"] is False
    assert row["links"][0]["follow"] is False
    assert result["summary"] == {
        "found": 1,
        "missing": 0,
        "dofollow": 0,
        "nofollow": 1,
        "on_noindex_page": 1,
    }


def test_googlebot_scoped_x_robots_tag_blocks_the_verdict(monkeypatch):
    """A Googlebot-scoped header is Google-effective and must also block the verdict."""
    response = _Response(headers={"X-Robots-Tag": "googlebot: noindex, nofollow"})
    result = _run(monkeypatch, response)

    row = result["results"][0]
    assert row["donor_indexable"] is False
    assert row["links"][0]["follow"] is False


def test_bingbot_only_x_robots_tag_does_not_block_the_verdict(monkeypatch):
    """Negative control: a Bingbot-only directive is out of Google's scope and must stay silent."""
    response = _Response(headers={"X-Robots-Tag": "bingbot: noindex"})
    result = _run(monkeypatch, response)

    row = result["results"][0]
    assert row["donor_indexable"] is True
    assert row["links"][0]["follow"] is True
    assert result["summary"]["dofollow"] == 1
    assert result["summary"]["on_noindex_page"] == 0


def test_meta_robots_noindex_behaviour_is_unchanged(monkeypatch):
    """Existing meta-robots behaviour (no X-Robots-Tag header at all) must not regress."""
    html = (
        '<meta name="robots" content="noindex, nofollow">'
        '<a href="https://target.example.test/landing">source</a>'
    )
    response = _Response(html=html)
    result = _run(monkeypatch, response)

    row = result["results"][0]
    assert row["donor_indexable"] is False
    assert row["links"][0]["follow"] is False
    assert row["links"][0]["blocked_by"] == ["page robots nofollow"]


def test_link_rel_reasons_stay_distinct_from_page_level_reasons(monkeypatch):
    """A rel=sponsored link on an otherwise-clean, indexable page keeps its own reason."""
    html = '<a href="https://target.example.test/landing" rel="sponsored">source</a>'
    response = _Response(html=html)
    result = _run(monkeypatch, response)

    row = result["results"][0]
    assert row["donor_indexable"] is True
    link = row["links"][0]
    assert link["follow"] is False
    assert link["blocked_by"] == ["sponsored"]
