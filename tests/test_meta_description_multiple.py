"""DESC_MULTIPLE (#385): a native crawl counts every live <meta name="description"> tag,
while the value every existing consumer reads (``meta_description``) stays exactly the
first occurrence, unchanged.
"""

from __future__ import annotations

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules


class _FakeResponse:
    def __init__(self, text: str, headers: dict[str, str]):
        self.text = text
        self.status_code = 200
        self.headers = headers


def _page(body: str) -> str:
    return body + ("Enough body text to be a real page. " * 40)


_ONE_DESC = f"""<html><head><title>One description</title>
<meta name="description" content="A single, ordinary description over seventy characters long.">
</head><body>{_page("")}</body></html>"""

_TWO_DESC = f"""<html><head><title>Two descriptions</title>
<meta name="description" content="The first description, which is what every reader keeps.">
<meta name="description" content="A second, different description nobody asked for.">
</head><body>{_page("")}</body></html>"""


def _fetcher(mapping):
    def fetch(url):
        return mapping[url]

    return fetch


def _run_crawl(mapping):
    crawl_result = collect_urls(list(mapping), fetcher=_fetcher(mapping), sleeper=lambda _s: None)
    evidence = build_evidence(crawl_result)
    exports = LoadedExports()
    exports.frames.update(evidence["frames"])
    exports.found = list(evidence["found"])
    exports.missing = list(evidence["missing"])
    ctx = AuditContext(exports, load_config(None))
    ctx.skip_unsupported(set(exports.frames))
    run_rules(ctx)
    return ctx


def _fired(ctx) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for issue in ctx.issues:
        out.setdefault(issue.check, set()).add(issue.target_url)
    return out


def test_desc_multiple_fires_only_for_the_two_meta_page():
    mapping = {
        "https://example.com/one": _FakeResponse(_ONE_DESC, {"content-type": "text/html"}),
        "https://example.com/two": _FakeResponse(_TWO_DESC, {"content-type": "text/html"}),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("DESC_MULTIPLE", set()) == {"https://example.com/two"}


def test_first_meta_description_value_is_unchanged_by_the_second():
    """The existing first-meta-description value must not change for current consumers
    (explicit acceptance criterion, #385)."""
    crawl_result = collect_urls(
        ["https://example.com/two"],
        fetcher=_fetcher(
            {"https://example.com/two": _FakeResponse(_TWO_DESC, {"content-type": "text/html"})}
        ),
        sleeper=lambda _s: None,
    )
    record = crawl_result.pages[0]
    assert record.meta_description == "The first description, which is what every reader keeps."
    assert record.meta_description_count == 2


def test_desc_missing_is_unaffected_by_desc_multiple(tmp_path):
    """DESC_MISSING must still evaluate normally alongside the new DESC_MULTIPLE guard."""
    no_desc = f"<html><head><title>No description</title></head><body>{_page('')}</body></html>"
    mapping = {"https://example.com/none": _FakeResponse(no_desc, {"content-type": "text/html"})}
    fired = _fired(_run_crawl(mapping))
    assert fired.get("DESC_MISSING", set()) == {"https://example.com/none"}
    assert fired.get("DESC_MULTIPLE", set()) == set()


def test_desc_multiple_skips_honestly_on_a_plain_sf_export(result):
    """``result`` (conftest.py) is a real-shaped SF export with no per-tag count column."""
    skipped = {s.id for s in result.skipped}
    assert "DESC_MULTIPLE" in skipped
    assert "DESC_MULTIPLE" not in {i.check for i in result.issues}
