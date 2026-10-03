"""Offline, occurrence-preserving link context from retained HTML and DOM."""

from __future__ import annotations

import hashlib
import json

import pytest

from seohead.crawl.settings import load
from seohead.crawl.spider import Scope
from seohead.crawl.sqlite_adapter import _document_batch
from seohead.storage import ScanError, open_scan
from seohead.storage.link_context import context_for_link, contexts_for_document
from seohead.storage.native_scan import NativeScan
from seohead.tools.link_context import extract_occurrences
from seohead.tools.parser import parse_html
from tests.test_native_capture import _event, _renderer
from tests.test_scan_native import _metadata, _record, _runtime

ROOT = "https://example.test/"
TARGET = ROOT + "target"


def _batch(html: str, settings, *, parse_cap=20_000):
    parsed = parse_html(
        html,
        ROOT,
        {
            "max_link_observations": parse_cap,
            "classify_links": settings["link_position"]["classify"],
            "link_position_rules": settings["link_position"]["rules"],
            "content_area": settings["evidence"]["content_area"],
        },
    )
    return _document_batch(
        parsed,
        source_url=ROOT,
        depth=0,
        scope=Scope.from_config(settings["scope"]),
        start_host="example.test",
        settings=settings,
    )


def _scan(
    tmp_path,
    html: str,
    *,
    overrides=None,
    retain_body=True,
    capture_changes=None,
    content_type="text/html",
    parse_cap=20_000,
    stored_destination_override=None,
    stored_attributes_override=None,
):
    settings = load(overrides={"speed.min_delay_seconds": 0, **(overrides or {})})
    metadata = _metadata()
    metadata["config"] = settings
    from seohead.crawl.settings import fingerprint

    metadata["config_fingerprint"] = fingerprint(settings)
    path = tmp_path / "scan.sqlite"
    batch = _batch(html, settings, parse_cap=parse_cap)
    if stored_destination_override is not None:
        batch.links[0]["destination"] = stored_destination_override
    if stored_attributes_override is not None:
        batch.links[0].update(stored_attributes_override)
    with NativeScan.create(path, **metadata) as scan:
        scan.enqueue([(ROOT, 0)])
        record = _record(ROOT)
        record["content_type"] = content_type
        scan.commit_page(
            scan.claim(1)[0],
            record,
            links=batch.links,
            captures=[_event(ROOT, body=html.encode(), **(capture_changes or {}))]
            if retain_body
            else (),
            runtime=_runtime(),
            partial_reasons=batch.partial_reasons,
        )
        scan.finish_capture()
    return path


def _link_rows(path):
    with open_scan(path, require_audit=False) as con:
        return [dict(row) for row in con.execute("SELECT * FROM links ORDER BY link_id")]


def test_repeated_target_links_keep_nav_and_heading_context(tmp_path):
    html = (
        "<html><body><nav><a href='/target'>Same</a></nav>"
        "<main><h2>Guide</h2><section><h3>Details</h3>"
        "<a href='/target'>Same</a></section></main></body></html>"
    )
    path = _scan(tmp_path, html)
    rows = _link_rows(path)
    assert len(rows) == 2
    result = contexts_for_document(path, rows[0]["source_document_id"])
    assert result["coverage"]["state"] == "complete"
    assert [item["link_id"] for item in result["items"]] == [row["link_id"] for row in rows]
    assert [item["placement"]["position"] for item in result["items"]] == ["nav", "content"]
    assert result["items"][0]["heading"] is None
    assert result["items"][1]["heading"]["text"] == "Details"
    assert result["items"][1]["heading"]["relation"] == "same_section"
    assert result["items"][0]["heading_relation"] == "not_content_link"
    assert result["items"][0]["scan_uuid"] == result["scan_uuid"]
    assert result["items"][0]["placement"]["basis"] == "matched_selector"
    assert result["items"][1]["placement"]["basis"] == "content_root_inference"
    assert context_for_link(path, rows[1]["link_id"]) == result["items"][1]
    first_page = contexts_for_document(path, rows[0]["source_document_id"], limit=1)
    assert first_page["total"] == 2 and first_page["next_offset"] == 1
    assert contexts_for_document(path, rows[0]["source_document_id"], offset=1, limit=1)[
        "items"
    ] == [result["items"][1]]
    first_size = len(json.dumps(result["items"][0], ensure_ascii=False).encode("utf-8"))
    byte_page = contexts_for_document(
        path, rows[0]["source_document_id"], max_result_bytes=max(1024, first_size + 10)
    )
    assert len(byte_page["items"]) == 1
    assert byte_page["has_more"] is True and byte_page["next_offset"] == 1


def test_store_filter_does_not_shift_occurrence_identity(tmp_path):
    html = (
        "<html><body><main><h2>Known</h2>"
        "<a href='https://outside.test/x'>External</a>"
        "<a href='/target'>Internal</a></main></body></html>"
    )
    path = _scan(tmp_path, html, overrides={"discovery.external.store": False})
    rows = _link_rows(path)
    assert len(rows) == 1
    item = context_for_link(path, rows[0]["link_id"])
    assert item["state"] == "measured"
    assert item["destination_url"] == TARGET
    assert item["heading"]["text"] == "Known"
    assert item["ordinal"] == 0


def test_static_and_rendered_contexts_keep_their_own_documents(tmp_path):
    raw_html = "<body><nav><a href='/target'>Same</a></nav></body>"
    rendered_html = "<body><main><h2>Rendered heading</h2><a href='/target'>Same</a></main></body>"
    settings = load(overrides={"speed.min_delay_seconds": 0})
    metadata = _metadata()
    metadata["config"] = settings
    from seohead.crawl.settings import fingerprint

    metadata["config_fingerprint"] = fingerprint(settings)
    path = tmp_path / "rendered.sqlite"
    with NativeScan.create(path, **metadata) as scan:
        scan.enqueue([(ROOT, 0)])
        scan.commit_page(
            scan.claim(1)[0],
            _record(ROOT),
            links=_batch(raw_html, settings).links,
            captures=[_event(ROOT, body=raw_html.encode())],
            runtime=_runtime(),
        )
        record = _record(ROOT)
        record["representation"] = "rendered"
        # The current page projection can change; raw context uses its own response MIME.
        record["content_type"] = "application/pdf"
        scan.commit_render(
            ROOT,
            record,
            html=rendered_html,
            renderer=_renderer(ROOT),
            captured_at="2026-10-03T00:00:00Z",
            links=_batch(rendered_html, settings).links,
        )
        scan.finish_capture()
    raw, rendered = _link_rows(path)
    raw_context = context_for_link(path, raw["link_id"])
    rendered_context = context_for_link(path, rendered["link_id"])
    assert raw_context["representation"] == "static"
    assert rendered_context["representation"] == "rendered"
    assert raw_context["source_document_id"] != rendered_context["source_document_id"]
    assert raw_context["placement"]["position"] == "nav"
    assert rendered_context["placement"]["position"] == "content"
    assert rendered_context["heading"]["text"] == "Rendered heading"


def test_missing_body_and_byte_budget_are_explicit(tmp_path):
    html = "<html><body><main><a href='/target'>Target</a></main></body></html>"
    absent = _scan(tmp_path, html, retain_body=False)
    link_id = _link_rows(absent)[0]["link_id"]
    assert context_for_link(absent, link_id)["state"] == "unavailable"
    assert context_for_link(absent, link_id)["reason"] == "source_document_unavailable"

    retained = _scan(tmp_path / "retained", html)
    link_id = _link_rows(retained)[0]["link_id"]
    limited = context_for_link(retained, link_id, max_body_bytes=10)
    assert limited["state"] == "unavailable"
    assert "decoded byte limit" in limited["reason"]
    disabled = _scan(tmp_path / "body-off", html, overrides={"storage.body_mode": "off"})
    item = context_for_link(disabled, _link_rows(disabled)[0]["link_id"])
    assert item["state"] == "unavailable"
    assert item["reason"] == "body_omitted:not_enabled"


def test_zero_link_document_keeps_representation_and_source_identity(tmp_path):
    html = "<body><main><h2>No links</h2></main></body>"
    path = _scan(tmp_path, html)
    with open_scan(path, require_audit=False) as con:
        document_id = con.execute("SELECT document_id FROM documents").fetchone()[0]
    result = contexts_for_document(path, document_id)
    assert result["total"] == 0 and result["items"] == []
    assert result["representation"] == "static"
    assert result["source_url"] == ROOT
    assert result["source_url_id"] > 0
    assert result["source_document_id"] == document_id
    assert result["coverage"]["state"] == "complete"

    unavailable = _scan(tmp_path / "off", html, overrides={"storage.body_mode": "off"})
    with open_scan(unavailable, require_audit=False) as con:
        document_id = con.execute("SELECT document_id FROM documents").fetchone()[0]
    result = contexts_for_document(unavailable, document_id)
    assert result["representation"] == "static"
    assert result["source_document_id"] == document_id
    assert result["coverage"]["state"] == "unavailable"


def test_base_href_replays_the_exact_stored_occurrence(tmp_path):
    html = (
        "<html><head><base href='https://example.test/sub/'></head>"
        "<body><main><h2>Guide</h2><a href='target'>Read</a></main></body></html>"
    )
    path = _scan(tmp_path, html)
    item = context_for_link(path, _link_rows(path)[0]["link_id"])
    assert item["state"] == "measured"
    assert item["destination_url"] == "https://example.test/sub/target"
    assert item["heading"]["text"] == "Guide"


def test_truncated_and_non_html_bodies_are_unavailable(tmp_path):
    html = "<body><a href='/target'>Target</a></body>"
    truncated = _scan(
        tmp_path,
        html,
        capture_changes={"body_state": "truncated", "body_reason": "truncated"},
    )
    item = context_for_link(truncated, _link_rows(truncated)[0]["link_id"])
    assert item["state"] == "unavailable"
    assert item["reason"] == "body_truncated:truncated"
    non_html = _scan(
        tmp_path / "non-html",
        html,
        content_type="application/pdf",
        capture_changes={"content_type": "application/pdf"},
    )
    item = context_for_link(non_html, _link_rows(non_html)[0]["link_id"])
    assert item["state"] == "unavailable"
    assert item["reason"] == "body_omitted:unsupported_media"


@pytest.mark.parametrize("media_type", ("text/nothtml", "application/xhtml+xml"))
def test_complete_non_html_mime_is_not_parsed_as_html(tmp_path, media_type):
    html = "<body><a href='/target'>Target</a></body>"
    path = _scan(
        tmp_path,
        html,
        content_type=media_type,
        capture_changes={"content_type": media_type},
    )
    item = context_for_link(path, _link_rows(path)[0]["link_id"])
    assert item["state"] == "unavailable"
    assert item["reason"] == f"unsupported_source_mime:{media_type}"


def test_parser_omission_remains_partial_after_context_replay(tmp_path, monkeypatch):
    html = (
        "<body><main>"
        + "".join(f"<a href='/target?i={i}'>A{i}</a>" for i in range(3))
        + "</main></body>"
    )
    path = _scan(tmp_path, html, parse_cap=2)
    from seohead.storage import link_context

    monkeypatch.setattr(link_context, "MAX_ANCHORS", 2)
    document_id = _link_rows(path)[0]["source_document_id"]
    result = contexts_for_document(path, document_id)
    assert result["coverage"] == {
        "state": "partial",
        "reason": "eligible anchor observations exceeded parser cap",
        "eligible_total": 3,
        "eligible_omitted": 1,
    }
    assert result["total"] == 2
    assert (
        context_for_link(path, _link_rows(path)[0]["link_id"])["document_coverage"]["state"]
        == "partial"
    )


def test_extractor_heading_precedence_and_cap():
    html = (
        "<body><main><h2>First</h2><a href='/a'>A</a>"
        "<section><a href='/b'>B</a><h3>Section</h3><a href='/c'>C</a></section>"
        "<footer><a href='/d'>D</a></footer></main></body>"
    )
    result = extract_occurrences(html, ROOT, cap=2)
    assert result["eligible_total"] == 4
    assert result["eligible_omitted"] == 2
    assert [item["heading"]["text"] for item in result["occurrences"]] == ["First", "First"]
    full = extract_occurrences(html, ROOT)
    assert [item["heading_relation"] for item in full["occurrences"]] == [
        "preceding_content",
        "preceding_content",
        "same_section",
        "not_content_link",
    ]
    assert full["occurrences"][2]["heading"]["text"] == "Section"
    assert full["occurrences"][3]["heading"] is None


def test_custom_selector_and_default_body_are_explained():
    custom = extract_occurrences(
        "<body><main><h2>Heading</h2><div class='promo'><a href='/x'>X</a></div></main></body>",
        ROOT,
        position_rules=[{"position": "nav", "selector": ".promo"}],
    )["occurrences"][0]
    assert custom["position"] == "nav"
    assert custom["matched_selector"] == ".promo"
    assert custom["heading"] is None
    fallback = extract_occurrences("<body><h2>Body heading</h2><a href='/x'>X</a></body>", ROOT)[
        "occurrences"
    ][0]
    assert fallback["content_root_strategy"] == "default_body"
    assert fallback["position"] == "content"
    assert fallback["heading"]["text"] == "Body heading"


def test_repeated_read_does_not_change_scan_and_invalid_id_is_named(tmp_path):
    html = "<body><main><h2>Known</h2><a href='/target'>Target</a></main></body>"
    path = _scan(tmp_path, html)
    link_id = _link_rows(path)[0]["link_id"]
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert context_for_link(path, link_id) == context_for_link(path, link_id)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with pytest.raises(ScanError, match="not present"):
        context_for_link(path, 999_999)


def test_replay_mismatch_refuses_to_guess_an_occurrence(tmp_path):
    html = "<body><a href='/target'>Target</a></body>"
    path = _scan(tmp_path, html, stored_destination_override=ROOT + "other")
    item = context_for_link(path, _link_rows(path)[0]["link_id"])
    assert item["state"] == "unavailable"
    assert item["reason"] == "stored_links_do_not_replay_from_retained_document"


def test_captured_target_and_rel_must_replay_exactly(tmp_path):
    html = "<body><a href='/target' target='_blank' rel='noopener'>Target</a></body>"
    correct = _scan(
        tmp_path,
        html,
        overrides={"link_attributes.capture": True},
    )
    assert context_for_link(correct, _link_rows(correct)[0]["link_id"])["state"] == "measured"
    changed = _scan(
        tmp_path / "changed",
        html,
        overrides={"link_attributes.capture": True},
        stored_attributes_override={"target": "_self", "rel": ("ugc",)},
    )
    item = context_for_link(changed, _link_rows(changed)[0]["link_id"])
    assert item["state"] == "unavailable"
    assert item["reason"] == "stored_links_do_not_replay_from_retained_document"


def test_captured_position_must_replay_even_when_stored_value_is_blank(tmp_path):
    html = "<body><nav><a href='/target'>Target</a></nav></body>"
    path = _scan(
        tmp_path,
        html,
        overrides={"link_position.classify": True},
        stored_attributes_override={"position": ""},
    )
    item = context_for_link(path, _link_rows(path)[0]["link_id"])
    assert item["state"] == "unavailable"
    assert item["reason"] == "stored_links_do_not_replay_from_retained_document"


def test_many_siblings_keep_bounded_deterministic_dom_paths():
    html = (
        "<body><main>"
        + "".join(f"<a href='/p{i}'>P{i}</a>" for i in range(1000))
        + "</main></body>"
    )
    result = extract_occurrences(html, ROOT)
    assert result["eligible_total"] == 1000
    assert result["occurrences"][-1]["dom_path"].endswith("a:nth-of-type(1000)")
