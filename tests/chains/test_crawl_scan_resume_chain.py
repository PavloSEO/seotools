"""Issue #619, end to end on the real chain fixture: a native scan crawl killed mid-run and
resumed with ``--resume`` must fetch every URL exactly once across the two processes and
produce the audit an uninterrupted crawl of the same site produces.

Not a unit test of the resume plumbing -- ``crawl_to_scan`` has had that since the artifact
was designed. The property this pins is the one that only exists *between* the two runs:
that the second process re-reads the first one's frontier and settings rather than the
command line, so it neither refetches what is already stored nor continues under different
rules. It is asserted against the site's own request log, because the number that mattered
in #618 was how many times a third party's server was asked for the same page.
"""

from __future__ import annotations

import contextlib
from collections import Counter

import pytest

from seohead.servers import handlers
from seohead.storage import open_scan, read_audit
from seohead.storage.native_scan import NativeScan
from tests.chains import chain_site
from tests.chains.chain_site import run_chain_site
from tests.evidence_contract_assertions import assert_saved_contract, semantic_audit

BUILD = "a" * 40

# The two fields that differ between any two runs of this crawl, resumed or not, and say
# nothing about what was collected: when the audit was written, and how long one response
# took. Everything else in the audit is evidence and must match.
TIMING_FIELDS = frozenset({"generated_at", "response_time"})


@pytest.fixture
def site(monkeypatch):
    # The crawler refuses private-network targets unless explicitly authorized; a loopback
    # fixture is exactly the case that authorization exists for.
    monkeypatch.setenv("SEOHEAD_ALLOW_PRIVATE_NETWORKS", "1")
    with run_chain_site() as base_url:
        yield base_url


@pytest.fixture
def request_log(monkeypatch):
    """Every request the fixture site actually served, in order, as the site saw it."""
    log: list[str] = []
    served = chain_site._Handler.do_GET

    def counting(handler):
        log.append(handler.path)
        return served(handler)

    monkeypatch.setattr(chain_site._Handler, "do_GET", counting, raising=True)
    return log


class Killed(Exception):
    """Stands in for the kill signal, the OOM or the closed lid of issue #618."""


@contextlib.contextmanager
def killed_after(batches: int):
    """Stop the crawl between batches, where a killed process leaves no request in flight.

    Restores the real ``claim`` itself rather than through ``monkeypatch``, because the
    resumed run in the same test still needs the site's request log patched in place.
    """
    claim = NativeScan.claim
    seen = {"batches": 0}

    def counting_claim(scan, count):
        seen["batches"] += 1
        if seen["batches"] > batches:
            raise Killed("the crawl process died between batches")
        return claim(scan, count)

    NativeScan.claim = counting_claim
    try:
        yield
    finally:
        NativeScan.claim = claim


def _differences(left, right, path=""):
    """Every path at which two audit documents disagree, timing fields aside."""
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            if key in TIMING_FIELDS:
                continue
            if key not in left or key not in right:
                yield f"{path}/{key}"
            else:
                yield from _differences(left[key], right[key], f"{path}/{key}")
    elif isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            yield f"{path}[{len(left)} vs {len(right)}]"
        else:
            for index, (one, other) in enumerate(zip(left, right, strict=True)):
                yield from _differences(one, other, f"{path}[{index}]")
    elif left != right:
        yield path


def _saved_semantic_audit(path):
    with open_scan(path) as scan:
        audit = read_audit(str(path))
        assert_saved_contract(audit, scan)
    return semantic_audit(audit)


def test_a_killed_scan_crawl_resumed_fetches_every_url_once_and_audits_the_same(
    site, request_log, tmp_path
):
    whole = tmp_path / "whole.sqlite"
    handlers.crawl_site(url=f"{site}/", scan_out=str(whole), min_delay=0, producer_build=BUILD)
    uninterrupted = Counter(request_log)
    assert uninterrupted, "the fixture site served nothing"
    request_log.clear()

    resumable = tmp_path / "resumed.sqlite"
    with killed_after(batches=2), pytest.raises(Killed):
        handlers.crawl_site(
            url=f"{site}/", scan_out=str(resumable), min_delay=0, producer_build=BUILD
        )
    interrupted_counts = NativeScan.inspect(resumable)["counts"]
    assert 0 < interrupted_counts["pages"] < uninterrupted.total()

    resumed = handlers.crawl_site(resume=str(resumable), producer_build=BUILD)

    assert resumed["resumed"] is True
    assert resumed["partial"] is False
    assert resumed["finish_reason"] == "finished"
    assert resumed["audit_available"] is True

    # The property the resume exists for: across both processes the site was asked for each
    # URL exactly the number of times one uninterrupted crawl asks for it -- never twice.
    assert Counter(request_log) == uninterrupted
    assert max(Counter(request_log).values()) == 1

    # Exactly one difference, and it is the one that must be there: an audit that matched
    # in every other field would leave a resumed run indistinguishable from a whole one.
    assert set(_differences(_saved_semantic_audit(resumable), _saved_semantic_audit(whole))) == {
        "/run/crawl_resumed"
    }


def test_the_resumed_audit_still_records_that_it_was_resumed(site, tmp_path):
    """Byte-identical evidence must not make the two runs indistinguishable (#619)."""
    resumable = tmp_path / "resumed.sqlite"
    with killed_after(batches=2), pytest.raises(Killed):
        handlers.crawl_site(
            url=f"{site}/", scan_out=str(resumable), min_delay=0, producer_build=BUILD
        )

    handlers.crawl_site(resume=str(resumable), producer_build=BUILD)

    whole = tmp_path / "whole.sqlite"
    handlers.crawl_site(url=f"{site}/", scan_out=str(whole), min_delay=0, producer_build=BUILD)

    assert read_audit(str(resumable))["run"]["crawl_resumed"] is True
    assert read_audit(str(whole))["run"]["crawl_resumed"] is False


def test_a_resume_of_a_scan_from_another_site_is_refused_by_name(site, tmp_path):
    scan = tmp_path / "scan.sqlite"
    with killed_after(batches=2), pytest.raises(Killed):
        handlers.crawl_site(url=f"{site}/", scan_out=str(scan), min_delay=0, producer_build=BUILD)

    with pytest.raises(ValueError) as raised:
        handlers.crawl_site(
            url="https://elsewhere.example/", resume=str(scan), producer_build=BUILD
        )

    named = set(str(raised.value).translate(str.maketrans(",;", "  ")).split())
    assert {f"{site}/", "https://elsewhere.example/"} <= named
