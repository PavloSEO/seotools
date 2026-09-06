"""SQL frontier identities and query spending match legacy queue semantics."""

import dataclasses

from seohead.crawl.collect import PageRecord
from seohead.storage.native_scan import NativeScan
from tests.test_scan_native import _metadata, _runtime


def seed(url, *, first=False, reason=""):
    return {
        "requested_url": url,
        "frontier_url": url,
        "depth": 0,
        "reason": reason,
        "source": "start" if first else "sitemap",
        "reserve_query": not first,
        "seed": not first,
    }


def test_seed_identity_keeps_request_text_and_skips_canonical_aliases(tmp_path):
    with NativeScan.create(tmp_path / "scan.sqlite", **_metadata()) as scan:
        assert scan.seed_frontier([seed("https://example.test/", first=True)])["queued"] == 1
        result = scan.seed_frontier(
            [
                seed("https://example.test"),
                seed("HTTPS://example.test/#anchor"),
                seed("https://example.test/a#original"),
                seed("https://example.test/a#other"),
            ]
        )
        assert result == {"queued": 1, "excluded": 0, "already_seen": 3}
        assert [
            row[0]
            for row in scan.con.execute(
                "SELECT u.url FROM frontier f JOIN urls u USING(url_id) ORDER BY queue_ordinal"
            )
        ] == ["https://example.test/", "https://example.test/a#original"]
    assert NativeScan.inspect(tmp_path / "scan.sqlite")["counts"]["frontier"] == 2


def test_rejected_seed_is_seen_without_spending_a_query_slot(tmp_path):
    with NativeScan.create(
        tmp_path / "scan.sqlite", **_metadata(**{"limits.max_query_variants_per_path": 1})
    ) as scan:
        scan.seed_frontier([seed("https://example.test/", first=True)])
        rejected = seed("https://example.test/a?one", reason="outside_host")
        assert scan.seed_frontier([rejected])["excluded"] == 1
        assert scan.seed_frontier([rejected])["already_seen"] == 1
        assert scan.seed_frontier([seed("https://example.test/a?two")])["queued"] == 1
        assert scan.con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert [tuple(row) for row in scan.con.execute("SELECT * FROM query_variants")] == [
            ("/a", "two")
        ]


def test_candidate_query_order_preserves_empty_query_and_exact_rejection(tmp_path):
    with NativeScan.create(
        tmp_path / "scan.sqlite", **_metadata(**{"limits.max_query_variants_per_path": 1})
    ) as scan:
        scan.seed_frontier([seed("https://example.test/", first=True)])
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            dataclasses.asdict(PageRecord(url=lease.url)),
            runtime=_runtime(),
            candidates=[
                {
                    "path_key": "/a",
                    "query_key": "",
                    "requested_url": "https://example.test/a#first",
                    "frontier_url": "https://example.test/a",
                    "depth": 1,
                },
                {
                    "path_key": "/a",
                    "query_key": "q=1",
                    "requested_url": "https://example.test/a?q=1#original",
                    "frontier_url": "https://example.test/a?q=1",
                    "depth": 1,
                },
            ],
        )
        assert [tuple(row) for row in scan.con.execute("SELECT * FROM query_variants")] == [
            ("/a", "")
        ]
        assert tuple(scan.con.execute("SELECT url,reason FROM decisions").fetchone()) == (
            "https://example.test/a?q=1#original",
            "query_variants_limit",
        )
        assert scan.claim(1)[0].url == "https://example.test/a"


def test_partial_observation_state_commits_with_page(tmp_path):
    with NativeScan.create(tmp_path / "scan.sqlite", **_metadata()) as scan:
        scan.seed_frontier([seed("https://example.test/", first=True)])
        lease = scan.claim(1)[0]
        scan.commit_page(
            lease,
            dataclasses.asdict(PageRecord(url=lease.url)),
            runtime=_runtime(),
            partial_reasons=["link_observations_omitted"],
        )
        assert scan.resume_snapshot()["scan"]["crawl_partial"] == 1
        assert "link_observations_omitted" in scan.resume_snapshot()["scan"]["limitations_json"]


def test_rejected_non_http_seed_keeps_the_original_decision_once(tmp_path):
    with NativeScan.create(tmp_path / "scan.sqlite", **_metadata()) as scan:
        scan.seed_frontier([seed("https://example.test/", first=True)])
        first = seed("MAILTO:hello@example.test#first", reason="outside_host")
        alias = seed("mailto:hello@example.test#again", reason="outside_host")
        assert scan.seed_frontier([first])["excluded"] == 1
        assert scan.seed_frontier([alias])["already_seen"] == 1
        assert tuple(scan.con.execute("SELECT url,reason FROM decisions").fetchone()) == (
            "MAILTO:hello@example.test#first",
            "outside_host",
        )
        assert scan.claim(1)[0].url == "https://example.test/"
