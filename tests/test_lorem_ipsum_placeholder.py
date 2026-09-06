"""LOREM_IPSUM_PLACEHOLDER (#385): matched as the full multi-word passage within the
resolved content area, never a substring match over the whole raw document.

A page that mentions the passage once outside the content area (nav/footer boilerplate) --
or merely says the word "lorem" or "ipsum" alone -- must stay silent.
"""

from __future__ import annotations

from seohead.crawl.collect import collect_urls
from seohead.crawl.evidence import build_evidence
from seohead.sf.config import load_config
from seohead.sf.core.context import AuditContext
from seohead.sf.core.loader import LoadedExports
from seohead.sf.core.rules import run_rules
from seohead.tools.parser import count_lorem_ipsum


def test_full_passage_counts_one_occurrence():
    text = "Some real copy. Lorem ipsum dolor sit amet, consectetur adipiscing elit."
    assert count_lorem_ipsum(text) == 1


def test_two_occurrences_are_both_counted():
    text = "Lorem ipsum dolor sit amet. " * 2
    assert count_lorem_ipsum(text) == 2


def test_incidental_mention_of_the_word_lorem_alone_does_not_match():
    """A product named Lorem, or a page discussing the term, must not trip a phrase match."""
    assert count_lorem_ipsum("Meet Lorem, our new ipsum-themed typography plugin.") == 0


def test_empty_content_counts_zero():
    assert count_lorem_ipsum("") == 0
    assert count_lorem_ipsum("Perfectly ordinary page content with nothing amiss.") == 0


# -- registry check, through the native crawl -> evidence -> rules pipeline --


class _FakeResponse:
    def __init__(self, text: str, headers: dict[str, str]):
        self.text = text
        self.status_code = 200
        self.headers = headers


def _page(body: str) -> str:
    return body + ("Enough body text to be a real page. " * 40)


_PLACEHOLDER_PAGE = f"""<html><head><title>Placeholder</title></head>
<body><main><p>Lorem ipsum dolor sit amet, consectetur adipiscing elit.</p>{_page("")}</main>
</body></html>"""

# The passage appears only in a <footer> -- outside the resolved <main> content area --
# so it must not fire: it is not the page's own content, and the content area is exactly
# what makes that distinction possible.
_FOOTER_ONLY_PAGE = f"""<html><head><title>Footer mention</title></head>
<body><main>{_page("Real page content lives here.")}</main>
<footer>Typography demo: Lorem ipsum dolor sit amet.</footer></body></html>"""


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


def test_lorem_ipsum_placeholder_fires_only_for_the_content_area_page():
    mapping = {
        "https://example.com/placeholder": _FakeResponse(
            _PLACEHOLDER_PAGE, {"content-type": "text/html"}
        ),
        "https://example.com/footer-only": _FakeResponse(
            _FOOTER_ONLY_PAGE, {"content-type": "text/html"}
        ),
    }
    fired = _fired(_run_crawl(mapping))
    assert fired.get("LOREM_IPSUM_PLACEHOLDER", set()) == {"https://example.com/placeholder"}


def test_lorem_ipsum_placeholder_skips_honestly_on_a_plain_sf_export(result):
    skipped = {s.id for s in result.skipped}
    assert "LOREM_IPSUM_PLACEHOLDER" in skipped
    assert "LOREM_IPSUM_PLACEHOLDER" not in {i.check for i in result.issues}
