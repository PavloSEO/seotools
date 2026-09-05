"""Network-free tests for URL variant consolidation in ``recon.mirrors``."""

from seohead.recon import mirrors as M


def _r(variant, group, url, *, status=200, hops=None, final=None, reachable=True, error=None):
    return {
        "variant": variant,
        "group": group,
        "url": url,
        "reachable": reachable,
        "error": error,
        "status": status,
        "hops": hops or [],
        "final_url": final or url,
        "redirects": len(hops or []),
    }


def hop(u, loc, status=301):
    return {"url": u, "status": status, "location": loc}


# ------------------------------------------------------------ variants


def test_variants_for_root():
    v = M.build_variants("https://example.com/")
    urls = {x["url"] for x in v}
    assert "https://example.com/" in urls and "http://www.example.com/" in urls
    assert "https://example.com/index.php" in urls and "https://example.com/index.html" in urls
    assert len([x for x in v if x["group"] == "origin"]) == 4
    # A root URL has no trailing-slash or case variants.
    assert not [x for x in v if x["group"] in ("path", "case")]


def test_variants_for_path_add_slash_and_case():
    v = M.build_variants("https://example.com/services/implant/")
    groups = {x["group"] for x in v}
    assert {"origin", "index", "path", "case"} <= groups
    paths = {x["url"] for x in v if x["group"] == "path"}
    assert paths == {
        "https://example.com/services/implant",
        "https://example.com/services/implant/",
    }
    (case,) = [x for x in v if x["group"] == "case"]
    assert case["url"] == "https://example.com/SERVICES/IMPLANT"


def test_variants_strip_www_input():
    v = M.build_variants("https://www.example.com/")
    assert v[0]["url"] == "https://example.com/"


# ------------------------------------------------------------ analysis

WWW_OK = {"resolvable": True, "a": ["1.2.3.4"], "cname": [], "source": "doh"}


def _healthy_results():
    return [
        _r("https://example.com/", "origin", "https://example.com/"),
        _r(
            "http://example.com/",
            "origin",
            "http://example.com/",
            hops=[hop("http://example.com/", "https://example.com/")],
            final="https://example.com/",
        ),
        _r(
            "https://www.example.com/",
            "origin",
            "https://www.example.com/",
            hops=[hop("https://www.example.com/", "https://example.com/")],
            final="https://example.com/",
        ),
        _r(
            "http://www.example.com/",
            "origin",
            "http://www.example.com/",
            hops=[hop("http://www.example.com/", "https://example.com/")],
            final="https://example.com/",
        ),
        _r(
            "/index.php",
            "index",
            "https://example.com/index.php",
            hops=[hop("https://example.com/index.php", "https://example.com/")],
            final="https://example.com/",
        ),
    ]


def test_healthy_site_is_consolidated():
    out = M.analyze("https://example.com/", _healthy_results(), WWW_OK)
    assert out["consolidated"] is True
    assert out["canonical_origin"] == "https://example.com/"
    assert out["duplicates_200"] == [] and out["downgrade_redirects"] == []
    assert out["findings"] == ["all checked variants consolidate correctly"]


def test_live_www_duplicate_detected():
    rs = _healthy_results()
    # The www variant returns a standalone 200 instead of redirecting.
    rs[2] = _r("https://www.example.com/", "origin", "https://www.example.com/")
    out = M.analyze("https://example.com/", rs, WWW_OK)
    assert out["consolidated"] is False
    assert "https://www.example.com/" in out["duplicates_200"]


def test_index_php_200_is_duplicate():
    rs = _healthy_results()
    rs[4] = _r("/index.php", "index", "https://example.com/index.php")
    out = M.analyze("https://example.com/", rs, WWW_OK)
    assert "/index.php" in out["duplicates_200"]


def test_uppercase_200_is_duplicate():
    rs = [*_healthy_results(), _r("/SERVICES", "case", "https://example.com/SERVICES")]
    out = M.analyze("https://example.com/services/", rs, WWW_OK)
    assert "/SERVICES" in out["duplicates_200"]


def test_slash_pair_both_200_is_duplicate():
    rs = [
        *_healthy_results(),
        _r("/services", "path", "https://example.com/services"),
        _r("/services/", "path", "https://example.com/services/"),
    ]
    out = M.analyze("https://example.com/services/", rs, WWW_OK)
    assert any("trailing-slash duplicate" in f for f in out["findings"])


def test_slash_pair_redirect_is_fine():
    rs = [
        *_healthy_results(),
        _r(
            "/services",
            "path",
            "https://example.com/services",
            hops=[hop("https://example.com/services", "https://example.com/services/")],
            final="https://example.com/services/",
        ),
        _r("/services/", "path", "https://example.com/services/"),
    ]
    out = M.analyze("https://example.com/services/", rs, WWW_OK)
    assert not any("trailing-slash duplicate" in f for f in out["findings"])


def test_downgrade_and_long_chain_reported():
    rs = _healthy_results()
    rs[4] = _r(
        "/index.php",
        "index",
        "https://example.com/index.php",
        hops=[
            hop("https://example.com/index.php", "http://example.com/"),
            hop("http://example.com/", "https://example.com/"),
        ],
        final="https://example.com/",
    )
    out = M.analyze("https://example.com/", rs, WWW_OK)
    assert out["downgrade_redirects"] and out["long_chains"]
    assert any("downgrade" in f for f in out["findings"])


def test_unresolvable_www_is_its_own_diagnosis():
    rs = _healthy_results()[:2]
    out = M.analyze(
        "https://example.com/",
        [
            *rs,
            _r(
                "https://www.example.com/",
                "origin",
                "https://www.example.com/",
                reachable=False,
                error="dns",
                status=None,
                final=None,
            ),
        ],
        {"resolvable": False, "a": [], "cname": [], "source": "doh"},
    )
    assert any("does not resolve" in f for f in out["findings"])
    assert any(d["variant"] == "https://www.example.com/" for d in out["unreachable"])


def test_www_primary_convergence_has_no_duplicate():
    # A valid www-primary site: every origin variant redirects to
    # https://www.example.com/, which itself answers 200 directly.
    canonical = "https://www.example.com/"

    def redirect_row(variant, url):
        return _r(
            variant,
            "origin",
            url,
            hops=[hop(url, canonical)],
            final=canonical,
        )

    rs = [
        redirect_row("https://example.com/", "https://example.com/"),
        redirect_row("http://example.com/", "http://example.com/"),
        _r(canonical, "origin", canonical),
        redirect_row("http://www.example.com/", "http://www.example.com/"),
    ]
    out = M.analyze(canonical, rs, WWW_OK)
    assert out["consolidated"] is True
    assert out["canonical_origin"] == canonical
    assert out["duplicates_200"] == []


def test_bare_primary_convergence_has_no_duplicate():
    # Bare-host canonical must stay clean too: the fix must not merely flip
    # the hard-coded host, it must follow whatever the evidence converges on.
    out = M.analyze("https://example.com/", _healthy_results(), WWW_OK)
    assert out["consolidated"] is True
    assert out["canonical_origin"] == "https://example.com/"
    assert out["duplicates_200"] == []


def test_two_independent_direct_200_origins_stay_duplicates():
    # Origins do not converge at all: two independently live hosts, neither
    # matching the bare-https fallback used when there is no single winner.
    rs = [
        _r(
            "https://example.com/",
            "origin",
            "https://example.com/",
            hops=[hop("https://example.com/", "http://example.com/")],
            final="http://example.com/",
        ),
        _r("http://example.com/", "origin", "http://example.com/"),
        _r("https://www.example.com/", "origin", "https://www.example.com/"),
        _r(
            "http://www.example.com/",
            "origin",
            "http://www.example.com/",
            hops=[hop("http://www.example.com/", "http://example.com/")],
            final="http://example.com/",
        ),
    ]
    out = M.analyze("https://example.com/", rs, WWW_OK)
    assert out["consolidated"] is False
    assert {"http://example.com/", "https://www.example.com/"} <= set(out["duplicates_200"])


def test_loop_suspect_reported():
    rs = _healthy_results()
    rs[0] = {
        "variant": "https://example.com/",
        "group": "origin",
        "url": "https://example.com/",
        "reachable": True,
        "status": None,
        "final_url": "https://example.com/x",
        "hops": [hop("https://example.com/", "https://example.com/x")] * 6,
        "redirects": 6,
        "loop_suspect": True,
    }
    out = M.analyze("https://example.com/", rs, WWW_OK)
    assert any("loop" in f for f in out["findings"])
