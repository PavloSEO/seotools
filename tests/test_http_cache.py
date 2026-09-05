"""HTTP response cache: freshness policy, Vary, revalidation, safety. No network.

The freshness policy is the point of #16, so it is tested directly against
``freshness_lifetime`` and ``ResponseCache.decide`` rather than only through a full crawl —
this is what lets "stale vs fresh" and "revalidate vs miss" be asserted precisely.
"""

from __future__ import annotations

import os
import pickle
from concurrent.futures import ThreadPoolExecutor

from seohead.crawl import cache as http_cache
from seohead.crawl.cache import (
    SCHEMA_VERSION,
    CacheEntry,
    ResponseCache,
    corrected_initial_age,
    freshness_lifetime,
)

# ── freshness_lifetime: the stated policy, directly ─────────────────────────


def test_no_store_means_never_cached():
    _max_age, no_store = freshness_lifetime({"cache-control": "no-store"})
    assert no_store is True


def test_no_cache_means_stored_but_immediately_stale():
    max_age, no_store = freshness_lifetime({"cache-control": "no-cache"})
    assert no_store is False
    assert max_age == 0.0


def test_max_age_sets_the_freshness_window():
    max_age, no_store = freshness_lifetime({"cache-control": "max-age=120"})
    assert no_store is False
    assert max_age == 120.0


def test_expires_is_computed_against_date():
    headers = {
        "date": "Wed, 01 Jan 2025 00:00:00 GMT",
        "expires": "Wed, 01 Jan 2025 00:05:00 GMT",
    }
    max_age, _no_store = freshness_lifetime(headers)
    assert max_age == 300.0


def test_no_freshness_information_at_all_is_treated_as_already_stale():
    """The stated, conservative default: "unstated" is not "forever"."""
    max_age, no_store = freshness_lifetime({})
    assert no_store is False
    assert max_age == 0.0


def test_max_age_wins_over_expires_when_both_are_present():
    headers = {
        "cache-control": "max-age=10",
        "date": "Wed, 01 Jan 2025 00:00:00 GMT",
        "expires": "Wed, 01 Jan 2025 01:00:00 GMT",
    }
    max_age, _ = freshness_lifetime(headers)
    assert max_age == 10.0


# ── #352: upstream Age must not restart freshness at local receipt ─────────


def test_max_age_60_with_age_59_misses_after_two_seconds_with_no_validator(monkeypatch, tmp_path):
    """The issue's own reproduction: a response already 59s old on arrival is stale after two
    more local seconds (61 > 60), and with no validator that is a plain miss."""
    now = 1_000_000.0
    monkeypatch.setattr(http_cache.time, "time", lambda: now)
    cache = ResponseCache(tmp_path, mode="live")
    cache.store(
        "https://example.test/",
        {"User-Agent": "seohead"},
        200,
        {"cache-control": "max-age=60", "age": "59"},
        "old body",
    )
    now += 2
    outcome = cache.decide("https://example.test/", {"User-Agent": "seohead"})
    assert outcome.status == "miss"


def test_max_age_60_with_age_59_revalidates_after_two_seconds_with_an_etag(monkeypatch, tmp_path):
    now = 1_000_000.0
    monkeypatch.setattr(http_cache.time, "time", lambda: now)
    cache = ResponseCache(tmp_path, mode="live")
    cache.store(
        "https://example.test/",
        {"User-Agent": "seohead"},
        200,
        {"cache-control": "max-age=60", "age": "59", "etag": '"v1"'},
        "old body",
    )
    now += 2
    outcome = cache.decide("https://example.test/", {"User-Agent": "seohead"})
    assert outcome.status == "revalidate"
    assert outcome.conditional_headers["If-None-Match"] == '"v1"'


def test_age_zero_preserves_normal_fresh_hit_behaviour(monkeypatch, tmp_path):
    """The control case named in the issue: an explicit ``Age: 0`` must behave exactly like no
    ``Age`` header at all — a fresh hit for the rest of the freshness window."""
    now = 1_000_000.0
    monkeypatch.setattr(http_cache.time, "time", lambda: now)
    cache = ResponseCache(tmp_path, mode="live")
    cache.store(
        "https://example.test/",
        {"User-Agent": "seohead"},
        200,
        {"cache-control": "max-age=60", "age": "0"},
        "fresh body",
    )
    now += 2
    outcome = cache.decide("https://example.test/", {"User-Agent": "seohead"})
    assert outcome.status == "hit"
    assert outcome.entry.body == "fresh body"


def test_date_skew_makes_a_response_stale_even_though_its_stated_age_is_small():
    """The RFC correction, not just ``max_age - age``: a response whose own ``Date`` is far in
    the past (an origin/cache clock far behind, or a long transit through an intermediary that
    did not update ``Age``) must be judged by the larger of "apparent age from Date" and the
    stated ``Age`` — never by ``Age`` alone. A naive ``max_age - age`` reading of this exact
    response would call it fresh (age=5 against max-age=800); the corrected initial age (~1000s,
    from Date) says otherwise.
    """
    from email.utils import formatdate

    receipt_time = 1_000_000.0
    date_hdr = formatdate(receipt_time - 1000, usegmt=True)  # Date says: created 1000s ago
    headers = {"date": date_hdr, "age": "5"}  # Age understates it
    age = corrected_initial_age(headers, receipt_time)
    assert age == 1000.0

    max_age, _no_store = freshness_lifetime({"cache-control": "max-age=800"})
    assert age >= max_age  # already stale on arrival, despite the small Age


def test_date_skew_does_not_undercut_a_larger_stated_age():
    """The same correction the other direction: a small apparent age from ``Date`` (clocks
    roughly agree, or ``Date`` is absent) must not undercut a larger, validly-parsed ``Age`` —
    the corrected initial age is the max of the two, not the min and not ``Date`` alone."""
    receipt_time = 1_000_000.0
    headers = {"age": "59"}  # no Date at all
    assert corrected_initial_age(headers, receipt_time) == 59.0


def test_invalid_age_values_are_ignored_and_never_lengthen_freshness(tmp_path):
    """Section 5.1: a syntactically invalid ``Age`` must be ignored, not parsed into something
    that shrinks the corrected initial age below what ``Date`` would otherwise say — and a
    negative value must never be treated as "younger than fresh"."""
    receipt_time = 1_000_000.0
    for bad in ("-5", "not-a-number", "", "5.5", "  ", "+5", "²", "٩"):
        assert corrected_initial_age({"age": bad}, receipt_time) == 0.0, bad

    # And end to end: a bogus Age on an otherwise-fresh response must not make it look older
    # than it is (that would be the unsafe direction) nor look artificially young forever — it
    # simply falls back to ordinary max-age freshness, as if Age had never been sent.
    cache = ResponseCache(tmp_path, mode="live")
    cache.store(
        "https://example.test/",
        {"User-Agent": "seohead"},
        200,
        {"cache-control": "max-age=60", "age": "-30"},
        "body",
    )
    outcome = cache.decide("https://example.test/", {"User-Agent": "seohead"})
    assert outcome.status == "hit"


def test_oversized_and_zero_padded_age_values_are_bounded_without_overflow():
    """A large valid header must cap before ``int`` sees it, while leading
    zeros still describe the same delta-seconds value."""
    receipt_time = 1_000_000.0
    assert corrected_initial_age({"age": "0" * 5_000 + "59"}, receipt_time) == 59.0
    assert corrected_initial_age({"age": "9" * 5_000}, receipt_time) == float(2**31 - 1)


def test_non_ascii_age_does_not_crash_cache_store(tmp_path):
    """Malformed Latin-1 response bytes must be ignored at the public store boundary."""
    cache = ResponseCache(tmp_path, mode="live")
    cache.store(
        "https://example.test/",
        {"User-Agent": "seohead"},
        200,
        {"cache-control": "max-age=60", "age": "²"},
        "body",
    )
    assert cache.decide("https://example.test/", {"User-Agent": "seohead"}).status == "hit"


def test_a_legacy_v2_disk_entry_is_never_read_as_fresh(tmp_path):
    """A v2 entry has no ``initial_age`` field. Defaulting a missing field to 0.0 during
    deserialization would silently re-introduce exactly the bug #352 fixes — a pre-existing
    disk entry treated as freshly originated here. Instead the schema-version bump this fix
    makes means such an entry is not read as valid at all: it is a miss, same as a v1 entry
    missing ``size_bytes`` before it."""
    import hashlib
    import json

    cache = ResponseCache(tmp_path, mode="live")
    url = "https://example.test/legacy"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    family_dir = tmp_path / digest[:2] / digest
    family_dir.mkdir(parents=True)
    legacy_payload = {
        "schema_version": "http_cache.v2",
        "url": url,
        "vary_headers": [],
        "request_header_values": {"user-agent": "seohead"},
        "status_code": 200,
        "headers": {"cache-control": "max-age=3600"},
        "body": "legacy body",
        "size_bytes": 11,
        "stored_at": 0.0,
        "max_age": 3600.0,
        # no initial_age at all
    }
    (family_dir / "variant.json").write_text(json.dumps(legacy_payload))

    assert SCHEMA_VERSION != "http_cache.v2"
    outcome = cache.decide(url, {"User-Agent": "seohead"})
    assert outcome.status == "miss"


# ── decide(): hit, revalidate, miss ─────────────────────────────────────────


def test_a_repeated_fetch_inside_the_freshness_window_is_a_hit(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store(
        "https://example.com/",
        {"User-Agent": "seohead"},
        200,
        {"cache-control": "max-age=3600"},
        "<html>fresh</html>",
    )
    outcome = cache.decide("https://example.com/", {"User-Agent": "seohead"})
    assert outcome.status == "hit"
    assert outcome.entry.body == "<html>fresh</html>"
    assert cache.stats["hits"] == 1


def test_an_expired_entry_with_a_validator_revalidates_not_a_fresh_fetch(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store(
        "https://example.com/",
        {"User-Agent": "seohead"},
        200,
        {"cache-control": "max-age=0", "etag": '"abc123"'},
        "<html>old</html>",
    )
    outcome = cache.decide("https://example.com/", {"User-Agent": "seohead"})
    assert outcome.status == "revalidate"
    assert outcome.conditional_headers["If-None-Match"] == '"abc123"'

    # A 304 confirms the body: recorded as a revalidation, never as a fresh fetch (only the
    # initial store() call above should ever count as a store).
    cache.refresh(outcome.entry, {"cache-control": "max-age=3600"})
    assert cache.stats["revalidations"] == 1
    assert cache.stats["stores"] == 1
    refreshed = cache.decide("https://example.com/", {"User-Agent": "seohead"})
    assert refreshed.status == "hit"
    assert refreshed.entry.body == "<html>old</html>"


def test_a_304_that_changes_vary_drops_the_old_variant(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store(
        "https://example.com/",
        {"User-Agent": "seohead"},
        200,
        {"cache-control": "max-age=0", "etag": '"abc123"', "vary": "User-Agent"},
        "old body",
    )
    outcome = cache.decide("https://example.com/", {"User-Agent": "seohead"})
    assert outcome.status == "revalidate"

    cache.refresh(
        outcome.entry,
        {"cache-control": "max-age=3600", "vary": "Accept-Language", "etag": '"new"'},
    )

    assert cache.decide("https://example.com/", {"User-Agent": "seohead"}).status == "miss"
    assert outcome.entry.body == "old body"
    assert outcome.entry.etag == '"new"'


def test_an_expired_entry_with_no_validator_is_a_plain_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store(
        "https://example.com/", {"User-Agent": "seohead"}, 200, {"cache-control": "max-age=0"}, "x"
    )
    outcome = cache.decide("https://example.com/", {"User-Agent": "seohead"})
    assert outcome.status == "miss"


def test_a_cold_url_is_a_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    outcome = cache.decide("https://example.com/never-seen", {"User-Agent": "seohead"})
    assert outcome.status == "miss"


def test_no_store_response_is_never_written(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store(
        "https://example.com/", {"User-Agent": "seohead"}, 200, {"cache-control": "no-store"}, "x"
    )
    assert cache.decide("https://example.com/", {"User-Agent": "seohead"}).status == "miss"
    assert cache.stats["bypassed"] == 1


def test_a_5xx_response_is_never_stored(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store(
        "https://example.com/", {"User-Agent": "seohead"}, 503, {"cache-control": "max-age=60"}, "x"
    )
    assert cache.decide("https://example.com/", {"User-Agent": "seohead"}).status == "miss"


# ── Vary ─────────────────────────────────────────────────────────────────────


def test_two_requests_differing_only_in_a_vary_listed_header_do_not_share_an_entry(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store(
        "https://example.com/",
        {"User-Agent": "desktop-ua"},
        200,
        {"cache-control": "max-age=3600", "vary": "User-Agent"},
        "<html>desktop</html>",
    )
    same_variant = cache.decide("https://example.com/", {"User-Agent": "desktop-ua"})
    other_variant = cache.decide("https://example.com/", {"User-Agent": "mobile-ua"})
    assert same_variant.status == "hit"
    assert other_variant.status == "miss"


def test_vary_star_is_never_cached(tmp_path):
    cache = ResponseCache(tmp_path)
    cache.store(
        "https://example.com/",
        {"User-Agent": "x"},
        200,
        {"cache-control": "max-age=60", "vary": "*"},
        "x",
    )
    assert cache.stats["bypassed"] == 1
    assert cache.stats["stores"] == 0
    assert cache.decide("https://example.com/", {"User-Agent": "x"}).status == "miss"


def test_two_variants_of_the_same_url_can_both_be_stored(tmp_path):
    cache = ResponseCache(tmp_path)
    for ua, body in (("desktop-ua", "desktop"), ("mobile-ua", "mobile")):
        cache.store(
            "https://example.com/",
            {"User-Agent": ua},
            200,
            {"cache-control": "max-age=3600", "vary": "User-Agent"},
            body,
        )
    desktop = cache.decide("https://example.com/", {"User-Agent": "desktop-ua"})
    mobile = cache.decide("https://example.com/", {"User-Agent": "mobile-ua"})
    assert desktop.entry.body == "desktop"
    assert mobile.entry.body == "mobile"


# ── #131: User-Agent is part of the key even when the origin never says Vary ───────────────


def test_a_different_user_agent_is_a_miss_even_without_a_vary_header(tmp_path):
    """The case every Vary test above omits: none of them exercise a response that stays
    silent about Vary, which is what almost every real site does. Without this, `_match`'s
    `all()` over an empty `vary_headers` list is vacuously true for any request."""
    cache = ResponseCache(tmp_path)
    cache.store(
        "https://example.com/",
        {"User-Agent": "A"},
        200,
        {"cache-control": "max-age=3600"},  # deliberately no Vary at all
        "desktop body",
    )
    same_ua = cache.decide("https://example.com/", {"User-Agent": "A"})
    other_ua = cache.decide("https://example.com/", {"User-Agent": "B"})
    assert same_ua.status == "hit"
    assert same_ua.entry.body == "desktop body"
    assert other_ua.status == "miss", "a different identity must never replay another one's body"


# ── replay mode and explicit invalidation ───────────────────────────────────


def test_replay_mode_serves_a_stale_entry_without_revalidating(tmp_path):
    cache = ResponseCache(tmp_path, mode="replay")
    cache.store(
        "https://example.com/",
        {"User-Agent": "x"},
        200,
        {"cache-control": "max-age=0"},
        "stale body",
    )
    outcome = cache.decide("https://example.com/", {"User-Agent": "x"})
    assert outcome.status == "hit"
    assert outcome.entry.body == "stale body"


def test_replay_mode_still_fetches_live_for_a_url_it_has_never_seen(tmp_path):
    cache = ResponseCache(tmp_path, mode="replay")
    assert cache.decide("https://example.com/new", {"User-Agent": "x"}).status == "miss"


def test_invalidate_forces_a_miss_but_still_allows_a_fresh_store(tmp_path):
    cache = ResponseCache(tmp_path, invalidate=True)
    cache.store(
        "https://example.com/",
        {"User-Agent": "x"},
        200,
        {"cache-control": "max-age=3600"},
        "old",
    )
    outcome = cache.decide("https://example.com/", {"User-Agent": "x"})
    assert outcome.status == "miss"
    assert cache.stats["invalidated"] == 1
    cache.store(
        "https://example.com/",
        {"User-Agent": "x"},
        200,
        {"cache-control": "max-age=3600"},
        "new",
    )
    # A later, non-invalidating cache pointed at the same directory sees the refreshed entry.
    plain = ResponseCache(tmp_path)
    assert plain.decide("https://example.com/", {"User-Agent": "x"}).entry.body == "new"


def test_off_mode_never_reads_or_writes(tmp_path):
    cache = ResponseCache(tmp_path, mode="off")
    cache.store(
        "https://example.com/", {"User-Agent": "x"}, 200, {"cache-control": "max-age=3600"}, "x"
    )
    assert cache.decide("https://example.com/", {"User-Agent": "x"}).status == "bypass"
    assert not os.listdir(tmp_path)


def test_build_returns_none_for_off_mode_or_no_directory():
    assert http_cache.build(None) is None
    assert http_cache.build("/tmp/whatever", mode="off") is None


# ── safety: a hostile or corrupt entry is a miss, never a crash ─────────────


def test_a_hostile_entry_file_cannot_execute_code_and_is_treated_as_a_miss(tmp_path):
    cache = ResponseCache(tmp_path)
    entry = CacheEntry(url="https://example.com/", stored_at=0, max_age=3600)
    path = cache._entry_path(entry)
    path.parent.mkdir(parents=True, exist_ok=True)

    marker = tmp_path / "pwned"

    class Payload:
        def __reduce__(self):
            return (os.system, (f"touch {marker}",))

    path.write_bytes(pickle.dumps(Payload()))

    outcome = cache.decide("https://example.com/", {})
    assert outcome.status == "miss"
    assert not marker.exists()


def test_a_truncated_entry_file_is_ignored_not_raised(tmp_path):
    cache = ResponseCache(tmp_path)
    entry = CacheEntry(url="https://example.com/", stored_at=0, max_age=3600)
    path = cache._entry_path(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema_version": ')
    assert cache.decide("https://example.com/", {}).status == "miss"


def test_a_world_writable_cache_directory_disables_the_cache_not_the_run(tmp_path):
    directory = tmp_path / "cache"
    directory.mkdir()
    os.chmod(directory, 0o777)
    cache = ResponseCache(directory)
    # Disabled, not raising: a broken cache degrades to "no cache".
    assert cache.decide("https://example.com/", {}).status == "bypass"
    cache.store("https://example.com/", {}, 200, {"cache-control": "max-age=60"}, "x")  # no crash


# ── concurrency: many threads, same and different URLs, no corruption ──────


def test_concurrent_stores_and_lookups_do_not_corrupt_the_cache_or_lose_stats(tmp_path):
    cache = ResponseCache(tmp_path)
    urls = [f"https://example.com/{i % 5}" for i in range(200)]  # heavy overlap on 5 keys

    def worker(i: int) -> str:
        url = urls[i]
        cache.store(
            url, {"User-Agent": "x"}, 200, {"cache-control": "max-age=3600"}, f"body-{i % 5}"
        )
        return cache.decide(url, {"User-Agent": "x"}).status

    with ThreadPoolExecutor(max_workers=16) as pool:
        statuses = list(pool.map(worker, range(200)))

    assert set(statuses) <= {"hit"}
    assert cache.stats["stores"] == 200
    # Every family directory still holds exactly one readable, valid variant.
    for i in range(5):
        outcome = cache.decide(f"https://example.com/{i}", {"User-Agent": "x"})
        assert outcome.status == "hit"
        assert outcome.entry.body == f"body-{i}"
