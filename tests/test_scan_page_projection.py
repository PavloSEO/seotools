"""Page projection can consume stored graph counts without rebuilding its edges."""

from types import SimpleNamespace

from seohead.crawl import evidence
from seohead.crawl.collect import PageRecord


def test_stored_graph_projection_keeps_measured_counts_without_inlinks_frame(monkeypatch):
    monkeypatch.setattr(
        evidence,
        "_inlinks_frame",
        lambda *_args: (_ for _ in ()).throw(AssertionError("edge frame")),
    )
    result = SimpleNamespace(
        pages=[PageRecord(url="https://example.test/", status_code=200)],
        links=[],
    )
    projected = evidence.build_evidence(
        result,
        inlink_counts={"https://example.test/": (7, 3)},
        stored_graph_available=True,
    )
    assert "all_inlinks" not in projected["frames"]
    assert "all_inlinks" in projected["found"]
    assert "all_inlinks" not in projected["missing"]
    row = projected["frames"]["internal_all"].iloc[0]
    assert row["Inlinks"] == 7
    assert row["Unique Inlinks"] == 3


def test_empty_stored_graph_retains_the_existing_unmeasured_population():
    result = SimpleNamespace(pages=[PageRecord(url="https://example.test/")], links=[])
    old = evidence.build_evidence(result)
    stored = evidence.build_evidence(result, stored_graph_available=False)
    assert stored["found"] == old["found"]
    assert stored["missing"] == old["missing"]
    assert stored["frames"]["internal_all"].equals(old["frames"]["internal_all"])
