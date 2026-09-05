"""#360: a page whose copy sits in an iframe is not thin, and must not be told it is.

An iframe's document is not part of the parent DOM, so ``word_count`` measures
the shell around the content. Reported as ``THIN_CONTENT``, that names the wrong
cause and asks the operator to write copy they have already written. The four
facts worth pinning: the frames are recorded, where they sit is recorded, a
same-origin frame in the content area changes the finding, and a third-party
embed does not.
"""

from __future__ import annotations

import csv

from seohead.sf.core.audit import run_audit
from seohead.tools.parser import parse_html
from tests.conftest import issues_of

_URL = "https://example.com/framed"

_COLS = [
    "Address",
    "Content Type",
    "Status Code",
    "Indexability",
    "Title 1",
    "Meta Description 1",
    "H1-1",
    "Word Count",
    "Content Frames",
    "Content Frames Same-Origin",
]


def _audit(tmp_path, *, word_count, frames, same_origin_frames, cols=None):
    exports = tmp_path / "exports"
    exports.mkdir()
    with open(exports / "internal_all.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(cols or _COLS)
        row = [
            _URL,
            "text/html; charset=UTF-8",
            "200",
            "Indexable",
            "A title long enough to pass the length rule for this page",
            "A meta description long enough to pass the length rule that applies here too.",
            "Heading",
            str(word_count),
            str(frames),
            str(same_origin_frames),
        ]
        writer.writerow(row[: len(cols or _COLS)])
    return run_audit(input_mode="parse-exports", exports_dir=str(exports), log=lambda m: None)


# ── the parser records what a page frames ────────────────────────────────────


def test_a_frame_is_recorded_with_where_it_sits_and_who_serves_it():
    parsed = parse_html(
        "<html><body>"
        '<header><iframe src="/chat-widget"></iframe></header>'
        '<main><p>tiny</p><iframe src="/the-actual-copy.html" title="Copy"></iframe>'
        '<iframe src="https://www.youtube.com/embed/x" loading="lazy"></iframe></main>'
        "</body></html>",
        "https://example.com/page",
    )
    frames = parsed["frames"]
    assert [f["raw_src"] for f in frames] == [
        "/chat-widget",
        "/the-actual-copy.html",
        "https://www.youtube.com/embed/x",
    ]
    widget, copy, embed = frames
    # A frame in the header is a widget; the content area is what matters.
    assert widget["in_content_area"] is False
    assert copy["in_content_area"] is True and copy["same_origin"] is True
    assert copy["src"] == "https://example.com/the-actual-copy.html"
    assert copy["title"] == "Copy"
    assert embed["in_content_area"] is True and embed["same_origin"] is False
    assert embed["loading"] == "lazy"


def test_a_template_only_frame_is_not_a_frame():
    """A <template>'s contents are inert per the HTML spec: never instantiated,
    never displayed, and so never the reason a page looks thin."""
    parsed = parse_html(
        '<html><body><main><template><iframe src="/never"></iframe></template></main></body></html>',
        "https://example.com/page",
    )
    assert parsed["frames"] == []


def test_a_javascript_populated_frame_still_counts():
    """No src is not the absence of a frame -- it hides text from a parser
    exactly as a src'd one does, and it is same-origin by definition."""
    parsed = parse_html(
        "<html><body><main><iframe></iframe></main></body></html>",
        "https://example.com/page",
    )
    assert len(parsed["frames"]) == 1
    assert parsed["frames"][0]["src"] == ""
    assert parsed["frames"][0]["same_origin"] is True
    assert parsed["frames"][0]["in_content_area"] is True


def test_the_marker_used_to_locate_frames_does_not_survive_into_the_output():
    parsed = parse_html(
        '<html><body><main><iframe src="/x"></iframe></main></body></html>',
        "https://example.com/page",
    )
    assert "seohead-frame" not in parsed["text"]
    assert "seohead-frame" not in parsed["content_text"]


# ── the finding says the true thing ──────────────────────────────────────────


def test_a_thin_page_framing_its_own_content_is_not_reported_as_thin(tmp_path):
    result = _audit(tmp_path, word_count=8, frames=1, same_origin_frames=1)
    assert not issues_of(result, "THIN_CONTENT")
    framed = issues_of(result, "CONTENT_IN_IFRAME")
    assert [i.target_url for i in framed] == [_URL]
    assert framed[0].details["word_count"] == 8
    assert framed[0].details["same_origin_frames_in_content_area"] == 1


def test_a_thin_page_framing_only_a_third_party_embed_is_still_thin(tmp_path):
    """An embedded video or map is normal. A check that fired on every YouTube
    embed would be ignored, and then it would be worth nothing on the case it
    was written for."""
    result = _audit(tmp_path, word_count=8, frames=1, same_origin_frames=0)
    assert [i.target_url for i in issues_of(result, "THIN_CONTENT")] == [_URL]
    assert not issues_of(result, "CONTENT_IN_IFRAME")


def test_a_substantial_page_that_also_frames_something_is_neither(tmp_path):
    result = _audit(tmp_path, word_count=900, frames=1, same_origin_frames=1)
    assert not issues_of(result, "THIN_CONTENT")
    assert not issues_of(result, "CONTENT_IN_IFRAME")


def test_evidence_without_a_frame_inventory_says_so_instead_of_running_clean(tmp_path):
    """A Screaming Frog export carries no iframe columns. The absence must read
    as "nobody looked", not as "nothing framed" -- otherwise a THIN_CONTENT
    finding stands beside a CONTENT_IN_IFRAME that silently found nothing, and
    a reader cannot tell that the framing explanation was never tested."""
    result = _audit(
        tmp_path,
        word_count=8,
        frames=0,
        same_origin_frames=0,
        cols=_COLS[:-2],
    )
    assert [i.target_url for i in issues_of(result, "THIN_CONTENT")] == [_URL]
    assert not issues_of(result, "CONTENT_IN_IFRAME")
    assert "CONTENT_IN_IFRAME" in {s.id for s in result.skipped}


def test_a_native_crawls_frame_inventory_is_not_reported_as_missing(tmp_path):
    """The columns are present and every page frames nothing: that is a real
    clean result, and it must not be filed as an unavailable check."""
    result = _audit(tmp_path, word_count=900, frames=0, same_origin_frames=0)
    assert "CONTENT_IN_IFRAME" not in {s.id for s in result.skipped}
