"""Yandex Webmaster API v4 adapter: user id discovery, query strings and paging."""

import json
import urllib.error
import urllib.parse

import pytest

from seohead.data_sources import yandex_webmaster as wm
from seohead.data_sources.providers import provider_collect

# Operation -> (documented list key, documented page-size cap).
PAGED_OPS = [
    ("search_performance", "queries", 500),
    ("crawl", "samples", 100),
    ("indexing", "samples", 100),
    ("broken_links_samples", "links", 100),
]


def _transport(pages):
    calls = []

    def send(method, url, payload, token):
        calls.append(url)
        if url.endswith("/user"):
            return json.dumps({"user_id": 42})
        return json.dumps(pages(url))

    return send, calls


def _offsets(calls):
    return [int(url.split("offset=")[1].split("&")[0]) for url in calls]


def _evidence(operation, request, send):
    envelope = provider_collect(
        "yandex_webmaster", operation, {"token": "t", **request}, transport=send
    )
    return envelope["evidence"]


def test_user_id_is_resolved_and_list_params_repeat():
    send, calls = _transport(lambda url: {"indicators": {}})
    result = wm.collect(
        "search_history",
        host_id="https:example.com:443",
        params={"query_indicator": ["TOTAL_SHOWS", "TOTAL_CLICKS"], "date_from": "2025-01-01"},
        token="t",
        transport=send,
    )
    assert result["ok"] and calls[0].endswith("/user")
    assert "/user/42/hosts/https:example.com:443/search-queries/all/history?" in calls[1]
    assert "query_indicator=TOTAL_SHOWS&query_indicator=TOTAL_CLICKS" in calls[1]


def test_search_queries_are_paged_until_a_short_page():
    def pages(url):
        offset = int(url.split("offset=")[1].split("&")[0])
        size = 500 if offset == 0 else 7
        return {"queries": [{"query_id": str(offset + i)} for i in range(size)], "count": 507}

    send, calls = _transport(pages)
    result = wm.collect(
        "search_performance", user_id="1", host_id="h", paginate=True, token="t", transport=send
    )
    assert result["returned"] == 507 and result["truncated"] is False
    assert "order_by=TOTAL_SHOWS" in calls[0] and len(calls) == 2


def test_paging_stops_at_the_row_ceiling():
    send, _ = _transport(lambda url: {"queries": [{}] * 500, "count": 10_000})
    result = wm.collect(
        "search_performance",
        user_id="1",
        host_id="h",
        paginate=True,
        max_rows=600,
        token="t",
        transport=send,
    )
    assert result["returned"] == 600 and result["truncated"] is True
    assert result["state"] == "partial"


def test_indexing_reads_the_real_v4_samples_endpoint():
    send, calls = _transport(lambda url: {"samples": [], "count": 0})
    result = wm.collect("indexing", user_id="1", host_id="h", token="t", transport=send)
    assert result["ok"] and "/hosts/h/indexing/samples" in calls[0]


def test_indexing_samples_page_at_the_documented_cap():
    def pages(url):
        offset = int(url.split("offset=")[1].split("&")[0])
        size = 100 if offset < 200 else 30
        return {
            "samples": [{"url": f"https://example.com/{offset + i}"} for i in range(size)],
            "count": 230,
        }

    send, calls = _transport(pages)
    result = wm.collect(
        "indexing", user_id="1", host_id="h", paginate=True, token="t", transport=send
    )
    assert result["returned"] == 230 and result["truncated"] is False
    assert "limit=100" in calls[0] and len(calls) == 3


def test_broken_links_samples_collect_the_links_list():
    def pages(url):
        offset = int(url.split("offset=")[1].split("&")[0])
        size = 100 if offset == 0 else 5
        return {
            "links": [
                {"destination_url": f"https://example.com/gone/{offset + i}"} for i in range(size)
            ],
            "count": 105,
        }

    send, calls = _transport(pages)
    result = wm.collect(
        "broken_links_samples", user_id="1", host_id="h", paginate=True, token="t", transport=send
    )
    assert result["ok"] and "/hosts/h/links/internal/broken/samples" in calls[0]
    assert result["returned"] == 105 and len(result["data"]["links"]) == 105


def test_host_operation_without_host_is_refused():
    try:
        wm.collect("summary", user_id="1", token="t", transport=lambda *a: "{}")
    except ValueError as exc:
        assert "host_id" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_query_history_puts_the_query_id_in_the_path():
    send, calls = _transport(lambda url: {"indicators": {}})
    wm.collect("query_history", user_id="1", host_id="h", query_id="a/b", token="t", transport=send)
    assert calls[0].endswith("/user/1/hosts/h/search-queries/a%2Fb/history")


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_an_empty_list_with_zero_count_completes(operation, list_key, cap):
    send, calls = _transport(lambda url: {list_key: [], "count": 0})
    result = wm.collect(
        operation, user_id="1", host_id="h", paginate=True, token="t", transport=send
    )
    assert result["ok"] and result["state"] == "complete"
    assert result["returned"] == 0 and result["truncated"] is False
    assert len(calls) == 1 and f"limit={cap}" in calls[0]


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_a_full_page_at_the_ceiling_completes_when_count_is_covered(operation, list_key, cap):
    send, calls = _transport(lambda url: {list_key: [{}] * cap, "count": cap})
    result = wm.collect(
        operation,
        user_id="1",
        host_id="h",
        paginate=True,
        max_rows=cap,
        token="t",
        transport=send,
    )
    assert result["state"] == "complete" and result["truncated"] is False
    assert result["returned"] == cap and len(calls) == 1


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_a_ceiling_below_count_reports_partial_in_result_and_evidence(operation, list_key, cap):
    send, _ = _transport(lambda url: {list_key: [{}] * cap, "count": cap + 1})
    result = wm.collect(
        operation,
        user_id="1",
        host_id="h",
        paginate=True,
        max_rows=cap,
        token="t",
        transport=send,
    )
    assert result["state"] == "partial"
    assert result["returned"] == cap and result["truncated"] is True

    evidence = _evidence(
        operation,
        {"user_id": "1", "host_id": "h", "paginate": True, "max_rows": cap},
        send,
    )
    assert evidence["pagination"] == {"returned": cap, "truncated": True}
    assert evidence["status"] == "partial" and evidence["complete"] is False


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_paging_continues_until_count_is_covered(operation, list_key, cap):
    def pages(url):
        offset = int(url.split("offset=")[1].split("&")[0])
        return {list_key: [{}] * (cap if offset == 0 else 1), "count": cap + 1}

    send, calls = _transport(pages)
    result = wm.collect(
        operation,
        user_id="1",
        host_id="h",
        paginate=True,
        max_rows=cap + 1,
        token="t",
        transport=send,
    )
    assert result["state"] == "complete" and result["truncated"] is False
    assert result["returned"] == cap + 1 and _offsets(calls) == [0, cap]


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_a_single_page_reports_the_unread_remainder(operation, list_key, cap):
    send, _ = _transport(lambda url: {list_key: [{}] * 10, "count": cap + 1})
    result = wm.collect(operation, user_id="1", host_id="h", token="t", transport=send)
    assert result["state"] == "partial" and result["truncated"] is True
    assert result["returned"] == 10

    evidence = _evidence(operation, {"user_id": "1", "host_id": "h"}, send)
    assert evidence["pagination"] == {"returned": 10, "truncated": True}
    assert evidence["status"] == "partial" and evidence["complete"] is False


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
@pytest.mark.parametrize("paginate", [True, False])
def test_a_page_without_count_fails(operation, list_key, cap, paginate):
    send, _ = _transport(lambda url: {list_key: [{}]})
    result = wm.collect(
        operation, user_id="1", host_id="h", paginate=paginate, token="t", transport=send
    )
    assert result["ok"] is False and result["state"] == "failed"


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
@pytest.mark.parametrize("paginate", [True, False])
@pytest.mark.parametrize("body", [{"count": 5}, {"unexpected": [], "count": 5}])
def test_a_page_without_the_list_fails(operation, list_key, cap, paginate, body):
    send, _ = _transport(lambda url: body)
    result = wm.collect(
        operation, user_id="1", host_id="h", paginate=paginate, token="t", transport=send
    )
    assert result["ok"] is False and result["state"] == "failed"


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
@pytest.mark.parametrize("bad_list", [{"a": 1, "b": 2}, "urls"])
def test_a_non_list_page_fails_instead_of_counting_keys(operation, list_key, cap, bad_list):
    send, _ = _transport(lambda url: {list_key: bad_list, "count": 2})
    result = wm.collect(
        operation, user_id="1", host_id="h", paginate=True, token="t", transport=send
    )
    assert result["ok"] is False and result["state"] == "failed"


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_an_empty_page_with_pending_count_is_partial(operation, list_key, cap):
    send, _ = _transport(lambda url: {list_key: [], "count": 5})
    result = wm.collect(
        operation, user_id="1", host_id="h", paginate=True, token="t", transport=send
    )
    assert result["ok"] is True and result["state"] == "partial"
    assert result["returned"] == 0 and result["truncated"] is True

    evidence = _evidence(operation, {"user_id": "1", "host_id": "h", "paginate": True}, send)
    assert evidence["pagination"]["truncated"] is True
    assert evidence["complete"] is False


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_a_decimal_string_count_is_parsed(operation, list_key, cap):
    send, calls = _transport(lambda url: {list_key: [{}] * cap, "count": str(cap)})
    result = wm.collect(
        operation, user_id="1", host_id="h", paginate=True, token="t", transport=send
    )
    assert result["ok"] and result["state"] == "complete"
    assert result["returned"] == cap and result["truncated"] is False
    assert len(calls) == 1


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_a_final_page_past_the_ceiling_stays_partial(operation, list_key, cap):
    def pages(url):
        params = dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))
        offset = int(params["offset"])
        size = cap if offset == 0 else min(int(params["limit"]), 50)
        return {list_key: [{}] * size, "count": cap + 50}

    send, calls = _transport(pages)
    result = wm.collect(
        operation,
        user_id="1",
        host_id="h",
        paginate=True,
        max_rows=cap + 20,
        token="t",
        transport=send,
    )
    assert result["state"] == "partial" and result["truncated"] is True
    assert result["returned"] == cap + 20
    assert "limit=20" in calls[1]

    evidence = _evidence(
        operation,
        {"user_id": "1", "host_id": "h", "paginate": True, "max_rows": cap + 20},
        send,
    )
    assert evidence["pagination"] == {"returned": cap + 20, "truncated": True}
    assert evidence["status"] == "partial" and evidence["complete"] is False


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_an_over_returning_page_still_reports_the_ceiling_cut(operation, list_key, cap):
    def pages(url):
        offset = int(url.split("offset=")[1].split("&")[0])
        return {list_key: [{}] * (cap if offset == 0 else 50), "count": cap + 50}

    send, _ = _transport(pages)
    result = wm.collect(
        operation,
        user_id="1",
        host_id="h",
        paginate=True,
        max_rows=cap + 20,
        token="t",
        transport=send,
    )
    assert result["state"] == "partial" and result["truncated"] is True
    assert result["returned"] == cap + 20


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
@pytest.mark.parametrize("paginate", [True, False])
def test_a_count_below_the_returned_rows_fails(operation, list_key, cap, paginate):
    send, _ = _transport(lambda url: {list_key: [{}], "count": 0})
    result = wm.collect(
        operation, user_id="1", host_id="h", paginate=paginate, token="t", transport=send
    )
    assert result["ok"] is False and result["state"] == "failed"

    evidence = _evidence(operation, {"user_id": "1", "host_id": "h", "paginate": paginate}, send)
    assert evidence["status"] == "failed" and evidence["complete"] is False


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_a_later_page_contradicting_count_fails(operation, list_key, cap):
    def pages(url):
        offset = int(url.split("offset=")[1].split("&")[0])
        if offset == 0:
            return {list_key: [{}] * cap, "count": cap + 1}
        return {list_key: [{}] * 5, "count": cap + 1}

    send, _ = _transport(pages)
    result = wm.collect(
        operation, user_id="1", host_id="h", paginate=True, token="t", transport=send
    )
    assert result["ok"] is False and result["state"] == "failed"


def test_paginate_requires_a_positive_max_rows():
    with pytest.raises(ValueError, match="max_rows"):
        wm.collect(
            "indexing",
            user_id="1",
            host_id="h",
            paginate=True,
            max_rows=0,
            token="t",
            transport=lambda *a: "{}",
        )


@pytest.mark.parametrize(("operation", "list_key", "cap"), PAGED_OPS)
def test_a_mid_paging_failure_is_never_complete(operation, list_key, cap):
    def send(method, url, payload, token):
        if "offset=0" in url:
            return json.dumps({list_key: [{}] * cap, "count": cap + 1})
        raise urllib.error.HTTPError(url, 503, "unavailable", {}, None)

    result = wm.collect(
        operation, user_id="1", host_id="h", paginate=True, token="t", transport=send
    )
    assert result["ok"] is False and result["state"] == "failed"

    evidence = _evidence(operation, {"user_id": "1", "host_id": "h", "paginate": True}, send)
    assert evidence["status"] == "failed" and evidence["complete"] is False
