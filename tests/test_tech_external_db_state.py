"""analyze_tech must not mistake a configured database's failure for absence (issue #240).

A broad ``except Exception`` around the optional external fingerprint database
caught a malformed supplied record and left ``external_db`` at ``{loaded:
false}`` — the exact state the tech-audit skill documents as "no database
connected". A user who supplied a database and got nothing from it was told
their setup was normal.
"""

import json

from seohead.recon import tech

HTML = "<p>ok</p>"


def _make_broken_db(tmp_path) -> str:
    """A database whose one record fails static matching, not just fails to load."""
    tech_dir = tmp_path / "src" / "technologies"
    tech_dir.mkdir(parents=True)
    (tmp_path / "categories.json").write_text(json.dumps({"1": {"name": "CMS"}}))
    # ``load_db`` accepts this shard happily; matching it is what raises, because
    # ``headers`` is expected to be a mapping and here it is a list.
    (tech_dir / "a.json").write_text(
        json.dumps({"BrokenDBRecord": {"cats": [1], "headers": ["not-a-mapping"]}})
    )
    return str(tmp_path)


def test_configured_db_failure_is_reported_as_its_own_state(tmp_path, monkeypatch):
    db_dir = _make_broken_db(tmp_path)
    monkeypatch.setenv("SEOHEAD_TECH_DB", db_dir)

    result = tech.analyze_tech(HTML, url="https://example.com/")
    external = result["external_db"]

    assert external["loaded"] is False
    assert external.get("state") == "external_db_failed"
    assert external.get("path") == db_dir
    assert "AttributeError" in external.get("error", "")
    # Built-in detection keeps running regardless of the external failure.
    assert result["ok"] is True


def test_unconfigured_db_keeps_the_plain_not_configured_state(monkeypatch):
    """Negative control: no SEOHEAD_TECH_DB must never look like a failure."""
    monkeypatch.delenv("SEOHEAD_TECH_DB", raising=False)

    result = tech.analyze_tech(HTML, url="https://example.com/")
    external = result["external_db"]

    assert external == {"loaded": False}
    assert "state" not in external
    assert "error" not in external
