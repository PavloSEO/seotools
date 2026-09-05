"""Offline tests for external data-source behavior around the API calls."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

import pytest

from seohead.data_sources import (
    arsenkin,
    credentials,
    crtsh,
    crux,
    indexnow,
    spend,
    wayback,
    yandex_cloud,
)
from seohead.data_sources import gsc as gsc_core

# --- Credentials -----------------------------------------------------------


def test_credential_from_env_wins(monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "  from-environment  ")
    assert credentials.read("missing/path", "SOME_TOKEN") == "from-environment"


def test_credential_missing_names_path_but_not_value(monkeypatch, tmp_path):
    monkeypatch.delenv("SOME_TOKEN", raising=False)
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    with pytest.raises(credentials.MissingCredential) as exc:
        credentials.read("svc/token", "SOME_TOKEN")
    message = str(exc.value)
    assert "svc/token" in message and "SOME_TOKEN" in message


def test_credential_empty_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("SOME_TOKEN", raising=False)
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "token").write_text("   \n", encoding="utf-8")
    with pytest.raises(credentials.MissingCredential):
        credentials.read("svc/token", "SOME_TOKEN")


def test_available_is_false_without_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("NOPE", raising=False)
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    assert credentials.available("nope/token", "NOPE") is False


# --- DataForSEO readiness (login AND password, issue #341) -----------------


def _clear_dataforseo_env(monkeypatch):
    monkeypatch.delenv("DATAFORSEO_LOGIN", raising=False)
    monkeypatch.delenv("DATAFORSEO_PASSWORD", raising=False)


def test_dataforseo_ready_requires_both_login_and_password(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    _clear_dataforseo_env(monkeypatch)
    assert credentials.dataforseo_ready() == (False, {"login": False, "password": False})


def test_dataforseo_ready_is_false_with_login_only(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    _clear_dataforseo_env(monkeypatch)
    monkeypatch.setenv("DATAFORSEO_LOGIN", "synthetic-login")
    assert credentials.dataforseo_ready() == (False, {"login": True, "password": False})


def test_dataforseo_ready_is_false_with_password_only(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    _clear_dataforseo_env(monkeypatch)
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "synthetic-password")
    assert credentials.dataforseo_ready() == (False, {"login": False, "password": True})


def test_dataforseo_ready_is_true_with_both_components(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    _clear_dataforseo_env(monkeypatch)
    monkeypatch.setenv("DATAFORSEO_LOGIN", "synthetic-login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "synthetic-password")
    assert credentials.dataforseo_ready() == (True, {"login": True, "password": True})


def test_dataforseo_ready_treats_blank_values_as_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    _clear_dataforseo_env(monkeypatch)
    monkeypatch.setenv("DATAFORSEO_LOGIN", "   ")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "synthetic-password")
    assert credentials.dataforseo_ready() == (False, {"login": False, "password": True})


def test_dataforseo_ready_never_exposes_the_secret_values(monkeypatch, tmp_path):
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    _clear_dataforseo_env(monkeypatch)
    monkeypatch.setenv("DATAFORSEO_LOGIN", "super-secret-login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "super-secret-password")
    ready, components = credentials.dataforseo_ready()
    serialized = json.dumps({"ready": ready, "components": components})
    assert "super-secret-login" not in serialized
    assert "super-secret-password" not in serialized


@pytest.mark.parametrize(
    ("ready", "components"),
    [
        (False, {"login": True, "password": False}),
        (False, {"login": False, "password": True}),
        (True, {"login": True, "password": True}),
    ],
    ids=["login_only", "password_only", "both"],
)
def test_sources_doctor_uses_shared_dataforseo_readiness(monkeypatch, tmp_path, ready, components):
    """The public doctor must use the two-component readiness decision, not login alone."""
    from seohead.servers import handlers

    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)
    monkeypatch.setattr(credentials, "available", lambda *_args: False)
    monkeypatch.setattr(credentials, "dataforseo_ready", lambda: (ready, components))

    dataforseo = handlers.sources_doctor()["sources"]["dataforseo"]
    assert dataforseo["ready"] is ready
    assert dataforseo["components"] == components


# --- Spend journal ---------------------------------------------------------


@pytest.fixture()
def journal(monkeypatch, tmp_path):
    monkeypatch.setenv("SEOHEAD_SPEND_LOG", str(tmp_path / "spend.jsonl"))
    return tmp_path / "spend.jsonl"


def test_spend_records_and_sums_by_unit(journal):
    spend.record("arsenkin", "keyword_exact", cost=120, unit="limits", task_id=555, items=40)
    spend.record("arsenkin", "keyword_exact", cost=30, unit="limits", task_id=556, items=10)
    spend.record("yandex_cloud", "wordstat.topRequests", cost=1, unit="requests", items=1)

    report = spend.report()
    assert report["calls"] == 3
    assert report["by_source"]["arsenkin"]["limits"] == 150.0
    assert report["by_source"]["yandex_cloud"]["requests"] == 1.0
    assert report["by_operation"]["arsenkin.keyword_exact"]["limits"] == 150.0


def test_spend_keeps_task_ids_so_paid_results_can_be_refetched(journal):
    spend.record("arsenkin", "top", cost=10, task_id=1)
    spend.record("arsenkin", "top", cost=10, task_id=2)
    spend.record("yandex_cloud", "serp", cost=1)  # No task ID is available.
    assert spend.paid_task_ids("arsenkin") == [1, 2]
    assert spend.paid_task_ids("yandex_cloud") == []


def test_spend_survives_broken_line(journal):
    spend.record("arsenkin", "top", cost=5)
    with journal.open("a", encoding="utf-8") as handle:
        handle.write("this is not JSON\n")
    spend.record("arsenkin", "top", cost=5)
    assert spend.report()["calls"] == 2  # The malformed line is skipped without breaking the log.


def test_spend_report_since_filters_by_day(journal, monkeypatch):
    spend.record("arsenkin", "top", cost=5)
    rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    old = dict(rows[0], at="2020-01-01T00:00:00")
    with journal.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(old, ensure_ascii=False) + "\n")
    assert spend.report()["calls"] == 1
    assert spend.report(since="2026-01-01")["calls"] == 0


def test_spend_report_on_missing_log_is_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("SEOHEAD_SPEND_LOG", str(tmp_path / "missing.jsonl"))
    report = spend.report()
    assert report["calls"] == 0 and report["by_source"] == {}


# --- Arsenkin rate limiting and usage accounting --------------------------


def test_rate_limiter_keeps_headroom_under_the_wall():
    limiter = arsenkin.RateLimiter(max_calls=30, period=60.0, safety=3)
    assert limiter.max_calls == 27  # Headroom prevents bursts of HTTP 429 responses.


def test_rate_limiter_never_drops_below_one():
    assert arsenkin.RateLimiter(max_calls=2, safety=10).max_calls == 1


@pytest.mark.parametrize(
    "data,expected",
    [
        ({"keywords": ["alpha", "beta", "gamma"]}, 3),
        ({"words": "alpha\nbeta\n\ngamma\n"}, 3),
        ({"urls": []}, 0),
        ({"unrelated": "value"}, 0),
    ],
)
def test_count_items_for_journal(data, expected):
    assert arsenkin._count_items(data) == expected


def test_refetch_is_get_so_paid_result_is_not_bought_twice():
    assert arsenkin.ArsenkinClient.refetch is arsenkin.ArsenkinClient.get


# --- Yandex Cloud normalization and SERP parsing --------------------------


# Cyrillic fixtures intentionally verify Russian case folding and yo-character normalization.
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Тёплый   ПОЛ ", "теплый пол"),
        ("ЁЛКА", "елка"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize(raw, expected):
    assert yandex_cloud.normalize(raw) == expected


def test_parse_serp_extracts_position_url_domain_title():
    xml = (
        "<doc><url>https://www.example.com/a</url><title>First result</title></doc>"
        "<doc><url>https://search.example/x</url><domain>search.example</domain>"
        "<title>Second result</title></doc>"
    )
    docs = yandex_cloud.parse_serp(xml)
    assert [d["pos"] for d in docs] == [1, 2]
    assert docs[0]["domain"] == "example.com"  # The ``www`` prefix is removed.
    assert docs[0]["title"] == "First result"
    assert docs[1]["domain"] == "search.example"


def test_parse_serp_strips_highlight_tags_inside_title():
    xml = (
        "<doc><url>https://search.example/</url><title>buy<hlword>ing</hlword> a pump</title></doc>"
    )
    assert yandex_cloud.parse_serp(xml)[0]["title"] == "buying a pump"


def test_parse_serp_on_empty_input_is_empty_not_error():
    assert yandex_cloud.parse_serp("") == []


def test_serp_body_never_asks_for_sync_search():
    """Synchronous search is deliberately absent because it costs 16 times more."""
    # The Cyrillic query intentionally exercises the Russian Yandex search type.
    body = yandex_cloud._serp_body(
        "тест", "225", "SEARCH_TYPE_RU", 10, 1, "FAMILY_MODE_NONE", "folder-1"
    )
    assert body["responseFormat"] == "FORMAT_XML"
    assert body["query"]["queryText"] == "тест"
    assert not hasattr(yandex_cloud.WebSearch, "search_sync")


# --- Regions ---------------------------------------------------------------

# Cyrillic region names intentionally verify Yandex's Russian aliases and canonical names.


def test_region_lookup_understands_both_official_and_api_names():
    """The official and API-specific names resolve to the same federal district."""
    from seohead.data_sources import yandex_regions as regions

    assert regions.by_name("Поволжье") == "40"
    assert regions.by_name("Приволжский") == "40"
    assert regions.by_name("Дальневосточный") == regions.by_name("Дальний Восток") == "73"


def test_region_lookup_returns_none_for_unknown():
    from seohead.data_sources import yandex_regions as regions

    assert regions.by_name("Atlantis") is None


def test_vladivostok_city_is_not_the_district():
    """Code 75 is the city and 73 the district; mixing them distorts demand data."""
    from seohead.data_sources import yandex_regions as regions

    assert regions.CITIES["Владивосток"] == "75"
    assert regions.DISTRICTS["Дальний Восток"] == "73"


def test_every_district_alias_points_at_a_real_district():
    from seohead.data_sources import yandex_regions as regions

    assert all(target in regions.DISTRICTS for target in regions.DISTRICT_ALIASES.values())


# --- Yandex Metrica --------------------------------------------------------


def test_metrika_backoff_respects_retry_after_header():
    """A valid Retry-After value takes precedence over the local backoff formula."""
    from seohead.data_sources.metrika import MetrikaClient

    assert MetrikaClient._backoff(1, "5") == 5.0
    assert MetrikaClient._backoff(1, "600") == 60.0  # Never wait longer than one minute.
    assert MetrikaClient._backoff(3, None) == 4.0  # Fall back to exponential backoff.
    assert MetrikaClient._backoff(1, "not-a-number") == 1.0  # Ignore invalid headers.


def test_metrika_backoff_is_capped():
    from seohead.data_sources.metrika import MAX_BACKOFF, MetrikaClient

    assert MetrikaClient._backoff(20, None) == MAX_BACKOFF


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{"message": "Counter not found"}', "Counter not found"),
        ('{"errors": [{"message": "Invalid metric"}]}', "Invalid metric"),
        ('{"errors": ["Flat error string"]}', "Flat error string"),
        ("not JSON at all", "not JSON at all"),
        ("", "empty response"),
    ],
)
def test_metrika_error_message_is_extracted_from_api_answer(payload, expected):
    from seohead.data_sources.metrika import _api_message

    assert _api_message(payload) == expected


def test_metrika_error_carries_status():
    from seohead.data_sources.metrika import MetrikaError

    exc = MetrikaError(429, "Too many requests")
    assert exc.status == 429 and "429" in str(exc)


def test_metrika_url_drops_empty_params_but_keeps_zero():
    from seohead.data_sources.metrika import MetrikaClient

    url = MetrikaClient._url(
        "stat/v1/data", {"limit": 100, "offset": 0, "filters": "", "preset": None}
    )
    assert "limit=100" in url and "offset=0" in url
    assert "filters" not in url and "preset" not in url


def test_metrika_rows_to_records_pairs_dimensions_with_metrics():
    """Pair Metrica's parallel dimension and metric arrays without shifting columns."""
    from seohead.data_sources.metrika import rows_to_records

    report = {
        "query": {"dimensions": ["ym:s:startURL"], "metrics": ["ym:s:visits", "ym:s:users"]},
        "data": [
            {"dimensions": [{"name": "/blog"}], "metrics": [120, 90]},
            {"dimensions": [{"name": "/about"}], "metrics": [10, 8]},
        ],
    }
    assert rows_to_records(report) == [
        {"startURL": "/blog", "visits": 120, "users": 90},
        {"startURL": "/about", "visits": 10, "users": 8},
    ]


def test_metrika_rows_to_records_survives_missing_query_and_extra_columns():
    from seohead.data_sources.metrika import rows_to_records

    report = {"data": [{"dimensions": ["plain string"], "metrics": [1]}]}
    assert rows_to_records(report) == [{"dimension_0": "plain string", "metric_0": 1}]
    assert rows_to_records({}) == []


def test_metrika_row_cap_exists_so_a_typo_cannot_pull_a_million_rows():
    from seohead.data_sources import metrika

    assert metrika.ROW_CAP == 100_000
    assert metrika.PAGE_PAUSE > 0  # Paging without a pause can exhaust the request quota.


def test_metrika_paginated_failure_keeps_the_usage_already_made(monkeypatch, journal):
    """A page-two failure must not erase the request that page one already spent.

    Before the fix, the aggregate ``report.paginated`` spend row was written only after the
    whole loop finished, so an exception on a later page left the journal with no entry at
    all — hiding both the successful first page and the failing second attempt from anyone
    diagnosing an interrupted collection.
    """
    from seohead.data_sources import metrika

    client = metrika.MetrikaClient(token="synthetic")
    calls = {"count": 0}

    def fake_request(_url):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"data": [{"metrics": [1]}] * 100, "total_rows": 300, "query": {}}
        raise metrika.MetrikaError(503, "synthetic outage")

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr(metrika.time, "sleep", lambda _seconds: None)

    with pytest.raises(metrika.MetrikaError) as exc:
        client.report({"metrics": "ym:s:visits"}, paginate=True, limit=100)

    assert exc.value.status == 503
    assert calls["count"] == 2  # Both the successful page and the failing attempt ran.

    rows = spend.read_all()
    assert len(rows) == 1  # The interrupted collection still leaves usage in the journal.
    assert rows[0]["cost"] == 2  # One completed page plus the one that raised.
    assert rows[0]["items"] == 100  # Rows collected before the failure are not discarded.
    assert rows[0]["extra"]["outcome"] == "failed"


# --- Arsenkin task batches -------------------------------------------------


class _FakeClient:
    """Offline client double with controllable submission and polling failures."""

    def __init__(self, fail_on=(), fail_wait_on=()):
        self.fail_on, self.fail_wait_on = set(fail_on), set(fail_wait_on)
        self.set_calls = []

    def set_task(self, tools_name, data):
        label = data.get("label")
        self.set_calls.append(label)
        if label in self.fail_on:
            raise arsenkin.ArsenkinError("400", f"invalid task {label}")
        return {"task_id": 1000 + len(self.set_calls), "cost": 10, "raw": {}}

    def wait(self, task_id, **kwargs):
        if task_id in self.fail_wait_on:
            raise arsenkin.ArsenkinError("TIMEOUT", f"task {task_id} timed out")
        return {"result": {"task": task_id}}

    def get(self, task_id):
        return {"result": {"refetched": task_id}}


def _jobs(*labels):
    return [
        {"tools_name": "wordstat", "data": {"label": label}, "label": label} for label in labels
    ]


def test_batch_keeps_input_order():
    runner = arsenkin.BatchRunner(client=_FakeClient())
    results = runner.run(_jobs("alpha", "beta", "gamma", "delta"))
    assert [r["label"] for r in results] == ["alpha", "beta", "gamma", "delta"]


def test_batch_one_bad_job_does_not_kill_the_rest():
    """A mid-batch exception must not orphan results from already paid tasks."""
    runner = arsenkin.BatchRunner(client=_FakeClient(fail_on={"beta"}))
    results = runner.run(_jobs("alpha", "beta", "gamma"))
    assert "error" in results[1] and results[1]["code"] == "400"
    assert "result" in results[0] and "result" in results[2]


def test_batch_failed_wait_still_returns_task_id_because_it_is_paid():
    client = _FakeClient(fail_wait_on={1001})
    results = arsenkin.BatchRunner(client=client).run(_jobs("one"))
    assert results[0]["task_id"] == 1001  # Return the identifier for the paid task.
    assert results[0]["cost"] == 10
    assert "error" in results[0]


def test_batch_respects_the_five_task_api_ceiling():
    from seohead.data_sources.arsenkin import MAX_CONCURRENT

    assert MAX_CONCURRENT == 5
    runner = arsenkin.BatchRunner(client=_FakeClient())
    assert runner._max_concurrent == 5


def test_batch_refetch_is_free_and_goes_through_get():
    client = _FakeClient()
    assert arsenkin.BatchRunner(client=client).refetch(777) == {"result": {"refetched": 777}}


def test_batch_on_empty_list_is_empty():
    assert arsenkin.BatchRunner(client=_FakeClient()).run([]) == []


# --- DataForSEO: Google ----------------------------------------------------


@pytest.mark.parametrize(
    "country", ["RU", "ru", "Россия", "россия", "РФ", "Russia", "BY", "Беларусь", "belarus"]
)
def test_geo_guard_blocks_geos_dataforseo_does_not_have(country):
    """Block unsupported geographies before a paid request returns no data."""
    # Cyrillic aliases intentionally verify localized Russia and Belarus inputs.
    from seohead.data_sources.dataforseo import geo_guard

    blocked = geo_guard(country)
    assert blocked is not None
    assert blocked["ok"] is False
    assert blocked["unsupported_geo"] in {"RU", "BY"}
    assert blocked["use_instead"]  # Always recommend the appropriate alternative provider.


@pytest.mark.parametrize("country", ["US", "de", "India", None, ""])
def test_geo_guard_lets_supported_geo_through(country):
    from seohead.data_sources.dataforseo import geo_guard

    assert geo_guard(country) is None


@pytest.mark.parametrize("location_code,iso", [(2643, "RU"), (2112, "BY")])
def test_geo_guard_blocks_location_code_even_without_country(location_code, iso):
    """``location_code`` is the field actually sent on the wire; it must be checked on its own."""
    from seohead.data_sources.dataforseo import geo_guard

    blocked = geo_guard(None, location_code)
    assert blocked is not None
    assert blocked["ok"] is False
    assert blocked["unsupported_geo"] == iso


def test_geo_guard_lets_supported_location_code_through():
    from seohead.data_sources.dataforseo import geo_guard

    assert geo_guard(None, 2840) is None  # United States


@pytest.mark.parametrize("location_code", [2643, 2112])
@pytest.mark.parametrize(
    "func_name,args",
    [
        ("search_volume", (["buy apartment"],)),
        ("keyword_ideas", ("buy apartment",)),
        ("keyword_difficulty", (["buy apartment"],)),
        ("serp", ("buy apartment",)),
    ],
)
def test_blocked_location_code_never_reaches_the_network(
    monkeypatch, location_code, func_name, args
):
    """A Russia/Belarus ``location_code`` must be rejected before any provider function posts.

    Reproduces the issue exactly: ``country`` is never supplied, only the numeric geo-target
    that DataForSEO actually bills on.
    """
    from seohead.data_sources import dataforseo

    def fail_urlopen(*_args, **_kwargs):
        raise AssertionError("must not reach the network for a blocked geo target")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)

    func = getattr(dataforseo, func_name)
    result = func(*args, location_code=location_code, country=None, env="prod")
    assert result["ok"] is False
    assert result["unsupported_geo"] in {"RU", "BY"}


def test_default_environment_is_sandbox_so_nothing_is_charged_by_accident(monkeypatch):
    monkeypatch.delenv("DATAFORSEO_ENV", raising=False)
    from seohead.data_sources.dataforseo import SANDBOX_BASE, DataForSEOClient

    client = DataForSEOClient()
    assert client.env == "sandbox" and client.base == SANDBOX_BASE


def test_prod_requires_an_explicit_switch(monkeypatch):
    from seohead.data_sources.dataforseo import PROD_BASE, DataForSEOClient

    monkeypatch.setenv("DATAFORSEO_ENV", "prod")
    assert DataForSEOClient().base == PROD_BASE
    monkeypatch.delenv("DATAFORSEO_ENV")
    assert DataForSEOClient(env="prod").base == PROD_BASE


def test_task_items_survives_none_at_every_nesting_level():
    """Handle ``None`` at every level of the nested task-result-item response."""
    from seohead.data_sources.dataforseo import task_items

    assert task_items({}) == []
    assert task_items({"tasks": None}) == []
    assert task_items({"tasks": [{"result": None}]}) == []
    assert task_items({"tasks": [{"result": [{"items": None}]}]}) == []
    assert task_items({"tasks": [{"result": [{"items": []}]}]}) == []
    assert task_items({"tasks": [{"result": [{"items": [{"keyword": "alpha"}]}]}]}) == [
        {"keyword": "alpha"}
    ]


def test_task_items_merges_several_tasks():
    from seohead.data_sources.dataforseo import task_items

    body = {
        "tasks": [
            {"result": [{"items": [{"keyword": "alpha"}, {"keyword": "beta"}]}]},
            {"result": [{"items": [{"keyword": "gamma"}]}]},
        ]
    }
    assert [i["keyword"] for i in task_items(body)] == ["alpha", "beta", "gamma"]


def test_task_errors_reports_everything_except_success_code():
    from seohead.data_sources.dataforseo import task_errors

    body = {
        "tasks": [
            {"status_code": 20000, "status_message": "Ok."},
            {"status_code": 40501, "status_message": "Invalid Field: 'location_code'"},
        ]
    }
    errors = task_errors(body)
    assert len(errors) == 1 and "40501" in errors[0]


def test_endpoints_are_v3_and_live_where_expected():
    from seohead.data_sources.dataforseo import ENDPOINTS

    assert all(path.startswith("v3/") for path in ENDPOINTS.values())
    assert "live" in ENDPOINTS["search_volume"]


def test_error_message_comes_from_the_api_not_from_us():
    from seohead.data_sources.dataforseo import _message

    assert _message('{"status_message": "Payment Required."}') == "Payment Required."
    assert _message('{"tasks":[{"status_message":"Invalid Field"}]}') == "Invalid Field"
    assert _message("") == "empty response"


# --- Network-loss during a billed call must not retry blindly --------------
#
# Each fake ``urlopen`` would SUCCEED on a second attempt (see the ``len(calls)`` branch below),
# so a passing test proves the client stops after the lost response instead of getting lucky on
# a retry it never should have made.


def test_dataforseo_network_error_does_not_retry_and_logs_the_lost_attempt(monkeypatch, journal):
    monkeypatch.setenv("DATAFORSEO_LOGIN", "test-login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "test-password")
    from seohead.data_sources import dataforseo

    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        raise urllib.error.URLError("simulated network failure")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = dataforseo.search_volume(
        ["buy apartment"], location_code=2840, country=None, env="prod"
    )

    assert result["ok"] is False
    assert len(calls) == 1  # The identical payload is never resent to the live endpoint.
    rows = spend.read_all()
    assert len(rows) == 1  # The lost attempt is recorded, not silently dropped.
    assert rows[0]["source"] == "dataforseo"
    assert rows[0]["cost"] == 0.0
    assert rows[0]["extra"]["attempt_failed"] == "network_error"


def test_dataforseo_malformed_response_still_creates_a_receipt(monkeypatch, journal):
    """Extends #306's receipt-after-deserialization fix to DataForSEO: a received malformed
    body must not leave the spend journal empty, and it must not claim a confirmed charge or a
    confirmed zero cost."""
    monkeypatch.setenv("DATAFORSEO_LOGIN", "test-login")
    monkeypatch.setenv("DATAFORSEO_PASSWORD", "test-password")
    from seohead.data_sources import dataforseo

    class MalformedResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"{malformed"

    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        return MalformedResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(json.JSONDecodeError):
        dataforseo.search_volume(["buy apartment"], location_code=2840, country=None, env="prod")

    assert len(calls) == 1  # The request reached the provider exactly once.
    rows = spend.read_all()
    assert len(rows) == 1  # A receipt exists even though the body could not be parsed.
    entry = rows[0]
    assert entry["source"] == "dataforseo"
    assert entry["extra"]["response_received"] is True
    assert entry["extra"]["response_malformed"] is True
    assert entry["extra"]["charge_status"] == "unknown"
    assert entry["extra"]["cost_unknown"] is True

    # A confirmed zero-cost call for the same source is the neighbouring legitimate case: it
    # must stay in by_source, not get pulled into "uncertain" alongside the malformed receipt.
    spend.record("dataforseo", "search_volume.prod", cost=0.0, unit="usd", items=1)

    report = spend.report()
    assert report["uncertain"] == [entry]
    assert report["by_source"]["dataforseo"]["usd"] == 0.0
    assert report["calls"] == 2


def test_arsenkin_set_task_network_error_does_not_retry_and_logs_the_lost_attempt(
    monkeypatch, journal
):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        raise urllib.error.URLError("simulated network failure")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = arsenkin.ArsenkinClient(
        token="test-token", limiter=arsenkin.RateLimiter(max_calls=100)
    )
    with pytest.raises(arsenkin.ArsenkinError):
        client.set_task("keywords_frequency", {"keywords": ["alpha", "beta"]})

    assert len(calls) == 1  # /set is never resent once its response is lost.
    rows = spend.read_all()
    assert len(rows) == 1
    assert rows[0]["source"] == "arsenkin"
    assert rows[0]["cost"] == 0.0
    assert rows[0]["extra"]["attempt_failed"] == "network_error"


def test_arsenkin_read_only_endpoint_still_retries_on_network_error(monkeypatch):
    """Only ``/set`` is billed; ``/check`` is a read and stays safe to retry."""
    monkeypatch.setattr(arsenkin.time, "sleep", lambda _seconds: None)
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(request)
        raise urllib.error.URLError("still failing, just proving a retry was attempted")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = arsenkin.ArsenkinClient(
        token="test-token", limiter=arsenkin.RateLimiter(max_calls=100)
    )
    with pytest.raises(arsenkin.ArsenkinError):
        client.check(123)

    assert len(calls) >= 2  # Idempotent reads keep retrying past one lost response.


def test_yandex_cloud_wordstat_top_network_error_does_not_retry_and_logs_the_lost_attempt(
    monkeypatch, journal
):
    calls = []

    def fake_urlopen(request, timeout=None, context=None):
        calls.append(request)
        raise urllib.error.URLError("simulated network failure")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = yandex_cloud.Wordstat(api_key="test-key", folder_id="test-folder")
    with pytest.raises(yandex_cloud.NetworkAmbiguousError):
        client.top("buy apartment")

    assert len(calls) == 1  # topRequests is never resent once its response is lost.
    rows = spend.read_all()
    assert len(rows) == 1
    assert rows[0]["source"] == "yandex_cloud"
    assert rows[0]["extra"]["attempt_failed"] == "network_error"


def test_yandex_cloud_websearch_submit_network_error_does_not_retry_and_logs_the_lost_attempt(
    monkeypatch, journal
):
    calls = []

    def fake_urlopen(request, timeout=None, context=None):
        calls.append(request)
        raise urllib.error.URLError("simulated network failure")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = yandex_cloud.WebSearch(api_key="test-key", folder_id="test-folder")
    with pytest.raises(yandex_cloud.NetworkAmbiguousError):
        client.search("buy apartment")

    assert len(calls) == 1  # searchAsync is never resent once its response is lost.
    rows = spend.read_all()
    assert len(rows) == 1
    assert rows[0]["source"] == "yandex_cloud"


def test_yandex_cloud_search_batch_isolates_a_lost_response_from_the_rest_of_the_batch(
    monkeypatch, journal
):
    """One query's lost response must not abort queries already queued in the same batch."""
    calls = []

    def fake_urlopen(request, timeout=None, context=None):
        calls.append(request)
        if len(calls) == 2:  # The second query's submission loses its response.
            raise urllib.error.URLError("simulated network failure")
        return _FakeSearchAsyncResponse(f"op-{len(calls)}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = yandex_cloud.WebSearch(api_key="test-key", folder_id="test-folder")
    results = client.search_batch(["alpha", "beta", "gamma"], timeout=0)

    assert "beta" in results and "error" in results["beta"]  # Logged, not silently dropped.
    rows = spend.read_all()
    assert any(r.get("extra", {}).get("attempt_failed") == "network_error" for r in rows)


class _FakeSearchAsyncResponse:
    """Minimal context-manager double for a successful ``searchAsync`` submission."""

    status = 200

    def __init__(self, operation_id: str):
        self._body = json.dumps({"id": operation_id}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def test_yandex_cloud_search_batch_reports_a_rejected_submission_as_an_error_not_a_timeout(
    monkeypatch, journal
):
    """A provider rejection (HTTP 4xx) must never be billed or read back as a lost timeout.

    Before the fix, a non-200 submission was silently dropped from the returned mapping, and the
    caller's only signal was the query's absence — indistinguishable from an operation that was
    genuinely billed and simply timed out while polling.
    """

    def fake_urlopen(request, timeout=None, context=None):
        raise _make_http_error(400, '{"message": "invalid query"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    client = yandex_cloud.WebSearch(api_key="test-key", folder_id="test-folder")
    results = client.search_batch(["synthetic invalid query"], timeout=0)

    entry = results["synthetic invalid query"]
    assert entry["status"] == "rejected"
    assert "400" in entry["error"]
    assert entry["docs"] == []
    assert "operation_id" not in entry

    rows = spend.read_all()
    assert len(rows) == 1
    assert rows[0]["cost"] == 0  # A rejected submission was never billed.
    assert rows[0]["extra"]["operation_ids"] == {}  # No operation exists to recover.


def test_serp_fetch_never_calls_a_rejected_query_billed(monkeypatch, journal):
    """serp_fetch's note must not claim a rejected query's operation is in the spend journal."""
    from seohead.servers import handlers

    def fake_urlopen(request, timeout=None, context=None):
        raise _make_http_error(400, '{"message": "invalid query"}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    real_websearch = yandex_cloud.WebSearch
    monkeypatch.setattr(
        yandex_cloud,
        "WebSearch",
        lambda: real_websearch(api_key="test-key", folder_id="test-folder"),
    )

    result = handlers.serp_fetch(query="synthetic invalid query")

    assert result["not_returned"] == []  # A rejection is an error, not a timeout.
    assert result["note"] is None
    assert result["results"]["synthetic invalid query"]["status"] == "rejected"


def test_yandex_cloud_search_batch_dedupes_exact_duplicate_queries_before_billing(
    monkeypatch, journal
):
    """A repeated query string must be billed once and its one result must stay retrievable.

    Before the fix, each duplicate created its own paid operation, but both wrote into the same
    dict key: the later completion silently overwrote the former, hiding one paid result forever
    while the ledger showed two charges with no operation id to recover either from.
    """
    calls = []

    def fake_urlopen(request, timeout=None, context=None):
        calls.append(request)
        if request.get_method() == "GET":
            return _FakeOperationDoneResponse("https://example.com/", "result")
        return _FakeSearchAsyncResponse("only-operation")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(yandex_cloud.time, "sleep", lambda _seconds: None)

    client = yandex_cloud.WebSearch(api_key="test-key", folder_id="test-folder")
    results = client.search_batch(["same query", "same query"])

    submissions = [c for c in calls if c.get_method() == "POST"]
    assert len(submissions) == 1  # The duplicate never reaches the provider a second time.
    assert list(results) == ["same query"]
    assert results["same query"]["operation_id"] == "only-operation"

    rows = spend.read_all()
    assert len(rows) == 1
    assert rows[0]["cost"] == 1  # Billed exactly once for the one operation created.
    assert rows[0]["extra"]["operation_ids"] == {"same query": "only-operation"}


def test_serp_fetch_reconciles_requested_and_returned_for_duplicate_queries(monkeypatch, journal):
    """requested/returned must reconcile exactly, including when the input repeats a query."""
    from seohead.servers import handlers

    def fake_urlopen(request, timeout=None, context=None):
        if request.get_method() == "GET":
            return _FakeOperationDoneResponse("https://example.com/", "result")
        return _FakeSearchAsyncResponse("only-operation")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(yandex_cloud.time, "sleep", lambda _seconds: None)
    real_websearch = yandex_cloud.WebSearch
    monkeypatch.setattr(
        yandex_cloud,
        "WebSearch",
        lambda: real_websearch(api_key="test-key", folder_id="test-folder"),
    )

    result = handlers.serp_fetch(queries=["same query", "same query"])

    assert result["requested"] == 1
    assert result["returned"] == 1
    assert result["not_returned"] == []


class _FakeOperationDoneResponse:
    """Minimal context-manager double for a completed ``searchAsync`` operation."""

    status = 200

    def __init__(self, url: str, title: str):
        xml = f"<doc><url>{url}</url><title>{title}</title></doc>"
        body = {"done": True, "response": {"rawData": base64.b64encode(xml.encode()).decode()}}
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


# --- CLI list parsing ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("alpha,beta,gamma", ["alpha", "beta", "gamma"]),
        ("  alpha , beta ", ["alpha", "beta"]),
        ("single", ["single"]),
        ("", None),
        (None, None),
        ("alpha,,beta", ["alpha", "beta"]),
    ],
)
def test_split_list_plain(raw, expected):
    from seohead.cli import _split_list

    assert _split_list(raw) == expected


def test_split_list_keeps_comma_inside_quotes():
    """A quoted comma must not split one query into two paid requests."""
    from seohead.cli import _split_list

    assert _split_list("'CDM pumps — specifications, selection, and prices'") == [
        "CDM pumps — specifications, selection, and prices"
    ]
    assert _split_list("'first, with a comma','second'") == ["first, with a comma", "second"]
    assert _split_list('"alpha, beta",gamma') == ["alpha, beta", "gamma"]


# --- Wayback Machine CDX (keyless, issue #97) -------------------------------


_CDX_HEADER = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]


def test_wayback_history_parses_a_recorded_cdx_response():
    """Fixture shape recorded from a real ``.../cdx/search/cdx?...&output=json`` response."""
    body = json.dumps(
        [
            _CDX_HEADER,
            [
                "com,example)/",
                "20200101000000",
                "https://example.com/",
                "text/html",
                "200",
                "ABCD1234",
                "1024",
            ],
            [
                "com,example)/",
                "20230601000000",
                "https://example.com/",
                "text/html",
                "404",
                "EFGH5678",
                "512",
            ],
        ]
    )
    result = wayback.history("https://example.com/", fetcher=lambda url: body)
    assert result["ok"] is True
    assert result["count"] == 2
    assert result["snapshots"][0]["statuscode"] == "200"
    assert result["snapshots"][1]["statuscode"] == "404"
    assert result["snapshots"][1]["archived_url"] == (
        "https://web.archive.org/web/20230601000000/https://example.com/"
    )


def test_wayback_history_builds_the_query_from_optional_filters():
    captured = {}

    def fetcher(url):
        captured["url"] = url
        return ""

    wayback.history(
        "https://example.com/", limit=5, from_date="2024", to_date="20260101", fetcher=fetcher
    )
    assert "url=https%3A%2F%2Fexample.com%2F" in captured["url"]
    assert "limit=5" in captured["url"]
    assert "from=2024" in captured["url"]
    assert "to=20260101" in captured["url"]


def test_wayback_history_empty_response_is_not_an_error():
    """No snapshot at all is a fact, not a failure: the CDX server returns a fully empty body."""
    result = wayback.history("https://example.com/never-archived", fetcher=lambda url: "")
    assert result == {
        "ok": True,
        "url": "https://example.com/never-archived",
        "count": 0,
        "snapshots": [],
    }


def test_wayback_history_non_json_response_is_reported_not_raised():
    result = wayback.history("https://example.com/", fetcher=lambda url: "<html>error</html>")
    assert result["ok"] is False
    assert "not JSON" in result["error"]


def test_wayback_history_empty_array_is_not_an_error():
    """The documented empty-result shape: a JSON array with nothing in it."""
    result = wayback.history("https://example.com/never-archived", fetcher=lambda url: "[]")
    assert result == {
        "ok": True,
        "url": "https://example.com/never-archived",
        "count": 0,
        "snapshots": [],
    }


def test_wayback_history_recognized_header_without_rows_is_not_an_error():
    """A valid CDX header can legitimately be the whole non-empty response."""
    result = wayback.history(
        "https://example.com/never-archived", fetcher=lambda url: json.dumps([_CDX_HEADER])
    )
    assert result == {
        "ok": True,
        "url": "https://example.com/never-archived",
        "count": 0,
        "snapshots": [],
    }


def test_wayback_history_json_object_error_payload_is_reported_not_silent():
    """A synthetically injected object error payload must not read as zero snapshots."""
    body = json.dumps({"error": "synthetic provider failure"})
    result = wayback.history("https://example.com/", fetcher=lambda url: body)
    assert result["ok"] is False
    assert "url" in result and result["url"] == "https://example.com/"
    assert "count" not in result


def test_wayback_history_malformed_non_empty_array_is_reported_not_silent():
    """A one-element array whose entry is not a header row must not read as zero snapshots."""
    body = json.dumps([{"error": "synthetic provider failure"}])
    result = wayback.history("https://example.com/", fetcher=lambda url: body)
    assert result["ok"] is False
    assert "count" not in result


def test_wayback_history_error_shaped_string_array_is_reported_not_silent():
    """A list of strings is only a header when it names the required CDX fields."""
    body = json.dumps([["error", "synthetic provider failure"]])
    result = wayback.history("https://example.com/", fetcher=lambda url: body)
    assert result["ok"] is False
    assert "count" not in result


def test_wayback_history_network_error_is_reported_not_raised():
    def fetcher(url):
        raise urllib.error.URLError("simulated network failure")

    result = wayback.history("https://example.com/", fetcher=fetcher)
    assert result["ok"] is False
    assert "request failed" in result["error"]


def test_wayback_history_requires_a_url():
    with pytest.raises(ValueError):
        wayback.history("")


# --- Certificate Transparency / crt.sh (keyless, issue #97) -----------------


def test_crtsh_subdomains_parses_a_recorded_response():
    """Fixture shape recorded from a real ``crt.sh/?q=%.example.com&output=json`` response."""
    body = json.dumps(
        [
            {"common_name": "example.com", "name_value": "example.com\nwww.example.com"},
            {"common_name": "*.app.example.com", "name_value": "*.app.example.com"},
            {"common_name": "unrelated-domain.test", "name_value": "unrelated-domain.test"},
        ]
    )
    result = crtsh.subdomains("example.com", fetcher=lambda url: body)
    assert result["ok"] is True
    assert result["subdomains"] == ["app.example.com", "example.com", "www.example.com"]
    assert result["count"] == 3


def test_crtsh_subdomains_empty_response_is_not_an_error():
    result = crtsh.subdomains("example.com", fetcher=lambda url: "")
    assert result == {"ok": True, "domain": "example.com", "count": 0, "subdomains": []}


def test_crtsh_subdomains_non_json_response_is_reported_not_raised():
    """crt.sh serves an HTML page under load instead of its JSON API; that must not read as zero."""
    result = crtsh.subdomains("example.com", fetcher=lambda url: "<html>overloaded</html>")
    assert result["ok"] is False
    assert "overloaded" in result["error"] or "not JSON" in result["error"]


def test_crtsh_subdomains_empty_array_is_not_an_error():
    """The documented empty-result shape: a JSON array with nothing in it."""
    result = crtsh.subdomains("example.com", fetcher=lambda url: "[]")
    assert result == {"ok": True, "domain": "example.com", "count": 0, "subdomains": []}


def test_crtsh_subdomains_json_object_error_payload_is_reported_not_silent():
    """A synthetically injected object error payload must not read as zero subdomains."""
    body = json.dumps({"error": "synthetic provider failure"})
    result = crtsh.subdomains("example.com", fetcher=lambda url: body)
    assert result["ok"] is False
    assert "count" not in result


def test_crtsh_subdomains_malformed_non_empty_array_is_reported_not_silent():
    """An array entry with neither expected field must not read as zero subdomains."""
    body = json.dumps([{"error": "synthetic provider failure"}])
    result = crtsh.subdomains("example.com", fetcher=lambda url: body)
    assert result["ok"] is False
    assert "count" not in result


def test_crtsh_subdomains_network_error_is_reported_not_raised():
    def fetcher(url):
        raise urllib.error.URLError("simulated network failure")

    result = crtsh.subdomains("example.com", fetcher=fetcher)
    assert result["ok"] is False


def test_crtsh_subdomains_requires_a_domain():
    with pytest.raises(ValueError):
        crtsh.subdomains("")


# --- Google Search Console (credential-gated skeleton, issue #97) ----------


def test_gsc_search_analytics_missing_credential_never_reaches_the_network(monkeypatch, tmp_path):
    monkeypatch.delenv("GSC_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)

    def fail_fetcher(payload, token):
        raise AssertionError("must not reach the network without a token")

    result = gsc_core.search_analytics(
        "sc-domain:example.com",
        start_date="2026-01-01",
        end_date="2026-01-31",
        fetcher=fail_fetcher,
    )
    assert result == {
        "ok": False,
        "error": (
            f"credential not found: store it in {tmp_path / 'gsc' / 'access_token'} or set "
            "$GSC_ACCESS_TOKEN. See docs/SETUP.md for how to obtain a Search Console OAuth token."
        ),
    }


def test_gsc_search_analytics_parses_a_recorded_response():
    body = json.dumps(
        {
            "rows": [
                {
                    "keys": ["technical seo"],
                    "clicks": 12,
                    "impressions": 400,
                    "ctr": 0.03,
                    "position": 8.4,
                },
            ]
        }
    )
    result = gsc_core.search_analytics(
        "sc-domain:example.com",
        start_date="2026-01-01",
        end_date="2026-01-31",
        token="fake-token",
        fetcher=lambda payload, token: body,
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["rows"][0]["clicks"] == 12
    assert result["rows"][0]["keys"] == ["technical seo"]


def test_gsc_inspect_url_parses_a_recorded_response():
    body = json.dumps(
        {
            "inspectionResult": {
                "indexStatusResult": {
                    "verdict": "PASS",
                    "coverageState": "Submitted and indexed",
                    "indexingState": "INDEXING_ALLOWED",
                    "googleCanonical": "https://example.com/",
                    "userCanonical": "https://example.com/",
                }
            }
        }
    )
    result = gsc_core.inspect_url(
        "sc-domain:example.com",
        "https://example.com/",
        token="fake-token",
        fetcher=lambda payload, token: body,
    )
    assert result["ok"] is True
    assert result["coverage_state"] == "Submitted and indexed"
    assert result["verdict"] == "PASS"


def _make_http_error(code: int, body: str) -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(
        url="https://example.invalid",
        code=code,
        msg="error",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(body.encode()),
    )


def test_gsc_http_error_extracts_the_api_message_without_leaking_the_token():
    def fetcher(payload, token):
        raise _make_http_error(403, json.dumps({"error": {"message": "no permission"}}))

    result = gsc_core.search_analytics(
        "sc-domain:example.com",
        start_date="2026-01-01",
        end_date="2026-01-31",
        token="secret-bearer-token",
        fetcher=fetcher,
    )
    assert result["ok"] is False
    assert result["status"] == 403
    assert "permission" in result["error"]
    assert "secret-bearer-token" not in result["error"]


def test_gsc_default_date_range_is_a_completed_inclusive_28_day_pacific_window():
    from datetime import date, datetime, timedelta

    start, end = gsc_core.default_date_range()
    expected_end = datetime.now(gsc_core.PACIFIC).date() - timedelta(days=1)
    expected_start = expected_end - timedelta(days=27)
    assert (date.fromisoformat(start), date.fromisoformat(end)) == (expected_start, expected_end)


def test_gsc_search_analytics_resolves_the_legacy_relative_labels_to_iso_dates():
    """The public CLI/MCP default (``28daysAgo``/``today``) must resolve to the documented
    Pacific-Time window instead of reaching the outbound payload unresolved."""
    captured = {}

    def fetcher(payload, token):
        captured.update(payload)
        return json.dumps({"rows": []})

    result = gsc_core.search_analytics(
        "sc-domain:example.com",
        start_date="28daysAgo",
        end_date="today",
        token="fake-token",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    expected_start, expected_end = gsc_core.default_date_range()
    assert captured["startDate"] == expected_start
    assert captured["endDate"] == expected_end
    assert result["period"] == f"{expected_start}..{expected_end}"


def test_gsc_search_analytics_omitted_dates_also_resolve_to_iso_dates():
    captured = {}

    def fetcher(payload, token):
        captured.update(payload)
        return json.dumps({"rows": []})

    result = gsc_core.search_analytics("sc-domain:example.com", token="fake-token", fetcher=fetcher)
    assert result["ok"] is True
    assert captured["startDate"] == result["period"].split("..")[0]
    assert captured["endDate"] == result["period"].split("..")[1]


def test_gsc_search_analytics_passes_explicit_valid_iso_dates_through_unchanged():
    captured = {}

    def fetcher(payload, token):
        captured.update(payload)
        return json.dumps({"rows": []})

    result = gsc_core.search_analytics(
        "sc-domain:example.com",
        start_date="2026-01-01",
        end_date="2026-01-31",
        token="fake-token",
        fetcher=fetcher,
    )
    assert result["ok"] is True
    assert captured == {
        "startDate": "2026-01-01",
        "endDate": "2026-01-31",
        "dimensions": ["query"],
        "rowLimit": 1000,
    }
    assert result["period"] == "2026-01-01..2026-01-31"


@pytest.mark.parametrize(
    ("start_date", "end_date"),
    [
        ("2026-13-01", "2026-01-31"),  # Not a real calendar date.
        ("2026-01-31", "2026-01-01"),  # Reversed range.
        ("01/01/2026", "2026-01-31"),  # Wrong format.
    ],
)
def test_gsc_search_analytics_rejects_invalid_or_reversed_dates_without_calling_the_fetcher(
    start_date, end_date
):
    def fail_fetcher(payload, token):
        raise AssertionError("must not reach the network with an invalid date range")

    result = gsc_core.search_analytics(
        "sc-domain:example.com",
        start_date=start_date,
        end_date=end_date,
        token="fake-token",
        fetcher=fail_fetcher,
    )
    assert result["ok"] is False
    assert "error" in result


def test_gsc_query_handler_rejects_an_unknown_mode():
    from seohead.servers.handlers import gsc_query

    with pytest.raises(ValueError):
        gsc_query(site_url="sc-domain:example.com", mode="bogus")


def test_gsc_query_handler_requires_inspection_url_for_inspect_mode():
    from seohead.servers.handlers import gsc_query

    with pytest.raises(ValueError):
        gsc_query(site_url="sc-domain:example.com", mode="inspect_url")


# --- Chrome UX Report / CrUX (credential-gated skeleton, issue #97) --------


def test_crux_query_missing_credential_never_reaches_the_network(monkeypatch, tmp_path):
    monkeypatch.delenv("CRUX_API_KEY", raising=False)
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)

    def fail_fetcher(payload, api_key):
        raise AssertionError("must not reach the network without an API key")

    result = crux.query(url="https://example.com/", fetcher=fail_fetcher)
    assert result["ok"] is False
    assert "crux/api_key" in result["error"]


def test_crux_query_parses_a_recorded_response():
    body = json.dumps(
        {
            "record": {
                "key": {"formFactor": "PHONE"},
                "collectionPeriod": {"firstDate": {"year": 2026, "month": 1, "day": 1}},
                "metrics": {
                    "largest_contentful_paint": {"percentiles": {"p75": 2100}},
                    "cumulative_layout_shift": {"percentiles": {"p75": "0.05"}},
                },
            }
        }
    )
    result = crux.query(url="https://example.com/", api_key="fake-key", fetcher=lambda p, k: body)
    assert result["ok"] is True
    assert result["form_factor"] == "PHONE"
    assert result["metrics"]["largest_contentful_paint"]["p75"] == 2100


def test_crux_query_404_means_no_data_not_a_failure():
    def fetcher(payload, api_key):
        raise _make_http_error(404, json.dumps({"error": {"message": "not found"}}))

    result = crux.query(origin="https://tiny-site.example", api_key="fake-key", fetcher=fetcher)
    assert result == {
        "ok": True,
        "target": "https://tiny-site.example",
        "metrics": {},
        "note": "no CrUX data",
    }


def test_crux_query_requires_exactly_one_of_url_or_origin():
    with pytest.raises(ValueError):
        crux.query(api_key="fake-key")
    with pytest.raises(ValueError):
        crux.query(url="https://example.com/", origin="https://example.com", api_key="fake-key")


def test_crux_query_key_travels_in_a_header_not_the_request_url():
    """The key must never be able to leak through a URL echoed into a log or an exception."""
    import inspect

    source = inspect.getsource(crux._default_fetcher)
    assert "X-goog-api-key" in source
    assert "?" not in source.split("urllib.request.Request(")[1].split(",")[0]


# --- IndexNow (credential-gated skeleton, issue #97) ------------------------


def test_indexnow_submit_missing_credential_never_reaches_the_network(monkeypatch, tmp_path):
    monkeypatch.delenv("INDEXNOW_KEY", raising=False)
    monkeypatch.setattr(credentials, "CONFIG_ROOT", tmp_path)

    def fail_fetcher(payload):
        raise AssertionError("must not reach the network without a key")

    result = indexnow.submit(["https://example.com/a"], host="example.com", fetcher=fail_fetcher)
    assert result["ok"] is False
    assert "indexnow/key" in result["error"]


def test_indexnow_submit_success_names_google_as_not_adopted():
    result = indexnow.submit(
        ["https://example.com/a", "https://example.com/b"],
        host="example.com",
        key="fake-key",
        fetcher=lambda payload: (200, ""),
    )
    assert result["ok"] is True
    assert result["submitted"] == 2
    assert result["not_adopted_by"] == ["Google"]


def test_indexnow_submit_rejects_an_oversized_batch_before_touching_credentials_or_network():
    def fail_fetcher(payload):
        raise AssertionError("must not reach the network over the batch limit")

    urls = [f"https://example.com/{i}" for i in range(indexnow.MAX_URLS_PER_BATCH + 1)]
    result = indexnow.submit(urls, host="example.com", key="fake-key", fetcher=fail_fetcher)
    assert result["ok"] is False
    assert "10000" in result["error"] or "10,000" in result["error"]


def test_indexnow_submit_reports_the_documented_status_message():
    result = indexnow.submit(
        ["https://example.com/a"],
        host="example.com",
        key="fake-key",
        fetcher=lambda payload: (403, ""),
    )
    assert result["ok"] is False
    assert "key" in result["error"]


def test_indexnow_submit_network_error_is_reported_not_raised():
    def fetcher(payload):
        raise urllib.error.URLError("simulated network failure")

    result = indexnow.submit(
        ["https://example.com/a"], host="example.com", key="fake-key", fetcher=fetcher
    )
    assert result["ok"] is False


def test_indexnow_submit_requires_urls_and_host():
    with pytest.raises(ValueError):
        indexnow.submit([], host="example.com")
    with pytest.raises(ValueError):
        indexnow.submit(["https://example.com/a"], host="")
