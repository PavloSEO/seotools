"""A valueless Set-Cookie header must not abort the fingerprint (issue #651).

``Set-Cookie: Secure; HttpOnly`` carries no ``name=value`` pair, so
``http.cookiejar`` stores ``Cookie(name='Secure', value=None)`` and
``httpx.Cookies.__getitem__`` raises ``KeyError`` for it. The old
``dict(resp.cookies)`` therefore failed on a name the mapping's own iterator had
produced, and ``tech-detect`` exited with a bare ``error: 'Secure'`` and no JSON
for a page that had been fetched successfully.

The malformed name is now kept with an empty value — several fingerprints match
on cookie name alone — and reported in ``malformed_cookies`` and in a finding, so
the site's broken header stays visible rather than being normalized away.
"""

import httpx

from seohead.recon import tech


def _response(headers: list[tuple[str, str]]) -> httpx.Response:
    """An offline response; no client, no socket, no network."""
    return httpx.Response(
        200,
        headers=[("content-type", "text/html"), *headers],
        content=b"<html><head></head><body>ok</body></html>",
        request=httpx.Request("GET", "https://example.test/"),
    )


def _client_returning(resp: httpx.Response):
    class _FakeClient:
        def get(self, _url):
            return resp

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    return lambda timeout: (_FakeClient(), None)


def test_dict_of_httpx_cookies_still_raises_on_a_valueless_cookie():
    """Pin the upstream behavior this fix works around."""
    resp = _response([("set-cookie", "a=1; Path=/"), ("set-cookie", "Secure; HttpOnly")])

    assert list(resp.cookies) == ["a", "Secure"]
    try:
        dict(resp.cookies)
    except KeyError as exc:
        assert exc.args[0] == "Secure"
    else:  # pragma: no cover - httpx changed; the workaround can then be revisited
        raise AssertionError("expected KeyError from dict(resp.cookies)")


def test_valueless_cookie_is_kept_with_an_empty_value_and_named():
    resp = _response([("set-cookie", "a=1; Path=/"), ("set-cookie", "Secure; HttpOnly")])

    cookies, malformed = tech.response_cookies(resp)

    assert cookies == {"a": "1", "Secure": ""}
    assert malformed == ["Secure"]


def test_a_well_formed_cookie_wins_over_a_valueless_one_of_the_same_name():
    """Two jar entries share a name when their paths differ; the real value survives."""
    resp = _response(
        [("set-cookie", "csrftoken; Path=/b; HttpOnly"), ("set-cookie", "csrftoken=abc; Path=/a")]
    )

    cookies, malformed = tech.response_cookies(resp)

    assert cookies == {"csrftoken": "abc"}
    assert malformed == ["csrftoken"]


def test_detect_tech_returns_json_despite_a_valueless_cookie(monkeypatch):
    """The whole point: the page is fingerprinted instead of the tool dying."""
    resp = _response(
        [
            ("set-cookie", "csrftoken=abc; Path=/"),
            ("set-cookie", "craft_session; HttpOnly"),
            ("set-cookie", "Secure; HttpOnly"),
        ]
    )
    monkeypatch.setattr(tech, "http_client", _client_returning(resp))

    result = tech.detect_tech("https://example.test/")

    assert result["ok"] is True
    names = {entry["name"] for entry in result["technologies"]}
    # The well-formed cookie fingerprints normally...
    assert "Django" in names
    # ...and a name-only signature still matches a cookie the site sent valueless.
    assert "Craft CMS" in names
    assert result["malformed_cookies"] == ["Secure", "craft_session"]
    assert any("no name=value pair" in finding for finding in result["findings"])


def test_well_formed_cookies_only_are_unchanged(monkeypatch):
    """No regression: a normal response reports nothing malformed."""
    resp = _response([("set-cookie", "csrftoken=abc; Path=/"), ("set-cookie", "other=2")])
    monkeypatch.setattr(tech, "http_client", _client_returning(resp))

    result = tech.detect_tech("https://example.test/")

    assert result["ok"] is True
    assert tech.response_cookies(resp)[0] == {"csrftoken": "abc", "other": "2"}
    assert result["malformed_cookies"] == []
    assert not any("name=value" in finding for finding in result["findings"])
    assert "Django" in {entry["name"] for entry in result["technologies"]}
